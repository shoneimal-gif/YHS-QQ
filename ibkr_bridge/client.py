"""Local TWS/IB Gateway read-only data client; never creates broker orders.

The connection uses ``readonly=True`` and a nonzero client ID. TWS's own
Read-Only API setting must ALSO remain enabled: the SDK flag is not proof that
the server enforces that setting. Live default ports identify the requested
endpoint only; they cannot prove an account is a live account. Explicit account
SHA256 binding is required before account data or option previews are exposed.

The synchronous interface is for the main thread with no running asyncio loop.
It lazily imports the pinned SDK in connect(). No credentials are accepted,
read, saved or printed. Public errors contain fixed categories only.

Upstream API references used for the pinned dependency:
https://ib-api-reloaded.github.io/ib_async/_modules/ib_async/ib.html
https://ib-api-reloaded.github.io/ib_async/_modules/ib_async/ticker.html
https://www.interactivebrokers.com/docs/tws-api/ref/contract-details
https://www.interactivebrokers.com/docs/tws-api/doc/orders/minimum-price-increment/introduction
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import importlib.metadata
import logging
import math
import re
import threading
import time
from typing import Any, Awaitable, Sequence


SDK_VERSION = "2.1.0"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT = re.compile(r"[A-Z][A-Z0-9]{2,31}\Z")
_OCC = re.compile(r"([A-Z]{1,6})([0-9]{6})([CP])([0-9]{8})\Z")
_TEXT = re.compile(r"[A-Z0-9. -]{1,32}\Z")
_ERRORS = frozenset({
    "ibkr_readonly_error", "local_live_endpoint_required", "invalid_client_id",
    "invalid_timeout", "invalid_account_allowlist", "main_thread_required",
    "running_asyncio_loop_unsupported", "session_already_connected",
    "session_not_connected", "sdk_missing", "sdk_version_mismatch",
    "sdk_initialization_failed", "tws_connection_failed", "managed_accounts_unconfirmed",
    "account_binding_required", "account_not_allowlisted", "account_not_managed",
    "account_rebinding_requires_reconnect",
    "account_snapshot_failed", "account_snapshot_unconfirmed", "positions_unconfirmed",
    "invalid_option_symbol", "expired_option", "invalid_reference_price",
    "price_tolerance_exceeds_three_percent", "contract_lookup_failed",
    "contract_ambiguous", "contract_unconfirmed", "market_rule_unconfirmed",
    "market_rule_lookup_failed", "quote_request_failed", "quote_cleanup_failed",
    "invalid_sleep_interval",
})
_FLAGS = {
    "broker": "ibkr", "readonly": True, "orders_submitted": False,
    "preview_only": True, "user_confirmation_required": True,
}
_ACCOUNT_TAGS = frozenset({
    "NetLiquidation", "TotalCashValue", "AvailableFunds", "BuyingPower",
    "SettledCash", "InitMarginReq", "MaintMarginReq", "ExcessLiquidity",
})


class _NoSDKLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


class IBKRReadOnlyError(ValueError):
    """A fixed public category; the original SDK exception is not exposed."""

    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "ibkr_readonly_error"
        self.category = self.code
        super().__init__(self.code)


def _decimal(value: Any, code: str, *, zero: bool = False,
             maximum: str = "1000000") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise IBKRReadOnlyError(code)
    text = str(value)
    if len(text) > 64:
        raise IBKRReadOnlyError(code)
    try:
        number = Decimal(text)
        if (not number.is_finite() or number < 0 or (not zero and number == 0)
                or number > Decimal(maximum) or number.as_tuple().exponent < -12):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, OverflowError):
        raise IBKRReadOnlyError(code) from None


def _finite(value: Any, *, positive: bool = False) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        number = float(value)
        if not math.isfinite(number) or abs(number) > 10**12 or (positive and number <= 0):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _account_info(account: str) -> dict[str, Any]:
    suffix_length = min(4, max(1, len(account) // 2))
    return {
        "account_masked": "****" + account[-suffix_length:],
        "account_sha256": hashlib.sha256(account.encode("ascii")).hexdigest(),
    }


def _parse_occ(value: Any) -> dict[str, Any]:
    match = _OCC.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise IBKRReadOnlyError("invalid_option_symbol")
    symbol, expiry, right, strike = match.groups()
    try:
        expiration = date(2000 + int(expiry[:2]), int(expiry[2:4]), int(expiry[4:6]))
    except ValueError:
        raise IBKRReadOnlyError("invalid_option_symbol") from None
    strike_value = Decimal(strike) / 1000
    if strike_value <= 0:
        raise IBKRReadOnlyError("invalid_option_symbol")
    if expiration < datetime.now(timezone.utc).date():
        raise IBKRReadOnlyError("expired_option")
    return {
        "occ_symbol": value, "symbol": symbol,
        "expiration": expiration.strftime("%Y%m%d"), "right": right,
        "strike": strike_value, "local_symbol": symbol.ljust(6) + expiry + right + strike,
    }


class ReadonlyIBKRClient:
    """Narrow read-only interface; the SDK object is never returned to callers.

    Empty account allowlists are useful only for connect() preflight. A caller
    must supply an exact permitted hash and bind_account(hash) for any account
    snapshot or market-data preview. No paper account is claimed or inferred.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 7496,
                 client_id: int = 17931,
                 account_sha256_allowlist: Sequence[str] = (),
                 request_timeout: float = 6.0, quote_timeout: float = 3.0,
                 max_quote_age_seconds: float = 2.0):
        # Literal loopback avoids DNS, remote forwarding and ambiguous hosts.
        if host != "127.0.0.1" or type(port) is not int or port not in (7496, 4001):
            raise IBKRReadOnlyError("local_live_endpoint_required")
        # Client ID 0 can bind manual TWS orders during SDK initialization.
        if type(client_id) is not int or not 1 <= client_id <= 2**31 - 1:
            raise IBKRReadOnlyError("invalid_client_id")
        for value in (request_timeout, quote_timeout, max_quote_age_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0.25 <= value <= 15):
                raise IBKRReadOnlyError("invalid_timeout")
        if (not isinstance(account_sha256_allowlist, (list, tuple))
                or len(account_sha256_allowlist) > 10
                or any(not isinstance(v, str) or _SHA.fullmatch(v) is None
                       for v in account_sha256_allowlist)
                or len(set(account_sha256_allowlist)) != len(account_sha256_allowlist)):
            raise IBKRReadOnlyError("invalid_account_allowlist")
        self.host, self.port, self.client_id = host, port, client_id
        self.request_timeout = float(request_timeout)
        self.quote_timeout = float(quote_timeout)
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self._allowlist = frozenset(account_sha256_allowlist)
        self._ib: Any = None
        self._option_type: Any = None
        self._accounts: tuple[str, ...] = ()
        self._account: str | None = None
        self._owner_thread: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owns_loop = False
        self._sdk_log_filter = _NoSDKLog()
        self._sdk_loggers: list[logging.Logger] = []
        self._sdk_parent_propagate: bool | None = None
        self._sdk_null_handler = logging.NullHandler()
        self._account_subscription_account: str | None = None
        self._account_subscription_started: float | None = None
        self._account_initial_download_utc: str | None = None
        self._account_receipts: dict[tuple[str, str], tuple[float, str]] = {}

    def _account_value_received(self, value: Any) -> None:
        # Receipt times are local, per field. They are not a broker valuation
        # timestamp. The subscription sends only changed values after its
        # initial download, at an interval the application cannot shorten.
        if (value.account != self._account or value.tag not in _ACCOUNT_TAGS
                or not isinstance(value.currency, str)
                or re.fullmatch(r"[A-Z]{3}|BASE", value.currency) is None
                or _finite(value.value) is None):
            return
        self._account_receipts[(value.tag, value.currency)] = (
            time.monotonic(), datetime.now(timezone.utc).isoformat())

    def _private_sdk_logging(self) -> None:
        # The SDK's normal position/error log records may contain full account
        # IDs. This dedicated client reports fixed errors itself. Filter SDK
        # logs even when its caller has enabled debug handlers, and restore
        # logging when the session closes. No account data is emitted here.
        parent = logging.getLogger("ib_async")
        self._sdk_parent_propagate = parent.propagate
        parent.propagate = False
        parent.addHandler(self._sdk_null_handler)
        names = {"ib_async", "ib_async.ib", "ib_async.client", "ib_async.wrapper",
                 "ib_async.decoder", "ib_async.connection"}
        names.update(name for name in logging.Logger.manager.loggerDict
                     if name.startswith("ib_async."))
        for name in names:
            logger = logging.getLogger(name)
            logger.addFilter(self._sdk_log_filter)
            self._sdk_loggers.append(logger)

    def _main_thread(self) -> None:
        if (threading.current_thread() is not threading.main_thread()
                or (self._owner_thread is not None
                    and self._owner_thread != threading.get_ident())):
            raise IBKRReadOnlyError("main_thread_required")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise IBKRReadOnlyError("running_asyncio_loop_unsupported")

    def _connected(self, *, bound: bool = False) -> None:
        self._main_thread()
        if self._ib is None or not self._ib.isConnected():
            raise IBKRReadOnlyError("session_not_connected")
        if bound and (self._account is None or self._account not in self._accounts):
            raise IBKRReadOnlyError("account_binding_required")

    def _run(self, awaitable: Awaitable[Any], code: str) -> Any:
        try:
            return self._ib.run(asyncio.wait_for(awaitable, self.request_timeout))
        except Exception:
            # A timed-out request can retain SDK futures/subscriptions. Discard
            # this whole connection before any later request can reuse state.
            self.close()
            raise IBKRReadOnlyError(code) from None

    def connect(self) -> dict[str, Any]:
        """Connect for preflight; return masked accounts and exact SHA256 hashes."""
        self._main_thread()
        if self._ib is not None:
            raise IBKRReadOnlyError("session_already_connected")
        try:
            if importlib.metadata.version("ib_async") != SDK_VERSION:
                raise IBKRReadOnlyError("sdk_version_mismatch")
        except importlib.metadata.PackageNotFoundError:
            raise IBKRReadOnlyError("sdk_missing") from None
        try:
            # Importing source-only readers never reaches the third-party SDK.
            from ib_async import IB, Option, StartupFetchNONE
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
            self._private_sdk_logging()
            self._ib.accountValueEvent += self._account_value_received
            self._ib.RequestTimeout = self.request_timeout
            self._ib.RaiseRequestErrors = True
        except Exception:
            self.close()
            raise IBKRReadOnlyError("sdk_initialization_failed") from None
        try:
            self._ib.connect(
                self.host, self.port, clientId=self.client_id,
                timeout=self.request_timeout, readonly=True, raiseSyncErrors=True,
                fetchFields=StartupFetchNONE,
            )
            accounts = self._ib.managedAccounts()
            if (not isinstance(accounts, list) or not 1 <= len(accounts) <= 100
                    or any(not isinstance(a, str) or _ACCOUNT.fullmatch(a) is None for a in accounts)
                    or len(accounts) != len(set(accounts))):
                raise IBKRReadOnlyError("managed_accounts_unconfirmed")
            self._accounts = tuple(accounts)
        except IBKRReadOnlyError:
            self.close()
            raise
        except Exception:
            self.close()
            raise IBKRReadOnlyError("tws_connection_failed") from None
        return dict(_FLAGS, connected=True, requested_endpoint="local_live_default_port",
                    account_environment_verified=False, tws_readonly_setting_verified=False,
                    managed_accounts=[_account_info(a) for a in self._accounts],
                    account_bound=False, sdk_version=SDK_VERSION)

    def bind_account(self, account_sha256: str) -> dict[str, Any]:
        """Bind one managed account by its exact preflight hash, with no guessing."""
        self._connected()
        if not isinstance(account_sha256, str) or account_sha256 not in self._allowlist:
            raise IBKRReadOnlyError("account_not_allowlisted")
        matches = [a for a in self._accounts
                   if _account_info(a)["account_sha256"] == account_sha256]
        if len(matches) != 1:
            raise IBKRReadOnlyError("account_not_managed")
        if self._account is not None and self._account != matches[0]:
            raise IBKRReadOnlyError("account_rebinding_requires_reconnect")
        self._account = matches[0]
        return dict(_FLAGS, account_bound=True, **_account_info(self._account))

    def account_snapshot(self) -> dict[str, Any]:
        """Read selected-account subscription values and a positions snapshot.

        Account values are not newly queried at each call. IBKR sends the full
        account download once, then updates only changed fields (up to three
        minutes between changes). Calling this method more often cannot make
        unchanged balance values carry a new server timestamp.
        """
        self._connected(bound=True)
        first_download_this_call = self._account_subscription_account is None
        if first_download_this_call:
            started = time.monotonic()
            self._run(self._ib.reqAccountUpdatesAsync(self._account), "account_snapshot_failed")
            self._account_subscription_account = self._account
            self._account_subscription_started = started
            self._account_initial_download_utc = datetime.now(timezone.utc).isoformat()
        elif self._account_subscription_account != self._account:
            raise IBKRReadOnlyError("account_binding_required")
        values = self._ib.accountValues(self._account)
        positions = self._run(self._ib.reqPositionsAsync(), "account_snapshot_failed")
        positions_completed_utc = datetime.now(timezone.utc).isoformat()
        now = time.monotonic()
        if not isinstance(values, list) or len(values) > 10000:
            raise IBKRReadOnlyError("account_snapshot_unconfirmed")
        if not isinstance(positions, list) or len(positions) > 5000:
            raise IBKRReadOnlyError("positions_unconfirmed")
        summary = []
        for value in values:
            if value.account != self._account or value.tag not in _ACCOUNT_TAGS:
                continue
            currency = value.currency
            number = _finite(value.value)
            if (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}|BASE", currency) is None
                    or number is None):
                continue
            receipt = self._account_receipts.get((value.tag, currency))
            summary.append({
                "tag": value.tag, "currency": currency, "value": number,
                "last_received_utc": receipt[1] if receipt is not None else None,
                "local_receipt_age_seconds": round(now - receipt[0], 6) if receipt is not None else None,
                "server_value_timestamp_verified": False,
            })
        result = []
        for position in positions:
            if position.account != self._account:
                continue
            contract = position.contract
            quantity, average_cost = _finite(position.position), _finite(position.avgCost)
            if quantity is None or average_cost is None:
                raise IBKRReadOnlyError("positions_unconfirmed")
            sec_type = str(contract.secType)
            symbol, currency = str(contract.symbol), str(contract.currency)
            result.append({
                "con_id": contract.conId if type(contract.conId) is int and contract.conId > 0 else None,
                "symbol": symbol if _TEXT.fullmatch(symbol) else None,
                "security_type": sec_type if sec_type in {"STK", "OPT", "FUT", "FOP", "CASH", "BOND", "BAG", "IND", "CFD", "FUND"} else "OTHER",
                "currency": currency if re.fullmatch(r"[A-Z]{3}", currency) else None,
                "quantity": quantity, "average_cost_broker_units": average_cost,
                "source": "ibkr_position_snapshot", "managed_by_bridge": False,
            })
        latest = max(self._account_receipts.values(), default=None, key=lambda item: item[0])
        return dict(_FLAGS, **_account_info(self._account),
                    observed_utc=datetime.now(timezone.utc).isoformat(),
                    summary=summary, positions=result,
                    account_values_source="read_only_account_subscription",
                    account_values_are_new_server_snapshot=False,
                    account_values_first_download_this_call=first_download_this_call,
                    account_subscription_initial_download_utc=self._account_initial_download_utc,
                    account_subscription_age_seconds=round(now - self._account_subscription_started, 6),
                    account_values_last_received_utc=latest[1] if latest is not None else None,
                    account_values_last_receipt_age_seconds=round(now - latest[0], 6) if latest is not None else None,
                    account_values_freshness_basis="per_field_local_subscription_receipt",
                    account_values_changed_update_interval_seconds=180,
                    account_values_server_timestamp_verified=False,
                    positions_snapshot_completed_utc=positions_completed_utc,
                    position_ownership_inferred=False)

    def _qualify(self, occ: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
        contract = self._option_type(
            symbol=occ["symbol"], lastTradeDateOrContractMonth=occ["expiration"],
            strike=float(occ["strike"]), right=occ["right"], exchange="SMART",
            multiplier="100", currency="USD", localSymbol=occ["local_symbol"],
            tradingClass=occ["symbol"],
        )
        details = self._run(self._ib.reqContractDetailsAsync(contract), "contract_lookup_failed")
        if not isinstance(details, list) or len(details) != 1:
            raise IBKRReadOnlyError("contract_ambiguous")
        detail = details[0]
        qualified = detail.contract
        try:
            strike = _decimal(qualified.strike, "contract_unconfirmed")
            multiplier = _decimal(qualified.multiplier, "contract_unconfirmed")
            min_tick = _decimal(detail.minTick, "contract_unconfirmed", maximum="100")
            local_symbol = qualified.localSymbol
            valid = (
                type(qualified.conId) is int and qualified.conId > 0
                and qualified.secType == "OPT" and qualified.currency == "USD"
                and multiplier == 100 and strike == occ["strike"]
                and qualified.symbol == occ["symbol"] and qualified.right == occ["right"]
                and qualified.lastTradeDateOrContractMonth == occ["expiration"]
                and qualified.tradingClass == occ["symbol"]
                and isinstance(local_symbol, str) and len(local_symbol) <= 32
                and local_symbol.replace(" ", "") == occ["occ_symbol"]
                and qualified.exchange == "SMART"
            )
        except (AttributeError, TypeError, ValueError):
            raise IBKRReadOnlyError("contract_unconfirmed") from None
        if not valid:
            raise IBKRReadOnlyError("contract_unconfirmed")
        info = {
            "occ_symbol": occ["occ_symbol"], "con_id": qualified.conId,
            "symbol": occ["symbol"], "expiration": occ["expiration"],
            "right": occ["right"], "strike": float(strike), "multiplier": 100,
            "currency": "USD", "exchange": "SMART", "trading_class": occ["symbol"],
            "min_tick": float(min_tick), "exact_contract_verified": True,
        }
        return qualified, detail, info

    def _tick_floor(self, detail: Any, cap: Decimal) -> tuple[Decimal, int]:
        exchanges = detail.validExchanges.split(",") if isinstance(detail.validExchanges, str) else []
        ids = detail.marketRuleIds.split(",") if isinstance(detail.marketRuleIds, str) else []
        if not 1 <= len(exchanges) <= 100 or len(exchanges) != len(ids):
            raise IBKRReadOnlyError("market_rule_unconfirmed")
        matches = [value.strip() for exchange, value in zip(exchanges, ids) if exchange.strip() == "SMART"]
        if len(matches) != 1 or re.fullmatch(r"[1-9][0-9]{0,8}", matches[0]) is None:
            raise IBKRReadOnlyError("market_rule_unconfirmed")
        rule_id = int(matches[0])
        rules = self._run(self._ib.reqMarketRuleAsync(rule_id), "market_rule_lookup_failed")
        if not isinstance(rules, list) or not 1 <= len(rules) <= 200:
            raise IBKRReadOnlyError("market_rule_unconfirmed")
        levels = []
        minimum = _decimal(detail.minTick, "market_rule_unconfirmed", maximum="100")
        for rule in rules:
            edge = _decimal(rule.lowEdge, "market_rule_unconfirmed", zero=True)
            increment = _decimal(rule.increment, "market_rule_unconfirmed", maximum="100")
            if increment < minimum or edge % increment != 0:
                raise IBKRReadOnlyError("market_rule_unconfirmed")
            levels.append((edge, increment))
        if levels != sorted(levels) or levels[0][0] != 0 or len({v[0] for v in levels}) != len(levels):
            raise IBKRReadOnlyError("market_rule_unconfirmed")
        candidates = []
        for index, (edge, increment) in enumerate(levels):
            if edge > cap:
                break
            upper = levels[index + 1][0] if index + 1 < len(levels) else None
            ceiling = min(cap, upper) if upper is not None else cap
            candidate = (ceiling / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
            if upper is not None and candidate >= upper:
                candidate -= increment
            if edge <= candidate <= cap and candidate > 0:
                candidates.append(candidate)
        if not candidates:
            raise IBKRReadOnlyError("market_rule_unconfirmed")
        return max(candidates), rule_id

    def _quote(self, contract: Any) -> dict[str, Any]:
        """Bound one streaming subscription; do not reuse cached Ticker prices."""
        ticker = None
        request_id: int | None = None
        ticks_received: dict[int, float] = {}
        delayed_tick_seen = False
        started = time.monotonic()

        def received(updated: Any) -> None:
            nonlocal delayed_tick_seen
            for tick in updated.ticks:
                if tick.tickType in (66, 67):
                    delayed_tick_seen = True
                if tick.tickType in (1, 2):
                    ticks_received[tick.tickType] = time.monotonic()

        def state() -> dict[str, Any]:
            now = time.monotonic()
            bid, ask = _finite(ticker.bid, positive=True), _finite(ticker.ask, positive=True)
            bid_size = _finite(ticker.bidSize, positive=True)
            ask_size = _finite(ticker.askSize, positive=True)
            data_type = ticker.marketDataType
            data_type = data_type if type(data_type) is int and data_type in (0, 1, 2, 3, 4) else 0
            fresh = (all(key in ticks_received and now - ticks_received[key] <= self.max_quote_age_seconds
                         for key in (1, 2)))
            local_age = (max(now - ticks_received[key] for key in (1, 2))
                         if all(key in ticks_received for key in (1, 2)) else None)
            live = data_type == 1 and not delayed_tick_seen and fresh
            complete = bid is not None and ask is not None and bid_size is not None and ask_size is not None and bid <= ask
            status = ("realtime_bid_ask_observed" if live and complete else
                      "non_realtime_data" if data_type in (2, 3, 4) or delayed_tick_seen else
                      "bid_ask_unavailable_or_stale")
            # Unverified values are deliberately not presented as current quotes.
            return {
                "bid_usd": bid if live and complete else None,
                "ask_usd": ask if live and complete else None,
                "bid_size": bid_size if live and complete else None,
                "ask_size": ask_size if live and complete else None,
                "market_data_type": data_type, "realtime_verified": live and complete,
                "status": status, "freshness_basis": "local_bid_ask_tick_receipt",
                "exchange_timestamp_verified": False,
                "max_local_quote_age_seconds": self.max_quote_age_seconds,
                "max_bid_ask_local_age_seconds": round(local_age, 6) if local_age is not None else None,
                "observed_utc": datetime.now(timezone.utc).isoformat(),
                "request_elapsed_seconds": round(now - started, 4),
            }

        try:
            self._ib.reqMarketDataType(1)
            ticker = self._ib.reqMktData(contract, "", False, False, [])
            request_id = self._ib.wrapper.ticker2ReqId["mktData"].get(ticker)
            if type(request_id) is not int or request_id <= 0:
                raise IBKRReadOnlyError("quote_request_failed")
            # The pinned SDK's default marketDataType is 1. Set it to unknown
            # until the actual broker callback arrives for this subscription.
            ticker.marketDataType = 0
            ticker.bid = ticker.ask = ticker.bidSize = ticker.askSize = float("nan")
            ticker.updateEvent += received
            while time.monotonic() - started < self.quote_timeout:
                if not self._ib.isConnected():
                    raise IBKRReadOnlyError("quote_request_failed")
                self._ib.sleep(min(0.05, max(0.001, self.quote_timeout - (time.monotonic() - started))))
                result = state()
                if result["realtime_verified"] or result["status"] == "non_realtime_data":
                    return result
            return state()
        except Exception:
            self.close()
            raise IBKRReadOnlyError("quote_request_failed") from None
        finally:
            if ticker is not None:
                ticker.updateEvent -= received
                if self._ib is not None and self._ib.isConnected():
                    try:
                        if not self._ib.cancelMktData(contract):  # Only a data subscription.
                            raise IBKRReadOnlyError("quote_cleanup_failed")
                        # ib_async 2.1.0 retains its conId-keyed Ticker and the
                        # canceled request's reqId2Ticker entry. Retire these
                        # local data-cache entries, so a later request for the
                        # same conId cannot accept delayed callbacks from an
                        # older canceled request as a new quote.
                        wrapper = self._ib.wrapper
                        if wrapper.reqId2Ticker.get(request_id) is ticker:
                            wrapper.reqId2Ticker.pop(request_id)
                        if wrapper.tickers.get(hash(contract)) is ticker:
                            wrapper.tickers.pop(hash(contract))
                        wrapper.pendingTickers.discard(ticker)
                    except Exception:
                        self.close()
                        raise IBKRReadOnlyError("quote_cleanup_failed") from None

    def observe_option(self, occ_symbol: str) -> dict[str, Any]:
        """Observe an exact option without inferring an Entry or exit policy."""
        self._connected(bound=True)
        occ = _parse_occ(occ_symbol)
        contract, _detail, info = self._qualify(occ)
        quote = self._quote(contract)
        return dict(_FLAGS, **_account_info(self._account), quantity=None,
                    contract=info, quote=quote, execution_authorized=False,
                    position_ownership_inferred=False)

    def preview_option(self, occ_symbol: str, reference_price: Any, *,
                       max_price_increase_pct: Any = "3") -> dict[str, Any]:
        """Observe a standard option and Entry-based price cap, with no quantity.

        This price comparison does not establish signal validity, suitable size,
        buying power, ownership, an execution decision or a ready-to-send order.
        Callers must not treat a CLOSE source's Exit as an Entry reference.
        """
        self._connected(bound=True)
        occ = _parse_occ(occ_symbol)
        reference = _decimal(reference_price, "invalid_reference_price", maximum="100000")
        tolerance = _decimal(max_price_increase_pct, "price_tolerance_exceeds_three_percent",
                             zero=True, maximum="3")
        raw_cap = reference * (1 + tolerance / 100)
        contract, detail, info = self._qualify(occ)
        tick_cap, rule_id = self._tick_floor(detail, raw_cap)
        quote = self._quote(contract)
        within = (Decimal(str(quote["ask_usd"])) <= tick_cap) if quote["realtime_verified"] else None
        return dict(_FLAGS, **_account_info(self._account), quantity=None,
                    contract=info, quote=quote, reference_price_usd=float(reference),
                    max_price_increase_pct=float(tolerance), raw_price_cap_usd=float(raw_cap),
                    tick_aligned_price_cap_usd=float(tick_cap), market_rule_id=rule_id,
                    tick_rule_verified=True, price_within_tolerance=within,
                    execution_authorized=False, position_ownership_inferred=False)

    def sleep(self, seconds: float) -> None:
        """Let the SDK process socket messages while the main loop waits."""
        self._connected()
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 <= seconds <= 15):
            raise IBKRReadOnlyError("invalid_sleep_interval")
        self._ib.sleep(float(seconds))

    def close(self) -> None:
        """Disconnect; never sends an order-management request."""
        self._main_thread()
        if self._ib is not None:
            try:
                self._ib.accountValueEvent -= self._account_value_received
            except Exception:
                pass
            try:
                self._ib.disconnect()
            except Exception:
                pass
        self._ib = self._option_type = None
        self._account, self._accounts = None, ()
        self._account_subscription_account = self._account_subscription_started = None
        self._account_initial_download_utc = None
        self._account_receipts.clear()
        for logger in self._sdk_loggers:
            logger.removeFilter(self._sdk_log_filter)
        self._sdk_loggers.clear()
        if self._sdk_parent_propagate is not None:
            parent = logging.getLogger("ib_async")
            parent.propagate = self._sdk_parent_propagate
            parent.removeHandler(self._sdk_null_handler)
            self._sdk_parent_propagate = None
        if self._owns_loop and self._loop is not None:
            try:
                self._loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)
        self._loop, self._owns_loop, self._owner_thread = None, False, None
