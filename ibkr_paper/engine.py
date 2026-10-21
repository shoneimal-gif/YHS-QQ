"""Dennis AlphaAgent -> independently journaled IBKR simulated option orders.

The injected broker must enforce a DU Paper account and a local Paper-only
endpoint. This engine never imports an Alpaca executor or a live-order client.
Confirmed broker executions alone create ownership; source messages and local
PendingSubmit records are never fill or acknowledgement evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
from pathlib import Path
import re
from typing import Any

from .common import PaperError, config_identity, now, timestamp
from .ledger import LedgerError, PaperLedger, REF, TERMINAL, decimal, source_identity, whole


_PENDING = {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
_UNRESOLVED = {"INTENT", "UNKNOWN", "PENDING", "SETTLING"}
_DEFER = {"paper_broker_warmup", "paper_source_recheck_unconfirmed", "paper_source_unhealthy",
          "paper_source_pending", "paper_quote_unavailable", "paper_market_not_open",
          "paper_entry_waiting_for_cancellation", "paper_position_quantity_unconfirmed"}


def _error(exc: BaseException) -> str:
    value = getattr(exc, "code", getattr(exc, "category", "paper_operation_unavailable"))
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else "paper_operation_unavailable"


def _equal_number(a, b) -> bool:
    return decimal(a, zero=True) == decimal(b, zero=True)


class PaperEngine:
    """One local process; caller must hold the executor lock for its lifetime."""

    def __init__(self, config: dict, client: Any, source: Any, *, runtime: Path, notifier=None):
        if (config.get("mode") != "ibkr_paper" or config.get("paper_only") is not True
                or config.get("execute_paper") is not True or config.get("paper_login_confirmed") is not True
                or config["ibkr"].get("host") != "127.0.0.1" or config["ibkr"].get("port") not in (4002, 7497)):
            raise PaperError("paper_execution_authorization_required")
        self.config, self.strategy, self.client, self.source = config, config["strategy"], client, source
        self.notifier, self.started = notifier, now()
        self.ledger = PaperLedger(Path(runtime) / "ledger.sqlite3",
            account_sha256=config["ibkr"]["account_sha256"], client_id=config["ibkr"]["client_id"],
            config_binding=config_identity(config))
        self.healthy_polls = 0
        self.last_snapshot = None
        self.last_source = None
        self.entry_blocks = []
        self.last_error = None
        # An INTENT with no durable write-attempt marker cannot have called the
        # transport. It is retired, never resent or re-armed after a restart.
        for order in self.ledger.state["orders"].values():
            if order["phase"] == "INTENT" and not order["write_attempted"]:
                order["phase"] = "NOT_SENT"
                order["last_error"] = "paper_interrupted_before_write"
        self.ledger.save()

    def close(self):
        self.ledger.close()

    def _snapshot(self) -> dict:
        self.client.register_known_orders([o for o in self.ledger.state["orders"].values()
                                           if o.get("perm_id", 0) and o["perm_id"] > 0 and o["write_attempted"]])
        snapshot = self.client.snapshot()
        if (not isinstance(snapshot, dict) or snapshot.get("complete") is not True
                or snapshot.get("paper_verified") is not True
                or snapshot.get("account_sha256") != self.config["ibkr"]["account_sha256"]
                or any(not isinstance(snapshot.get(k), list) for k in ("orders", "positions", "executions"))):
            raise PaperError("paper_broker_snapshot_unconfirmed")
        if not -5 <= (now() - timestamp(snapshot["observed_utc"])).total_seconds() <= 15:
            raise PaperError("paper_broker_snapshot_stale")
        self._reconcile(snapshot)
        self.last_snapshot = snapshot
        self.healthy_polls += 1
        return snapshot

    def _order_match(self, order: dict, broker: dict) -> bool:
        return (broker.get("order_ref") == order["order_ref"]
                and broker.get("order_id") == order["order_id"]
                and broker.get("client_id") == order["client_id"]
                and broker.get("con_id") == order["con_id"]
                and broker.get("occ_symbol") == order["occ_symbol"]
                and broker.get("action") == order["action"]
                and whole(broker.get("quantity")) == order["quantity"]
                and _equal_number(broker.get("limit_price"), order["limit_price"])
                and (not order.get("perm_id") or broker.get("perm_id") == order["perm_id"]))

    def _reconcile(self, snapshot: dict):
        state = self.ledger.state
        orders = state["orders"]
        indexed = {}
        foreign_orders = False
        for item in snapshot["orders"]:
            ref = item.get("order_ref")
            if ref in orders:
                order = orders[ref]
                if not self._order_match(order, item) or ref in indexed:
                    self.ledger.halt("paper_order_identity_mismatch")
                    raise LedgerError("paper_order_identity_mismatch")
                indexed[ref] = item
            elif item.get("status") not in TERMINAL:
                foreign_orders = True
        corrections = {key.rsplit(".", 1)[0]: key for key in state["fills"] if "." in key}
        new_fills = []
        for execution in snapshot["executions"]:
            ref, exec_id = execution.get("order_ref"), execution.get("exec_id")
            if ref not in orders:
                continue
            order = orders[ref]
            if (not isinstance(exec_id, str) or not 1 <= len(exec_id) <= 128
                    or execution.get("client_id") != order["client_id"]
                    or execution.get("order_id") != order["order_id"]
                    or execution.get("con_id") != order["con_id"]
                    or execution.get("occ_symbol") != order["occ_symbol"]
                    or execution.get("side") != ("BOT" if order["action"] == "BUY" else "SLD")
                    or (order.get("perm_id") and execution.get("perm_id") != order["perm_id"])):
                self.ledger.halt("paper_execution_identity_mismatch")
                raise LedgerError("paper_execution_identity_mismatch")
            qty, price = whole(execution["quantity"]), decimal(execution["price"])
            timestamp(execution["executed_utc"])
            if execution.get("pending_price_revision") is True:
                # IBKR explicitly marks this price as provisional. Do not
                # manufacture final-cost ownership from it.
                continue
            # Execution corrections require an explicit accounting migration.
            # Never count a corrected execution as an additional purchase.
            base = exec_id.rsplit(".", 1)[0] if "." in exec_id else None
            if base is not None and base in corrections and corrections[base] != exec_id:
                self.ledger.halt("paper_execution_correction_requires_review")
                raise LedgerError("paper_execution_correction_requires_review")
            canonical = {"exec_id": exec_id, "order_ref": ref, "quantity": qty,
                         "price": format(price, "f"), "executed_utc": execution["executed_utc"]}
            if exec_id in state["fills"]:
                if state["fills"][exec_id] != canonical:
                    self.ledger.halt("paper_execution_changed_requires_review")
                    raise LedgerError("paper_execution_changed_requires_review")
                continue
            if order["phase"] == "NOT_SENT" or order["filled_qty"] + qty > order["quantity"]:
                self.ledger.halt("paper_execution_exceeds_intent")
                raise LedgerError("paper_execution_exceeds_intent")
            if order["action"] == "BUY" and price > decimal(order["limit_price"]):
                self.ledger.halt("paper_execution_above_buy_limit")
                raise LedgerError("paper_execution_above_buy_limit")
            state["fills"][exec_id] = canonical
            if base is not None:
                corrections[base] = exec_id
            order["filled_qty"] += qty
            order["filled_notional_usd"] = format(decimal(order["filled_notional_usd"], zero=True) + price * qty * 100, "f")
            if type(execution.get("perm_id")) is int and execution["perm_id"] > 0:
                order["perm_id"] = execution["perm_id"]
            new_fills.append((order, canonical))
        for ref, order in orders.items():
            if order["phase"] == "NOT_SENT":
                continue
            broker = indexed.get(ref)
            if broker is None:
                # Neither reqAllOpenOrders nor completed-order history can prove
                # that a timed-out request was never accepted.
                if order["filled_qty"] == order["quantity"]:
                    order["phase"] = "TERMINAL"
                    order["broker_status"] = "EXECUTIONS_COMPLETE"
                    order["last_error"] = None
                elif order["phase"] != "TERMINAL":
                    order["phase"] = "UNKNOWN"
                    order["last_error"] = "paper_order_absent_not_proof_of_failure"
                continue
            if type(broker.get("perm_id")) is int and broker["perm_id"] > 0:
                order["perm_id"] = broker["perm_id"]
            filled, remaining = whole(broker.get("filled"), zero=True), whole(broker.get("remaining"), zero=True)
            if filled > order["quantity"] or remaining > order["quantity"] or filled + remaining > order["quantity"]:
                self.ledger.halt("paper_order_quantity_mismatch")
                raise LedgerError("paper_order_quantity_mismatch")
            status = broker.get("status")
            order["broker_status"] = status
            if order["filled_qty"] == order["quantity"]:
                # Exact, deduplicated broker executions cover the immutable
                # requested quantity. No quantity remains available to fill.
                order["phase"] = "TERMINAL"
                order["last_error"] = None
                continue
            if filled != order["filled_qty"]:
                order["phase"] = "SETTLING"
                order["terminal_observation"] = None
                order["last_error"] = "paper_fills_not_yet_reconciled"
                continue
            if status in TERMINAL and broker.get("broker_reported") is True:
                if status == "Filled" and filled != order["quantity"]:
                    self.ledger.halt("paper_filled_status_quantity_mismatch")
                    raise LedgerError("paper_filled_status_quantity_mismatch")
                evidence = f"{status}:{filled}"
                previous = order.get("terminal_observation")
                if (isinstance(previous, dict) and previous.get("evidence") == evidence
                        and (now() - timestamp(previous["first_utc"])).total_seconds() >= 1):
                    order["phase"] = "TERMINAL"
                    order["last_error"] = None
                    if not order.get("terminal_notified"):
                        self.ledger.notice("terminal:" + ref, "IBKR 模拟盘委托已核对终态",
                            {"合约": order["occ_symbol"], "方向": order["action"], "券商状态": status,
                             "已确认成交张数": filled, "委托张数": order["quantity"],
                             "委托编号": str(order["order_id"]), "说明": "终态与成交回报已核对；不代表全部成交"})
                        order["terminal_notified"] = True
                else:
                    order["phase"] = "SETTLING"
                    order["terminal_observation"] = {"evidence": evidence, "first_utc": now().isoformat()}
            elif status in _PENDING and broker.get("broker_reported") is True:
                order["phase"] = "PENDING"
                order["terminal_observation"] = None
                order["last_error"] = None
                if not order.get("ack_notified"):
                    self.ledger.notice("ack:" + ref, "IBKR 模拟盘委托已获券商回报",
                        {"合约": order["occ_symbol"], "方向": order["action"], "张数": order["quantity"],
                         "限价USD每份": order["limit_price"], "委托编号": str(order["order_id"]),
                         "说明": "券商已回报委托状态；尚不能据此确认成交"})
                    order["ack_notified"] = True
            else:
                order["phase"] = "UNKNOWN"
                order["last_error"] = "paper_broker_acknowledgement_unconfirmed"
        self.entry_blocks = []
        try:
            decimal(snapshot.get("available_funds_usd"), zero=True)
        except LedgerError:
            # An unavailable cash figure blocks purchases. Confirmed owned
            # exits do not require manufacturing a cash or buying-power value.
            self.entry_blocks.append("paper_available_funds_unconfirmed")
        if foreign_orders:
            self.entry_blocks.append("paper_foreign_or_unmanaged_orders")
        owned = {}
        for lot in state["lots"].values():
            entry = orders[lot["entry_ref"]]
            owned[entry["con_id"]] = owned.get(entry["con_id"], 0) + self.ledger.remaining(lot)
        positions = {}
        foreign_option = False
        for position in snapshot["positions"]:
            con_id, kind = position.get("con_id"), position.get("security_type")
            if kind != "OPT":
                continue
            try:
                raw_qty = Decimal(str(position.get("quantity")))
                qty = whole(raw_qty, zero=True) if raw_qty.is_finite() and raw_qty >= 0 else None
            except Exception:
                qty = None
            # Negative options cannot be adopted or offset by this long-only engine.
            if qty is None or con_id in positions:
                self.entry_blocks.append("paper_option_position_unconfirmed")
                continue
            positions[con_id] = qty
            if qty != owned.get(con_id, 0):
                foreign_option = True
        if foreign_option or any(positions.get(con_id, 0) != qty for con_id, qty in owned.items()):
            self.entry_blocks.append("paper_foreign_or_unreconciled_option_positions")
        for order, execution in new_fills:
            self.ledger.notice("fill:" + execution["exec_id"], "IBKR 模拟盘已确认成交",
                {"合约": order["occ_symbol"], "方向": "买入开仓" if order["action"] == "BUY" else "卖出平仓",
                 "本次成交张数": execution["quantity"], "成交价USD每份": execution["price"],
                 "成交时间UTC": execution["executed_utc"], "委托编号": str(order["order_id"]),
                 "成交依据": "IBKR Paper execution 回报"})
        self.ledger.save()

    def _source(self) -> dict:
        snapshot = self.source.snapshot()
        if (not isinstance(snapshot, dict) or snapshot.get("source_healthy") is not True
                or not isinstance(snapshot.get("events"), list)):
            raise PaperError("paper_source_unhealthy")
        if not -5 <= (now() - timestamp(snapshot["checked_utc"])).total_seconds() <= 20:
            raise PaperError("paper_source_unhealthy")
        self.last_source = snapshot
        for event in snapshot["events"]:
            key = source_identity(event["source_id"])
            record = self.ledger.state["sources"].setdefault(key, {
                "source_id": event["source_id"], "invalidated": False, "decision": None,
                "last_status": None, "event": None})
            if event["status"] == "invalidated":
                record["invalidated"] = True
                if record["decision"] == "intent_created":
                    self.ledger.notice("source-invalidated:" + event["source_id"], "IBKR 模拟盘原信号已修改或撤回",
                        {"源事件": event["source_id"], "处理": "核对并取消待成交委托；已成交数量仍以券商记录为准",
                         "说明": "原信号不再用于新委托；已有仓位继续等待有效平仓信号或人工处理"})
            record["last_status"] = event["status"]
            if event["verified"]:
                record["event"] = event
        self.ledger.save()
        return snapshot

    def _fresh(self, event: dict, *, new_intent=False):
        key = source_identity(event["source_id"])
        record = self.ledger.state["sources"].get(key)
        if (record is not None and record["invalidated"]) or event.get("status") == "invalidated":
            raise PaperError("paper_source_invalidated")
        if event.get("status") == "pending":
            raise PaperError("paper_source_pending")
        if event.get("verified") is not True or event.get("status") != "verified":
            raise PaperError("paper_source_unverified")
        if event.get("action") not in {"open", "close"}:
            raise PaperError("paper_source_action_unsupported")
        maximum = self.strategy["max_open_signal_age_seconds" if event["action"] == "open" else "max_close_signal_age_seconds"]
        created, received = timestamp(event["source_created_utc"]), timestamp(event["source_received_utc"])
        if (not 0 <= (now() - created).total_seconds() <= maximum
                or not -5 <= (now() - received).total_seconds() <= maximum):
            raise PaperError("paper_signal_expired")
        if created < timestamp(self.ledger.state["activated_at"]):
            raise PaperError("paper_source_before_activation")
        if new_intent and created < self.started:
            raise PaperError("paper_source_before_process_start")
        if not isinstance(event.get("tier"), str) or not re.fullmatch(r"T[1-9][0-9]{0,2}", event["tier"]):
            raise PaperError("paper_signal_tier_required")

    def _recheck(self, event: dict):
        snapshot = self._source()
        fields = ("revision", "action", "tier", "contract", "reference_price_decimal", "source_created_utc", "source_received_utc")
        current = next((e for e in snapshot["events"] if e["source_id"] == event["source_id"]), None)
        if current is None or any(current.get(k) != event.get(k) for k in fields):
            raise PaperError("paper_source_recheck_unconfirmed")
        self._fresh(current)

    def _entry_gate(self, snapshot: dict, *, exclude_ref=None):
        if self.healthy_polls < 2:
            raise PaperError("paper_broker_warmup")
        if self.ledger.state["halts"]:
            raise PaperError("paper_operator_review_required")
        if self.entry_blocks:
            raise PaperError(self.entry_blocks[0])
        if any(o["phase"] in {"UNKNOWN", "SETTLING"} and ref != exclude_ref
               for ref, o in self.ledger.state["orders"].items()):
            raise PaperError("paper_unknown_or_unsettled_order_blocks_entries")
        if self.ledger.used_usd() > decimal(self.strategy["max_option_exposure_usd"]):
            raise PaperError("paper_option_budget_exceeded")

    def _quote(self, event: dict):
        result = self.client.quote_option(event["contract"]["occ_symbol"],
            reference_price=event["reference_price_decimal"] if event["action"] == "open" else None)
        quote = result.get("quote", {})
        if result.get("market_open") is not True:
            raise PaperError("paper_market_not_open")
        if quote.get("realtime_verified") is not True:
            raise PaperError("paper_quote_unavailable")
        try:
            bid, ask = decimal(quote.get("bid_usd")), decimal(quote.get("ask_usd"))
        except LedgerError:
            raise PaperError("paper_quote_unavailable") from None
        if bid > ask:
            raise PaperError("paper_quote_unavailable")
        return result

    def _quantity(self, limit: Decimal, snapshot: dict) -> tuple[int, Decimal]:
        fee = decimal(self.strategy["fee_reserve_usd"], zero=True)
        maximum = min(decimal(self.strategy["max_debit_usd"]),
                      decimal(self.strategy["max_option_exposure_usd"]) - self.ledger.used_usd(),
                      decimal(snapshot["available_funds_usd"], zero=True))
        candidates = [(qty, limit * 100 * qty + fee)
                      for qty in range(1, self.strategy["max_contracts_per_trade"] + 1)
                      if decimal(self.strategy["min_debit_usd"]) <= limit * 100 * qty + fee <= maximum]
        if not candidates:
            raise PaperError("paper_whole_contract_budget_unavailable")
        target = decimal(self.strategy["target_debit_usd"])
        return min(candidates, key=lambda row: (abs(row[1] - target), row[1], row[0]))

    def _new_order(self, event: dict, *, action: str, quantity: int, limit: Decimal, con_id: int, lot: dict) -> dict:
        identity_key = source_identity(event["source_id"])
        ref = "AAIBP:" + identity_key[:32]
        if ref in self.ledger.state["orders"]:
            raise PaperError("paper_source_order_already_claimed")
        identity = self.client.reserve_order_identity(ref, minimum_order_id=max(1, self.ledger.max_order_id() + 1))
        if (identity.get("order_ref") != ref or identity.get("client_id") != self.config["ibkr"]["client_id"]
                or type(identity.get("order_id")) is not int or identity["order_id"] <= self.ledger.max_order_id()):
            raise PaperError("paper_order_identity_unconfirmed")
        order = dict(identity, source_id=event["source_id"], source_revision=event["revision"],
                     signal_created_utc=event["source_created_utc"], signal_received_utc=event["source_received_utc"],
                     source_event=event, lot_id=lot["lot_id"], occ_symbol=event["contract"]["occ_symbol"],
                     tier=event["tier"], con_id=con_id, action=action, quantity=quantity,
                     limit_price=format(limit, "f"), fee_reserve_usd=str(self.strategy["fee_reserve_usd"] if action == "BUY" else 0),
                     phase="INTENT", write_attempted=False, cancel_attempted=False,
                     filled_qty=0, filled_notional_usd="0", perm_id=None, broker_status=None,
                     terminal_observation=None, created_utc=now().isoformat(), submitted_utc=None,
                     last_error=None, ack_notified=False, terminal_notified=False)
        self.ledger.state["orders"][ref] = order
        if action == "BUY":
            lot["entry_ref"] = ref
        else:
            lot["exit_refs"].append(ref)
        self.ledger.state["sources"][identity_key]["decision"] = "intent_created"
        self.ledger.state["sources"][identity_key]["lot_id"] = lot["lot_id"]
        self.ledger.save()
        return order

    def _owned_position_gate(self, order: dict, snapshot: dict):
        lot = self.ledger.state["lots"][order["lot_id"]]
        if self.ledger.remaining(lot) != order["quantity"]:
            raise PaperError("paper_owned_quantity_changed")
        entry = self.ledger.state["orders"][lot["entry_ref"]]
        if entry["phase"] not in {"TERMINAL", "NOT_SENT"}:
            raise PaperError("paper_entry_waiting_for_cancellation")
        total_owned = sum(self.ledger.remaining(item) for item in self.ledger.state["lots"].values()
                          if self.ledger.state["orders"][item["entry_ref"]]["con_id"] == order["con_id"])
        positions = [p for p in snapshot["positions"] if p.get("con_id") == order["con_id"] and p.get("security_type") == "OPT"]
        try:
            confirmed_quantity = whole(positions[0]["quantity"], zero=True) if len(positions) == 1 else None
        except LedgerError:
            confirmed_quantity = None
        if confirmed_quantity != total_owned:
            raise PaperError("paper_position_quantity_unconfirmed")
        if any(p.get("con_id") == order["con_id"] and p.get("action") == "SELL"
               and p.get("status") not in TERMINAL and p.get("order_ref") != order["order_ref"]
               for p in snapshot["orders"]):
            raise PaperError("paper_contract_exit_quantity_already_reserved")
        if any(o["lot_id"] == lot["lot_id"] and o["action"] == "SELL" and o["phase"] in _UNRESOLVED
               and o["order_ref"] != order["order_ref"] for o in self.ledger.state["orders"].values()):
            raise PaperError("paper_owned_exit_already_pending")

    def _send(self, order: dict, event: dict):
        # Persist uncertainty BEFORE transport code can reach any write.
        order["write_attempted"] = True
        order["phase"] = "UNKNOWN"
        order["submitted_utc"] = now().isoformat()
        self.ledger.save()

        def before_send():
            snapshot = self._snapshot()
            if self.ledger.state["halts"]:
                raise PaperError("paper_operator_review_required")
            if order["action"] == "BUY":
                self._entry_gate(snapshot, exclude_ref=order["order_ref"])
                debit = decimal(order["limit_price"]) * 100 * order["quantity"] + decimal(order["fee_reserve_usd"], zero=True)
                if (debit > decimal(snapshot["available_funds_usd"], zero=True)
                        or not decimal(self.strategy["min_debit_usd"]) <= debit <= decimal(self.strategy["max_debit_usd"])):
                    raise PaperError("paper_cash_or_trade_budget_unavailable")
            else:
                self._owned_position_gate(order, snapshot)
            # Keep the freshest source/edit proof closest to transmission.
            # The transport follows this with its own fresh quote and age check.
            self._recheck(event)
            self._fresh(event)
            return True

        try:
            self.client.submit_limit(order_id=order["order_id"], order_ref=order["order_ref"],
                occ_symbol=order["occ_symbol"], action=order["action"], quantity=order["quantity"],
                limit_price=order["limit_price"],
                reference_price=event["reference_price_decimal"] if order["action"] == "BUY" else None,
                signal_created_utc=event["source_created_utc"], before_send=before_send)
            # A returned local Trade object is NOT a broker acknowledgement.
            order["phase"] = "UNKNOWN"
            order["last_error"] = "paper_write_attempted_awaiting_broker_evidence"
            self.ledger.notice("sent:" + order["order_ref"], "IBKR 模拟盘委托已尝试发送",
                {"合约": order["occ_symbol"], "方向": order["action"], "张数": order["quantity"],
                 "限价USD每份": order["limit_price"], "委托编号": str(order["order_id"]),
                 "说明": "等待券商回报与成交确认；不重复发送同一委托"})
        except Exception as exc:
            if getattr(exc, "mutation_attempted", None) is False:
                order["phase"], order["write_attempted"] = "NOT_SENT", False
                title = "IBKR 模拟盘委托未发送"
            else:
                order["phase"] = "UNKNOWN"
                title = "IBKR 模拟盘委托结果待核对"
            order["last_error"] = _error(exc)
            self.ledger.notice("write-result:" + order["order_ref"], title,
                {"合约": order["occ_symbol"], "方向": order["action"], "原因": order["last_error"],
                 "委托编号": str(order["order_id"]), "说明": "未知结果保留预算，只查询原委托身份，不自动重发"})
        self.ledger.save()

    def _open(self, event: dict, snapshot: dict):
        self._entry_gate(snapshot)
        occ, tier = event["contract"]["occ_symbol"], event["tier"]
        if any(lot["occ_symbol"] == occ and lot["tier"] == tier
               and (self.ledger.remaining(lot) or self.ledger.state["orders"][lot["entry_ref"]]["phase"] in _UNRESOLVED)
               for lot in self.ledger.state["lots"].values()):
            raise PaperError("paper_same_contract_tier_owned_or_pending")
        result = self._quote(event)
        limit = decimal(result.get("tick_aligned_price_cap_usd"))
        reference = decimal(event["reference_price_decimal"])
        cap = reference * (1 + decimal(self.strategy["max_buy_premium_pct"], zero=True) / 100)
        if (limit > cap or decimal(result["quote"]["ask_usd"]) > limit
                or result.get("tick_rule_verified") is not True):
            raise PaperError("paper_price_above_authorized_cap")
        qty, _ = self._quantity(limit, snapshot)
        self._fresh(event, new_intent=True)
        # The durable intent is local evidence, not a broker write. The final
        # before_send callback rechecks this exact source after broker risk
        # checks, immediately before the transport refreshes its quote.
        lot_id = source_identity(event["source_id"])
        lot = {"lot_id": lot_id, "occ_symbol": occ, "tier": tier, "entry_ref": None,
               "exit_refs": [], "close_source": None}
        self.ledger.state["lots"][lot_id] = lot
        try:
            order = self._new_order(event, action="BUY", quantity=qty, limit=limit,
                                    con_id=result["contract"]["con_id"], lot=lot)
        except Exception:
            if lot["entry_ref"] is None:
                self.ledger.state["lots"].pop(lot_id, None)
            raise
        self._send(order, event)

    def _close(self, event: dict, snapshot: dict):
        occ, tier = event["contract"]["occ_symbol"], event["tier"]
        matches = [lot for lot in self.ledger.state["lots"].values() if lot["occ_symbol"] == occ and lot["tier"] == tier
                   and (self.ledger.remaining(lot) or self.ledger.state["orders"][lot["entry_ref"]]["phase"] in _UNRESOLVED)]
        if len(matches) != 1:
            raise PaperError("paper_close_has_no_unique_owned_contract_tier")
        lot = matches[0]
        lot["close_source"] = event
        entry = self.ledger.state["orders"][lot["entry_ref"]]
        if entry["phase"] in _UNRESOLVED:
            self._cancel(entry, "close_signal_received")
            raise PaperError("paper_entry_waiting_for_cancellation")
        quantity = self.ledger.remaining(lot)
        if quantity <= 0:
            raise PaperError("paper_no_confirmed_owned_quantity")
        if any(self.ledger.state["orders"][ref]["phase"] in _UNRESOLVED for ref in lot["exit_refs"]):
            raise PaperError("paper_owned_exit_already_pending")
        result = self._quote(event)
        if result["contract"]["con_id"] != entry["con_id"]:
            raise PaperError("paper_close_contract_identity_mismatch")
        limit = decimal(result.get("sell_limit_price_usd"))
        self._fresh(event, new_intent=True)
        self._owned_position_gate({"lot_id": lot["lot_id"], "quantity": quantity,
            "con_id": entry["con_id"], "order_ref": "AAIBP:" + source_identity(event["source_id"])[:32]}, snapshot)
        order = self._new_order(event, action="SELL", quantity=quantity, limit=limit,
                                con_id=entry["con_id"], lot=lot)
        self._send(order, event)

    def _cancel(self, order: dict, reason: str):
        if order["cancel_attempted"] or order["phase"] not in _UNRESOLVED:
            return
        # Cancel only a broker-reported immutable identity. Unknown absence
        # cannot authorize a write against a guessed or reused numeric ID.
        broker = next((o for o in (self.last_snapshot or {}).get("orders", [])
                       if o.get("order_ref") == order["order_ref"]), None)
        if broker is None or broker.get("broker_reported") is not True or not self._order_match(order, broker):
            return
        order["cancel_attempted"] = True
        self.ledger.save()
        attempted = False
        try:
            result = self.client.cancel_order(order_ref=order["order_ref"], order_id=order["order_id"])
            attempted = result.get("mutation_attempted") is not False
            if not attempted:
                order["cancel_attempted"] = False
                if result.get("status") == "already_terminal":
                    self.ledger.save()
                    return
                order["last_error"] = "paper_cancel_not_sent"
        except Exception as exc:
            order["last_error"] = _error(exc)
            attempted = getattr(exc, "mutation_attempted", None) is not False
            if not attempted:
                order["cancel_attempted"] = False
        if attempted:
            self.ledger.notice("cancel:" + order["order_ref"], "IBKR 模拟盘已请求核对取消委托",
                {"合约": order["occ_symbol"], "方向": order["action"], "原因": reason,
                 "委托编号": str(order["order_id"]), "说明": "取消结果待核对；终态和部分成交核对前保留预算及持仓"})
        else:
            self.ledger.notice("cancel-deferred:" + order["order_ref"], "IBKR 模拟盘取消请求尚未发出",
                {"合约": order["occ_symbol"], "委托编号": str(order["order_id"]),
                 "原因": order["last_error"] or "paper_cancel_unconfirmed", "说明": "保留全部风险预留，下一轮先核对原委托再继续处理"})
        self.ledger.save()

    def _maintenance(self):
        for order in self.ledger.state["orders"].values():
            if order["phase"] not in _UNRESOLVED:
                continue
            record = self.ledger.state["sources"].get(source_identity(order["source_id"]), {})
            if record.get("invalidated") or record.get("last_status") == "pending":
                self._cancel(order, "source_invalidated_or_pending")
            elif order["action"] == "BUY":
                started = order["submitted_utc"] or order["created_utc"]
                lot = self.ledger.state["lots"][order["lot_id"]]
                if lot["close_source"] is not None:
                    self._cancel(order, "close_signal_received")
                elif (now() - timestamp(started)).total_seconds() >= self.strategy["entry_cancel_after_seconds"]:
                    self._cancel(order, "entry_timeout")
            elif (now() - timestamp(order["signal_created_utc"])).total_seconds() > self.strategy["max_close_signal_age_seconds"]:
                self._cancel(order, "close_signal_expired")

    def _claim_skip(self, event: dict, reason: str):
        record = self.ledger.state["sources"][source_identity(event["source_id"])]
        if record["decision"] is None:
            record["decision"] = reason
            if reason not in {"paper_source_before_activation", "paper_source_before_process_start", "paper_signal_expired"}:
                self.ledger.notice("skip:" + event["source_id"], "IBKR 模拟盘跳过本次信号",
                    {"合约": event.get("contract", {}).get("occ_symbol", "不可用"),
                     "方向": str(event.get("action", "不可用")), "原因": reason, "说明": "本信号没有创建新委托"})
        self.ledger.save()

    def step(self) -> dict:
        self.last_error = None
        snapshot = self._snapshot()
        source_error = None
        try:
            source = self._source()
        except Exception as exc:
            if isinstance(exc, LedgerError):
                raise
            source_error = _error(exc)
            source = {"source_healthy": False, "events": []}
        self._maintenance()
        events = sorted(source["events"], key=lambda e: timestamp(e["source_created_utc"]))
        closes = [e for e in events if e.get("verified") and e.get("action") == "close"]
        for event in events:
            record = self.ledger.state["sources"].get(source_identity(event["source_id"]))
            if record is None or record["decision"] is not None or not event.get("verified"):
                continue
            try:
                self._fresh(event, new_intent=True)
                if record["invalidated"]:
                    raise PaperError("paper_source_invalidated")
                if event["action"] == "open":
                    if any(e["contract"] == event["contract"] and e["tier"] == event["tier"]
                           and timestamp(e["source_created_utc"]) >= timestamp(event["source_created_utc"]) for e in closes):
                        raise PaperError("paper_later_close_already_observed")
                    self._open(event, snapshot)
                else:
                    self._close(event, snapshot)
            except LedgerError:
                raise
            except Exception as exc:
                reason = _error(exc)
                self.last_error = reason
                if reason not in _DEFER:
                    self._claim_skip(event, reason)
        self.ledger.save()
        self.ledger.flush(self.notifier)
        state = self.ledger.state
        return {"mode": "ibkr_paper", "paper_only": True, "execute_paper": True,
                "account_sha256": state["account_sha256"], "client_id": state["client_id"],
                "observed_utc": now().isoformat(), "activated_at": state["activated_at"],
                "process_started_utc": self.started.isoformat(), "source_healthy": source_error is None,
                "source_error": source_error, "broker_snapshot_complete": True,
                "entries_enabled": source_error is None and self.healthy_polls >= 2 and not self.entry_blocks
                                   and not state["halts"] and not any(o["phase"] in {"UNKNOWN", "SETTLING"} for o in state["orders"].values()),
                "entry_blocks": list(self.entry_blocks), "halts": list(state["halts"]),
                "option_cost_and_reserved_usd": float(self.ledger.used_usd()),
                "owned_contracts": sum(self.ledger.remaining(lot) for lot in state["lots"].values()),
                "pending_orders": sum(o["phase"] in _UNRESOLVED for o in state["orders"].values()),
                "unknown_orders": sum(o["phase"] == "UNKNOWN" for o in state["orders"].values()),
                "confirmed_execution_count": len(state["fills"]), "last_error": self.last_error,
                "broker_ack_is_not_fill": True, "historical_signals_replayed": False,
                "foreign_positions_adopted": False}
