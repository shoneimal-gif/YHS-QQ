"""IBKR Paper-only socket transport with one-attempt order writes.

The existing live-account read-only client remains untouched. This class reuses
its contract/data algorithms, but implements its own constructor, connection,
account guards and Paper-only writes. Its parent constructor/connect are never
called. Port plus an authenticated unique DU account plus an exact hash binding
are all required; a port by itself is not proof of a simulated account.

Primary account/port references:
https://www.interactivebrokers.co.jp/en/support/customer-service.php?p=email
https://www.interactivebrokers.com/docs/excel/rtd/connection-parameters
SDK implementation is pinned to ib_async 2.1.0. No username or password enters
this module. The caller must durably journal each reserved identity before its
before_send callback grants a single submission attempt. Missing orders or
missing executions never prove that a write did not arrive.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time as day_time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import importlib.metadata
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from ibkr_bridge.client import (IBKRReadOnlyError, ReadonlyIBKRClient, SDK_VERSION,
                                _NoSDKLog, _account_info, _decimal, _finite, _parse_occ)


_PAPER_ACCOUNT = re.compile(r"DU[A-Z0-9]{1,20}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"AAIBP:[0-9a-f]{32}\Z")
_EXEC = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_STATES = frozenset({"PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted",
                    "ApiPending", "ApiCancelled", "ApiUpdate", "Cancelled", "Filled",
                    "Inactive", "ValidationError"})
_DONE = frozenset({"Cancelled", "ApiCancelled", "Filled", "Inactive"})
_FLAGS = {"broker": "ibkr", "paper_only": True, "paper_verified": True}


class PaperClientError(ValueError):
    """A fixed safe category plus whether a mutation might have been sent."""
    def __init__(self, code: str, *, mutation_attempted: bool = False):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) else "paper_client_unavailable"
        self.category = self.code
        self.mutation_attempted = bool(mutation_attempted)
        super().__init__(self.code)


def _error(exc: Exception, fallback: str, *, attempted=False) -> PaperClientError:
    code = exc.code if isinstance(exc, (IBKRReadOnlyError, PaperClientError)) else fallback
    return PaperClientError(code, mutation_attempted=attempted)


def _stamp(value: Any) -> datetime:
    if isinstance(value, str) and len(value) <= 64:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PaperClientError("paper_timestamp_unconfirmed")
    return value.astimezone(timezone.utc)


def _integer(value: Any, *, positive=False, maximum=2**31-1) -> int:
    number = value if type(value) is int else _finite(value)
    if number is None or int(number) != number or number < (1 if positive else 0) or number > maximum:
        raise PaperClientError("paper_numeric_field_unconfirmed")
    return int(number)


class PaperIBKRClient(ReadonlyIBKRClient):
    """Separate Paper client. No live endpoint is accepted or initialized."""
    def __init__(self, *, host="127.0.0.1", port=4002, client_id=17932,
                 account_sha256_allowlist: Sequence[str] = (), execute_paper=False,
                 request_timeout=6.0, quote_timeout=3.0, max_quote_age_seconds=2.0):
        if host != "127.0.0.1" or type(port) is not int or port not in (4002, 7497):
            raise PaperClientError("local_paper_endpoint_required")
        if type(client_id) is not int or not 1 <= client_id <= 2**31-1:
            raise PaperClientError("paper_nonzero_client_id_required")
        if type(execute_paper) is not bool:
            raise PaperClientError("paper_execution_flag_invalid")
        if (not isinstance(account_sha256_allowlist, (list, tuple)) or len(account_sha256_allowlist) > 1
                or any(not isinstance(v, str) or _SHA.fullmatch(v) is None for v in account_sha256_allowlist)):
            raise PaperClientError("paper_account_allowlist_invalid")
        for value in (request_timeout, quote_timeout, max_quote_age_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.25 <= value <= 15:
                raise PaperClientError("paper_timeout_invalid")
        if max_quote_age_seconds > 2:
            raise PaperClientError("paper_quote_age_exceeds_policy")
        # Initialize the shared read-only algorithms' state explicitly. Calling
        # the live-only parent constructor with a dummy live port is forbidden.
        self.host, self.port, self.client_id = host, port, client_id
        self.execute_paper = execute_paper
        self.request_timeout, self.quote_timeout = float(request_timeout), float(quote_timeout)
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self._allowlist = frozenset(account_sha256_allowlist)
        self._ib = self._option_type = self._order_type = self._execution_filter_type = None
        self._accounts, self._account = (), None
        self._owner_thread = self._loop = None
        self._owns_loop = False
        self._sdk_log_filter, self._sdk_loggers = _NoSDKLog(), []
        self._sdk_parent_propagate = None
        self._sdk_null_handler = logging.NullHandler()
        self._account_subscription_account = self._account_subscription_started = None
        self._account_initial_download_utc = None
        self._account_receipts = {}
        self._qualified = {}
        self._grids = {}
        self._reserved = {}
        self._attempted_refs = set()
        self._attempted_ids = set()
        self._last_orders = {}
        self._known_orders = {}
        self._last_snapshot_monotonic = None

    def _guard(self, *, bound=False, write=False):
        self._main_thread()
        if (self.host != "127.0.0.1" or self.port not in (4002, 7497)
                or self.client_id <= 0 or self._ib is None or not self._ib.isConnected()
                or self._ib.client.host != self.host or self._ib.client.port != self.port
                or self._ib.wrapper.clientId != self.client_id):
            raise PaperClientError("paper_connection_unconfirmed")
        accounts = self._ib.managedAccounts()
        if (not isinstance(accounts, list) or len(accounts) != 1 or not _PAPER_ACCOUNT.fullmatch(str(accounts[0]))
                or tuple(accounts) != self._accounts):
            raise PaperClientError("unique_du_paper_account_required")
        if bound and (self._account != accounts[0] or _account_info(self._account)["account_sha256"] not in self._allowlist):
            raise PaperClientError("paper_account_binding_required")
        if write and not self.execute_paper:
            raise PaperClientError("paper_execution_not_enabled")

    def _connected(self, *, bound=False):
        self._guard(bound=bound)

    def _run(self, awaitable, code):
        try:
            return self._ib.run(asyncio.wait_for(awaitable, self.request_timeout))
        except Exception:
            self.close()
            raise PaperClientError(code) from None

    def connect(self):
        self._main_thread()
        if self._ib is not None:
            raise PaperClientError("paper_session_already_connected")
        if self.host != "127.0.0.1" or self.port not in (4002, 7497):
            raise PaperClientError("local_paper_endpoint_required")
        try:
            if importlib.metadata.version("ib_async") != SDK_VERSION:
                raise PaperClientError("sdk_version_mismatch")
            from ib_async import IB, Option, LimitOrder, ExecutionFilter, StartupFetchNONE
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._owns_loop = True
            self._loop, self._owner_thread = loop, threading.get_ident()
            self._ib, self._option_type = IB(), Option
            self._order_type, self._execution_filter_type = LimitOrder, ExecutionFilter
            self._private_sdk_logging()
            self._ib.accountValueEvent += self._account_value_received
            self._ib.RequestTimeout, self._ib.RaiseRequestErrors = self.request_timeout, True
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.request_timeout,
                             readonly=not self.execute_paper, raiseSyncErrors=True, fetchFields=StartupFetchNONE)
            accounts = self._ib.managedAccounts()
            if (not isinstance(accounts, list) or len(accounts) != 1
                    or not isinstance(accounts[0], str) or not _PAPER_ACCOUNT.fullmatch(accounts[0])):
                raise PaperClientError("unique_du_paper_account_required")
            self._accounts = tuple(accounts)
            self._guard()
            return dict(_FLAGS, connected=True, execute_paper=self.execute_paper,
                        managed_accounts=[_account_info(accounts[0])], account_bound=False,
                        orders_submitted=False, sdk_version=SDK_VERSION)
        except Exception as exc:
            self.close()
            raise _error(exc, "paper_connection_failed") from None

    def bind_account(self, account_sha256):
        self._guard()
        if (not isinstance(account_sha256, str) or account_sha256 not in self._allowlist
                or _account_info(self._accounts[0])["account_sha256"] != account_sha256):
            raise PaperClientError("paper_account_binding_required")
        if self._account is not None and self._account != self._accounts[0]:
            raise PaperClientError("paper_account_rebinding_refused")
        self._account = self._accounts[0]
        return dict(_FLAGS, **_account_info(self._account), account_bound=True)

    def _server_time(self):
        stamp = _stamp(self._run(self._ib.reqCurrentTimeAsync(), "paper_server_time_unavailable"))
        if abs((datetime.now(timezone.utc)-stamp).total_seconds()) > 5:
            raise PaperClientError("paper_server_clock_unconfirmed")
        return stamp

    @staticmethod
    def _contract_fields(contract):
        con_id = _integer(contract.conId, positive=True)
        sec_type = str(contract.secType)
        occ = None
        if sec_type == "OPT":
            local = contract.localSymbol
            if isinstance(local, str):
                candidate = local.replace(" ", "")
                if re.fullmatch(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}", candidate):
                    occ = candidate
        return {"con_id": con_id, "occ_symbol": occ,
                "security_type": sec_type if sec_type in {"OPT", "STK", "FUT", "FOP", "BAG", "CASH", "BOND", "FUND", "CFD", "IND"} else "OTHER"}

    def _trade_fields(self, trade, *, broker_reported, completed=False, identity=None):
        order, status = trade.order, trade.orderStatus
        if order.account != self._account:
            raise PaperClientError("paper_order_account_unconfirmed")
        quantity = _finite(order.totalQuantity, positive=True)
        filled = _finite(order.filledQuantity) if completed else _finite(status.filled)
        remaining = 0.0 if completed else _finite(status.remaining)
        if quantity is None or filled is None or remaining is None or filled < 0 or remaining < 0 or filled > quantity:
            raise PaperClientError("paper_order_quantity_unconfirmed")
        state = str(status.status)
        if state not in _STATES:
            raise PaperClientError("paper_order_status_unconfirmed")
        if completed and state not in _DONE:
            raise PaperClientError("paper_completed_status_unconfirmed")
        if not completed and state in _DONE:
            # openOrder and SDK local cache do not carry an explicit cumulative
            # fill field. Only completedOrder.filledQuantity is used as terminal
            # quantity evidence, never the SDK's default filled=0.
            broker_reported = False
        ref = order.orderRef if isinstance(order.orderRef, str) and _REF.fullmatch(order.orderRef) else None
        return dict(self._contract_fields(trade.contract),
                    order_ref=ref, order_ref_unrecognized=ref is None,
                    order_id=identity["order_id"] if identity else _integer(order.orderId),
                    client_id=identity["client_id"] if identity else _integer(order.clientId),
                    perm_id=_integer(order.permId, maximum=2**63-1), action=str(order.action) if order.action in {"BUY", "SELL"} else "OTHER",
                    quantity=quantity, filled=filled, remaining=remaining,
                    limit_price=_finite(order.lmtPrice, positive=True) if order.orderType == "LMT" else None,
                    order_type=str(order.orderType) if order.orderType in {"LMT", "MKT", "STP", "STP LMT", "TRAIL", "REL", "PEG MKT", "MIT", "LIT"} else "OTHER",
                    status=state, owned_client=(identity["client_id"] if identity else order.clientId) == self.client_id,
                    broker_reported=bool(broker_reported),
                    quantity_evidence="broker_completed_filled_quantity" if completed else "nonterminal_order_status_or_local_cache")

    def register_known_orders(self, orders):
        """Use immutable, already broker-confirmed journal identities on resume.

        Completed-order messages omit API orderId/clientId. A durable permId
        association may restore those fields only after every immutable broker
        order field is compared; the registration never authorizes a new write.
        """
        if not isinstance(orders, list) or len(orders) > 10000:
            raise PaperClientError("paper_known_orders_invalid")
        for order in orders:
            if not isinstance(order, dict) or order.get("write_attempted") is not True:
                continue
            perm_id = order.get("perm_id")
            if type(perm_id) is not int or perm_id <= 0:
                continue
            ref = order.get("order_ref")
            if (not isinstance(ref, str) or not _REF.fullmatch(ref) or order.get("client_id") != self.client_id
                    or type(order.get("order_id")) is not int or order["order_id"] <= 0
                    or order.get("action") not in {"BUY", "SELL"}):
                raise PaperClientError("paper_known_orders_invalid")
            fields = {key: order[key] for key in ("order_ref", "order_id", "client_id", "perm_id", "con_id",
                                                   "occ_symbol", "action", "quantity")}
            fields["limit_price"] = str(_decimal(order["limit_price"], "invalid_reference_price", maximum="100000"))
            if perm_id in self._known_orders and self._known_orders[perm_id] != fields:
                raise PaperClientError("paper_known_order_identity_changed")
            self._known_orders[perm_id] = fields

    def _completed_identity(self, trade, executions, active):
        order, contract = trade.order, trade.contract
        ref = order.orderRef if isinstance(order.orderRef, str) and _REF.fullmatch(order.orderRef) else None
        if ref is None:
            return None
        perm_id = _integer(order.permId, positive=True, maximum=2**63-1)
        con = self._contract_fields(contract)
        quantity = _finite(order.totalQuantity, positive=True)
        price = _finite(order.lmtPrice, positive=True) if order.orderType == "LMT" else None
        if quantity is None or price is None or order.action not in {"BUY", "SELL"}:
            raise PaperClientError("paper_completed_order_fields_unconfirmed")
        candidates = set()
        known = self._known_orders.get(perm_id)
        if known is not None:
            if (known["order_ref"] != ref or known["con_id"] != con["con_id"] or known["occ_symbol"] != con["occ_symbol"]
                    or known["action"] != order.action or known["quantity"] != quantity
                    or Decimal(known["limit_price"]) != Decimal(str(price))):
                raise PaperClientError("paper_completed_order_identity_mismatch")
            candidates.add((known["client_id"], known["order_id"]))
        for row in active:
            if row["perm_id"] == perm_id and row["order_ref"] == ref:
                if (row["con_id"] != con["con_id"] or row["occ_symbol"] != con["occ_symbol"]
                        or row["action"] != order.action or row["quantity"] != quantity
                        or row["limit_price"] != price):
                    raise PaperClientError("paper_completed_order_identity_mismatch")
                candidates.add((row["client_id"], row["order_id"]))
        for fill in executions:
            if fill["perm_id"] == perm_id and fill["order_ref"] == ref:
                if (fill["con_id"] != con["con_id"] or fill["occ_symbol"] != con["occ_symbol"]
                        or fill["side"] != ("BOT" if order.action == "BUY" else "SLD")):
                    raise PaperClientError("paper_completed_execution_identity_mismatch")
                candidates.add((fill["client_id"], fill["order_id"]))
        # A positive permId in a known cached order originated in a broker
        # callback. A local pre-ACK object always has permId=0 and cannot qualify.
        for cached in self._ib.trades():
            other = cached.order
            if (other.account == self._account and other.permId == perm_id and other.orderRef == ref
                    and other.orderId > 0 and other.clientId > 0
                    and cached.contract.conId == con["con_id"] and other.action == order.action
                    and _finite(other.totalQuantity) == quantity and _finite(other.lmtPrice) == price):
                candidates.add((other.clientId, other.orderId))
        if len(candidates) != 1:
            raise PaperClientError("paper_completed_identity_unconfirmed")
        client_id, order_id = next(iter(candidates))
        if client_id <= 0 or order_id <= 0:
            raise PaperClientError("paper_completed_identity_unconfirmed")
        return {"client_id": client_id, "order_id": order_id}

    def _execution_fields(self, fill):
        execution = fill.execution
        if execution.acctNumber != self._account:
            raise PaperClientError("paper_execution_account_unconfirmed")
        if not isinstance(execution.execId, str) or not _EXEC.fullmatch(execution.execId):
            raise PaperClientError("paper_execution_id_unconfirmed")
        quantity, price = _finite(execution.shares, positive=True), _finite(execution.price, positive=True)
        if quantity is None or price is None or execution.side not in {"BOT", "SLD"}:
            raise PaperClientError("paper_execution_fields_unconfirmed")
        ref = execution.orderRef if isinstance(execution.orderRef, str) and _REF.fullmatch(execution.orderRef) else None
        return dict(self._contract_fields(fill.contract), exec_id=execution.execId, order_ref=ref,
                    order_id=_integer(execution.orderId), client_id=_integer(execution.clientId),
                    perm_id=_integer(execution.permId, maximum=2**63-1), side=execution.side, quantity=quantity,
                    price=price, executed_utc=_stamp(execution.time).isoformat(),
                    pending_price_revision=bool(execution.pendingPriceRevision))

    def snapshot(self):
        self._guard(bound=True)
        try:
            account = super().account_snapshot()
            # These distinct SDK request keys may be awaited together. Contract
            # data and the final source/quote checks remain sequential at submit.
            active, completed, fills, server_raw, raw_positions = self._run(asyncio.gather(
                self._ib.reqAllOpenOrdersAsync(), self._ib.reqCompletedOrdersAsync(False),
                self._ib.reqExecutionsAsync(self._execution_filter_type(acctCode=self._account)),
                self._ib.reqCurrentTimeAsync(), self._ib.reqPositionsAsync()), "paper_snapshot_requests_unavailable")
            if any(not isinstance(rows, list) or len(rows) > 10000 for rows in (active, completed, fills)):
                raise PaperClientError("paper_snapshot_size_limit")
            executions = [self._execution_fields(fill) for fill in fills]
            active_rows = [self._trade_fields(trade, broker_reported=True) for trade in active]
            completed_rows = [self._trade_fields(trade, broker_reported=True, completed=True,
                identity=self._completed_identity(trade, executions, active_rows)) for trade in completed]
            orders, seen = [], set()
            # If the order crosses from open to completed during this batch,
            # keep the open observation until the next unambiguous snapshot.
            for row in active_rows + completed_rows:
                identity = (row["client_id"], row["order_id"], row["perm_id"] if row["order_id"] == 0 else 0)
                if identity not in seen:
                    seen.add(identity)
                    orders.append(row)
            # Local writes may not yet have an ACK. Preserve them as unknown
            # risk, explicitly separate from order callbacks returned by IBKR.
            for trade in self._ib.trades():
                if trade.order.account != self._account or trade.order.clientId != self.client_id:
                    continue
                row = self._trade_fields(trade, broker_reported=False)
                identity = (row["client_id"], row["order_id"], row["perm_id"] if row["order_id"] == 0 else 0)
                if row["order_ref"] is not None and row["order_id"] == 0:
                    continue  # Unlinked completed cache defaults are not evidence.
                if identity not in seen and row["order_ref"] is not None:
                    seen.add(identity)
                    orders.append(row)
            positions = []
            if not isinstance(raw_positions, list) or len(raw_positions) > 5000:
                raise PaperClientError("paper_positions_unavailable")
            for position in raw_positions:
                if position.account != self._account:
                    raise PaperClientError("paper_position_account_unconfirmed")
                quantity, average = _finite(position.position), _finite(position.avgCost)
                if quantity is None or average is None:
                    raise PaperClientError("paper_position_quantity_unconfirmed")
                positions.append(dict(self._contract_fields(position.contract), quantity=quantity,
                                      average_cost_broker_units=average))
            if len({e["exec_id"] for e in executions}) != len(executions):
                raise PaperClientError("paper_execution_duplicates_unconfirmed")
            funds = [value for value in account["summary"] if value["tag"] == "AvailableFunds" and value["currency"] == "USD"]
            server_time = _stamp(server_raw)
            if abs((datetime.now(timezone.utc)-server_time).total_seconds()) > 5:
                raise PaperClientError("paper_server_clock_unconfirmed")
            self._guard(bound=True)
            self._last_orders = {(o["client_id"], o["order_id"]): o for o in orders}
            maximum_id = max([o["order_id"] for o in orders] + [e["order_id"] for e in executions] + [0])
            self._ib.client.updateReqId(maximum_id + 1)
            self._last_snapshot_monotonic = time.monotonic()
            return dict(_FLAGS, **_account_info(self._account), observed_utc=datetime.now(timezone.utc).isoformat(),
                        server_time_utc=server_time.isoformat(), market_open=None, complete=True,
                        positions=positions, orders=orders, executions=executions,
                        available_funds_usd=funds[0]["value"] if len(funds) == 1 else None,
                        funds_local_receipt_age_seconds=funds[0]["local_receipt_age_seconds"] if len(funds) == 1 else None,
                        funds_are_subscription_cache=True, positions_snapshot_completed_utc=datetime.now(timezone.utc).isoformat(),
                        order_absence_proves_no_submission=False, execution_history_is_complete_lifetime=False)
        except Exception as exc:
            self.close()
            raise _error(exc, "paper_snapshot_unconfirmed") from None

    def _contract(self, occ_symbol):
        occ = _parse_occ(occ_symbol)
        if occ_symbol not in self._qualified:
            self._qualified[occ_symbol] = self._qualify(occ)
        return self._qualified[occ_symbol]

    def _grid(self, detail):
        exchanges = detail.validExchanges.split(",") if isinstance(detail.validExchanges, str) else []
        ids = detail.marketRuleIds.split(",") if isinstance(detail.marketRuleIds, str) else []
        if not 1 <= len(exchanges) <= 100 or len(exchanges) != len(ids):
            raise PaperClientError("paper_market_rule_unconfirmed")
        matching = [value.strip() for exchange, value in zip(exchanges, ids) if exchange.strip() == "SMART"]
        if len(matching) != 1 or not re.fullmatch(r"[1-9][0-9]{0,8}", matching[0]):
            raise PaperClientError("paper_market_rule_unconfirmed")
        rule_id = int(matching[0])
        minimum = _decimal(detail.minTick, "market_rule_unconfirmed", maximum="100")
        key = (rule_id, str(minimum))
        if key not in self._grids:
            rules = self._run(self._ib.reqMarketRuleAsync(rule_id), "paper_market_rule_unavailable")
            if not isinstance(rules, list) or not 1 <= len(rules) <= 200:
                raise PaperClientError("paper_market_rule_unconfirmed")
            levels = [(_decimal(r.lowEdge, "market_rule_unconfirmed", zero=True),
                       _decimal(r.increment, "market_rule_unconfirmed", maximum="100")) for r in rules]
            if (levels != sorted(levels) or levels[0][0] != 0 or len({p[0] for p in levels}) != len(levels)
                    or any(increment < minimum or edge % increment != 0 for edge, increment in levels)):
                raise PaperClientError("paper_market_rule_unconfirmed")
            self._grids[key] = levels
        return self._grids[key], rule_id

    def _floor(self, detail, price):
        levels, rule_id = self._grid(detail)
        candidates = []
        for index, (edge, increment) in enumerate(levels):
            if edge > price:
                break
            upper = levels[index+1][0] if index+1 < len(levels) else None
            candidate = (min(price, upper) / increment if upper is not None else price / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
            if upper is not None and candidate >= upper:
                candidate -= increment
            if edge <= candidate <= price and candidate > 0:
                candidates.append(candidate)
        if not candidates:
            raise PaperClientError("paper_tick_price_unconfirmed")
        return max(candidates), rule_id

    @staticmethod
    def _market_open(detail, server_time):
        # Regular US equity-option session only. Contract liquidHours supplies
        # holiday/early-close evidence; a weekday clock alone is insufficient.
        zone_name = getattr(detail, "timeZoneId", None)
        hours = getattr(detail, "liquidHours", None)
        if zone_name not in {"US/Eastern", "America/New_York", "EST5EDT"} or not isinstance(hours, str) or len(hours) > 4096:
            return None
        zone = ZoneInfo("America/New_York")
        local = server_time.astimezone(zone)
        if local.weekday() >= 5 or not day_time(9, 30) <= local.time().replace(tzinfo=None) < day_time(16):
            return False
        date_text = local.strftime("%Y%m%d")
        entries = [entry for entry in hours.split(";") if entry.startswith(date_text + ":")]
        if len(entries) != 1:
            return None
        body = entries[0].split(":", 1)[1]
        if body == "CLOSED":
            return False
        for interval in body.split(","):
            match = re.fullmatch(r"([0-9]{4})-(?:(20[0-9]{6}):)?([0-9]{4})", interval)
            if not match:
                return None
            end_date = match[2] or date_text
            try:
                begin = datetime.strptime(date_text + match[1], "%Y%m%d%H%M").replace(tzinfo=zone)
                end = datetime.strptime(end_date + match[3], "%Y%m%d%H%M").replace(tzinfo=zone)
            except ValueError:
                return None
            if begin <= local < end:
                return True
        return False

    def quote_option(self, occ_symbol, reference_price=None):
        self._guard(bound=True)
        try:
            contract, detail, info = self._contract(occ_symbol)
            self._grid(detail)
            cap = rule_id = None
            if reference_price is not None:
                reference = _decimal(reference_price, "invalid_reference_price", maximum="100000")
                cap, rule_id = self._floor(detail, reference * Decimal("1.03"))
            server_time = self._server_time()
            quote = self._quote(contract)
            sell = self._floor(detail, Decimal(str(quote["bid_usd"])))[0] if quote["realtime_verified"] else None
            self._guard(bound=True)
            return dict(_FLAGS, contract=info, quote=quote, server_time_utc=server_time.isoformat(),
                        market_open=self._market_open(detail, server_time),
                        tick_rule_verified=True,
                        tick_aligned_price_cap_usd=float(cap) if cap is not None else None,
                        sell_limit_price_usd=float(sell) if sell is not None else None, market_rule_id=rule_id)
        except Exception as exc:
            raise _error(exc, "paper_quote_unavailable") from None

    def reserve_order_identity(self, order_ref, minimum_order_id=None):
        self._guard(bound=True, write=True)
        if not isinstance(order_ref, str) or not _REF.fullmatch(order_ref):
            raise PaperClientError("paper_order_reference_invalid")
        if (self._last_snapshot_monotonic is None or time.monotonic()-self._last_snapshot_monotonic > 30
                or order_ref in self._attempted_refs or order_ref in self._reserved.values()
                or any(o["order_ref"] == order_ref for o in self._last_orders.values())):
            raise PaperClientError("paper_order_identity_unavailable")
        if minimum_order_id is not None:
            self._ib.client.updateReqId(_integer(minimum_order_id, positive=True))
        order_id = self._ib.client.getReqId()
        if type(order_id) is not int or order_id <= 0 or (self.client_id, order_id) in self._last_orders:
            raise PaperClientError("paper_order_identity_unavailable")
        self._reserved[order_id] = order_ref
        return {"client_id": self.client_id, "order_id": order_id, "order_ref": order_ref}

    def _unthrottled_write(self):
        # SDK writes can otherwise queue and transmit after freshness guards.
        # No event-loop yield occurs between this check and place/cancel below.
        client = self._ib.client
        if client._msgQ or client._isThrottling or client.MaxRequests <= 0:
            raise PaperClientError("paper_send_queue_busy")
        current = self._loop.time()
        active = sum(current-value <= client.RequestsInterval for value in client._timeQ)
        if active >= client.MaxRequests:
            raise PaperClientError("paper_send_queue_busy")

    def _quote_fresh(self, quote):
        age = quote.get("max_bid_ask_local_age_seconds")
        elapsed = (datetime.now(timezone.utc)-_stamp(quote.get("observed_utc"))).total_seconds()
        if (quote.get("realtime_verified") is not True or quote.get("market_data_type") != 1
                or type(age) not in (int, float) or elapsed < 0 or age+elapsed > self.max_quote_age_seconds):
            raise PaperClientError("paper_realtime_quote_required")

    def submit_limit(self, *, order_id, order_ref, occ_symbol, action, quantity, limit_price,
                     signal_created_utc, reference_price=None, before_send: Callable[[], bool]):
        attempted = False
        try:
            self._guard(bound=True, write=True)
            if (type(order_id) is not int or self._reserved.get(order_id) != order_ref
                    or order_id in self._attempted_ids or order_ref in self._attempted_refs):
                raise PaperClientError("paper_order_identity_not_reserved")
            if action not in {"BUY", "SELL"} or type(quantity) is not int or not 1 <= quantity <= 50:
                raise PaperClientError("paper_order_fields_invalid")
            price = _decimal(limit_price, "invalid_reference_price", maximum="100000")
            contract, detail, _info = self._contract(occ_symbol)
            grid_price, _rule_id = self._floor(detail, price)
            if grid_price != price:
                raise PaperClientError("paper_order_tick_invalid")
            if action == "BUY":
                reference = _decimal(reference_price, "invalid_reference_price", maximum="100000")
                cap, _ = self._floor(detail, reference * Decimal("1.03"))
                if price != cap or not Decimal("3000") <= price*quantity*100+2 <= Decimal("5000"):
                    raise PaperClientError("paper_entry_price_or_budget_invalid")
            created = _stamp(signal_created_utc)
            if not callable(before_send) or before_send() is not True:
                raise PaperClientError("paper_final_source_check_failed")
            if action == "SELL":
                positions = self._run(self._ib.reqPositionsAsync(), "paper_exit_position_unavailable")
                active = self._run(self._ib.reqAllOpenOrdersAsync(), "paper_exit_orders_unavailable")
                quantities = [_finite(p.position) for p in positions
                              if p.account == self._account and p.contract.conId == contract.conId]
                if len(quantities) != 1 or quantities[0] is None or quantities[0] < quantity:
                    raise PaperClientError("paper_exit_exceeds_broker_long_position")
                other_sells = Decimal("0")
                for pending in active:
                    if (pending.order.account == self._account and pending.contract.conId == contract.conId
                            and pending.order.action == "SELL"):
                        unfilled = _finite(pending.orderStatus.remaining)
                        if unfilled is None or unfilled < 0:
                            raise PaperClientError("paper_exit_orders_unconfirmed")
                        other_sells += Decimal(str(unfilled))
                if Decimal(str(quantities[0])) - other_sells < quantity:
                    raise PaperClientError("paper_exit_quantity_already_reserved")
            # The final broker quote follows the potentially slow SSH source
            # check, so an old quote cannot survive that check as current data.
            server_time = self._server_time()
            server_time_received = time.monotonic()
            if self._market_open(detail, server_time) is not True:
                raise PaperClientError("paper_option_session_closed_or_unknown")
            quote = self._quote(contract)
            if action == "BUY":
                if quote.get("ask_usd") is None or Decimal(str(quote["ask_usd"])) > price:
                    raise PaperClientError("paper_buy_quote_above_limit")
            else:
                if quote.get("bid_usd") is None or price > Decimal(str(quote["bid_usd"])):
                    raise PaperClientError("paper_sell_quote_below_limit")
            self._quote_fresh(quote)
            age = (datetime.now(timezone.utc)-created).total_seconds()
            if not 0 <= age <= (30 if action == "BUY" else 120):
                raise PaperClientError("paper_signal_expired")
            estimated_server_now = server_time + timedelta(seconds=time.monotonic()-server_time_received)
            if (self._market_open(detail, estimated_server_now-timedelta(seconds=1)) is not True
                    or self._market_open(detail, estimated_server_now+timedelta(seconds=1)) is not True):
                raise PaperClientError("paper_option_session_boundary_unconfirmed")
            self._guard(bound=True, write=True)
            if any(o.order.orderId == order_id for o in self._ib.trades()):
                raise PaperClientError("paper_order_id_already_observed")
            if order_id <= max((o.order.orderId for o in self._ib.trades()), default=0):
                raise PaperClientError("paper_order_id_below_observed_order")
            order = self._order_type(action, quantity, float(price), orderId=order_id, clientId=self.client_id,
                                     orderRef=order_ref, account=self._account, tif="DAY", outsideRth=False,
                                     transmit=True, openClose="O" if action == "BUY" else "C")
            self._unthrottled_write()
            # Caller journal already reserves this identity; from here onward
            # every exception is unknown, never permission to resubmit.
            self._attempted_refs.add(order_ref)
            self._attempted_ids.add(order_id)
            attempted = True
            trade = self._ib.placeOrder(contract, order)
            return dict(_FLAGS, mutation_attempted=True, acknowledged=False,
                        status="submission_attempted_ack_pending", order=self._trade_fields(trade, broker_reported=False),
                        quote=quote, orders_submitted=True)
        except Exception as exc:
            raise _error(exc, "paper_submission_unknown" if attempted else "paper_submission_blocked", attempted=attempted) from None

    def lookup(self, order_ref, order_id):
        if not isinstance(order_ref, str) or not _REF.fullmatch(order_ref) or type(order_id) is not int or order_id <= 0:
            raise PaperClientError("paper_lookup_identity_invalid")
        snapshot = self.snapshot()
        matches = [o for o in snapshot["orders"] if o["client_id"] == self.client_id
                   and o["order_id"] == order_id and o["order_ref"] == order_ref]
        if len(matches) > 1:
            raise PaperClientError("paper_lookup_ambiguous")
        return {"found": len(matches) == 1, "order": matches[0] if matches else None,
                "absence_proves_no_order": False, "executions": [e for e in snapshot["executions"]
                    if e["client_id"] == self.client_id and e["order_id"] == order_id and e["order_ref"] == order_ref]}

    def cancel_order(self, *, order_ref, order_id):
        attempted = False
        try:
            self._guard(bound=True, write=True)
            lookup = self.lookup(order_ref, order_id)
            if not lookup["found"]:
                return dict(_FLAGS, mutation_attempted=False, acknowledged=False, status="order_unconfirmed",
                            absence_proves_no_order=False)
            if lookup["order"]["status"] in _DONE and lookup["order"]["broker_reported"]:
                return dict(_FLAGS, mutation_attempted=False, acknowledged=True, status="already_terminal", order=lookup["order"])
            matches = [trade for trade in self._ib.trades()
                       if trade.order.account == self._account and trade.order.clientId == self.client_id
                       and trade.order.orderId == order_id and trade.order.orderRef == order_ref]
            if len(matches) != 1:
                raise PaperClientError("paper_cancel_ownership_unconfirmed")
            self._guard(bound=True, write=True)
            self._unthrottled_write()
            attempted = True
            self._ib.cancelOrder(matches[0].order)
            return dict(_FLAGS, mutation_attempted=True, acknowledged=False, status="cancel_requested_ack_pending")
        except Exception as exc:
            raise _error(exc, "paper_cancel_unknown" if attempted else "paper_cancel_blocked", attempted=attempted) from None

    def close(self):
        try:
            super().close()
        finally:
            self._qualified.clear()
            self._grids.clear()
            self._reserved.clear()
            self._last_orders.clear()
            self._last_snapshot_monotonic = None
            self._order_type = self._execution_filter_type = None
