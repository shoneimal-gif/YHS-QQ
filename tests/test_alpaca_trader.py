"""Synthetic Paper broker/ledger crash recovery and Discord event validation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

from alpaca_client import AlpacaError, SubmissionNotSent, SubmissionUnknown
from alpaca_trader import AlpacaLedgerError, AlpacaPaperTrader, TERMINAL
from config import SystemConfig, eastern_timezone
from wecom_notifications import _message, _normalise_event

SYMBOL = "NVDA261016C00188000"
ENTRY_ID = "00000000-0000-0000-0000-000000000001"
EXIT_ID = "00000000-0000-0000-0000-000000000002"


class SimulatedCrash(BaseException):
    pass


class FakeBroker:
    base_url = "https://paper-api.alpaca.markets"
    paper_only = True

    def __init__(self, now_fn):
        self.now_fn = now_fn
        self.orders = {}
        self.remote_quantity = 0
        self.submissions = []
        self.cancellations = []
        self.submit_error = None
        self.submit_response = None
        self.cancel_error = None
        self.get_error = None

    def get_account(self):
        return {"id": "synthetic-account", "status": "ACTIVE", "options_trading_level": 2,
                "equity": "200000", "cash": "200000", "options_buying_power": "200000"}

    def get_clock(self):
        now = self.now_fn()
        return {"is_open": True, "timestamp": now.isoformat(),
                "next_close": now.replace(hour=16, minute=0, second=0).isoformat()}

    def get_positions(self):
        if not self.remote_quantity:
            return []
        return [{"symbol": SYMBOL, "qty": str(self.remote_quantity),
                 "asset_class": "us_option", "side": "long"}]

    def get_orders(self, status="open"):
        return [deepcopy(row) for row in self.orders.values()
                if row["status"] not in TERMINAL or status != "open"]

    def get_order(self, order_id):
        if self.get_error:
            raise self.get_error
        return next((deepcopy(row) for row in self.orders.values() if row["id"] == order_id), None)

    def get_order_by_client_id(self, client_id):
        if self.get_error:
            raise self.get_error
        return deepcopy(self.orders.get(client_id))

    def get_contract(self, symbol):
        return {"symbol": symbol, "tradable": True, "status": "active", "type": "call",
                "underlying_symbol": "NVDA", "expiration_date": "2026-10-16",
                "strike_price": "188", "size": "100"}

    def get_option_snapshot(self, symbol, feed="indicative"):
        return {"greeks": {"delta": 0.45, "gamma": 0.04},
                "latestQuote": {"bp": 2.99, "ap": 3.0, "t": self.now_fn().isoformat()}}

    def get_option_quotes(self, symbols, feed="indicative"):
        return {symbol: {"bp": 2.99, "ap": 3.0, "t": self.now_fn().isoformat()} for symbol in symbols}

    def submit_order(self, payload, *, before_send=None):
        if before_send:
            before_send()
        self.submissions.append(dict(payload))
        if self.submit_error:
            raise self.submit_error
        row = {"id": ENTRY_ID if payload["side"] == "buy" else EXIT_ID,
               "client_order_id": payload["client_order_id"], "symbol": payload["symbol"],
               "side": payload["side"], "qty": payload["qty"], "filled_qty": "0", "status": "new"}
        if self.submit_response:
            row.update(self.submit_response)
        self.orders[payload["client_order_id"]] = deepcopy(row)
        return row

    def cancel_order(self, order_id):
        self.cancellations.append(order_id)
        if self.cancel_error:
            raise self.cancel_error


class AlpacaTraderCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-alpaca-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 9, 10, 0, tzinfo=eastern_timezone())
        self.config = SystemConfig(api_key="", output_dir=self.root / "outputs")
        self.client = FakeBroker(lambda: self.now)
        self.events = []
        self.signal = SimpleNamespace(signal_id="synthetic-signal", ticker="NVDA", action="BUY_CALL",
                                      expiration="2026-10-16", flow_contract=SYMBOL, spot=188)
        self.quote = {"contract_symbol": SYMBOL}
        self.trader = self.create_trader()

    def accept(self, event):
        self.events.append(deepcopy(event))
        return True

    def create_trader(self, *, execute=True, sink=None):
        trader = AlpacaPaperTrader(self.client, self.config, self.root / "ledger.json",
                                   execute_paper=execute, now_fn=lambda: self.now,
                                   notification_sink=self.accept if sink is None else sink)
        self.addCleanup(trader.release_state_lock)
        return trader

    def restart(self, *, sink=None):
        self.trader.release_state_lock()
        self.trader = self.create_trader(sink=sink)
        return self.trader

    def open_pending(self):
        position = self.trader.open_position(self.signal, self.quote, self.now)
        self.assertIsNotNone(position)
        self.assertEqual(position.status, "PENDING_ENTRY")
        return position

    def row(self, position, *, entry=True, state="filled", filled=3, price=3.0, stamp=None, quantity=None):
        return {"id": (position.entry_order_id or ENTRY_ID) if entry else (position.exit_order_id or EXIT_ID),
                "client_order_id": position.entry_client_order_id if entry else position.exit_client_order_id,
                "symbol": SYMBOL, "side": "buy" if entry else "sell",
                "qty": str(position.quantity if entry else quantity if quantity is not None else
                           position.remaining_quantity + position.exit_order_filled_quantity),
                "status": state, "filled_qty": str(filled), "filled_avg_price": str(price),
                "updated_at": (stamp if stamp is not None else self.now.isoformat())}

    def apply(self, position, **kwargs):
        entry = kwargs.get("entry", True)
        row = self.row(position, **kwargs)
        self.client.orders[row["client_order_id"]] = deepcopy(row)
        self.trader._apply_order(position, row, self.now, entry=entry)
        self.trader._persist()
        return row

    def open_filled(self):
        position = self.open_pending()
        self.apply(position)
        self.client.remote_quantity = 3
        self.assertEqual(position.status, "OPEN")
        return position

    def start_exit(self, position):
        self.trader._request_exit(position, self.now, "SYNTHETIC_EXIT")
        self.assertEqual(position.status, "PENDING_EXIT")
        return position

    def test_paper_budget_quantity_and_preview_are_preserved(self):
        self.trader.release_state_lock()
        self.trader = self.create_trader(execute=False)
        self.assertIsNone(self.trader.open_position(self.signal, self.quote, self.now))
        self.assertEqual(self.client.submissions, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.trader.policy.strategy_budget_usd, 100000)
        self.assertEqual(self.trader.policy.target_premium_fraction, 0.01)
        self.assertEqual(self.trader.policy.max_open_positions, 10)

    def test_submit_notice_and_intent_reserved_before_post(self):
        original = self.client.submit_order
        def submit(payload, **kwargs):
            saved = json.loads(self.trader.state_path.read_text())
            self.assertEqual(saved["positions"][0]["status"], "ENTRY_INTENT")
            self.assertEqual(saved["positions"][0]["entry_client_order_id"], payload["client_order_id"])
            return original(payload, **kwargs)
        self.client.submit_order = submit
        position = self.open_pending()
        self.assertEqual(position.quantity, 3)
        self.assertEqual(position.reserved_debit, 902)
        event = self.events[-1]
        self.assertEqual((event["kind"], event["contract_symbol"], event["price"]), ("submitted", SYMBOL, 3))
        self.assertIn("$900.00", _message(_normalise_event(event)))

    def test_partial_entry_differing_average_notifies_incremental_amount(self):
        position = self.open_pending()
        self.apply(position, state="partially_filled", filled=1, price=2.8)
        self.assertEqual(position.reserved_debit, 882)
        first = self.events[-1]
        self.assertEqual((first["new_fill_quantity"], first["new_fill_notional_usd"]), (1, 280))
        self.apply(position, state="partially_filled", filled=2, price=2.9)
        event = self.events[-1]
        self.assertAlmostEqual(event["new_fill_notional_usd"], 300)
        self.assertAlmostEqual(event["new_fill_price"], 3.0)
        self.assertAlmostEqual(event["cumulative_notional_usd"], 580)
        message = _message(_normalise_event(event))
        self.assertIn("部分成交", message)
        self.assertIn("本次新增成交均价：$3.0000", message)
        self.assertIn("累计买入金额：$580.00", message)
        self.apply(position, filled=3, price=2.9)
        self.assertEqual(position.status, "OPEN")
        self.assertIn("全部成交", _message(_normalise_event(self.events[-1])))

    def test_repeated_partial_and_full_fill_do_not_duplicate_money_or_notices(self):
        position = self.open_filled()
        self.start_exit(position)
        row = self.apply(position, entry=False, state="partially_filled", filled=1, price=3.5)
        self.assertEqual((position.exit_proceeds_usd, position.exit_filled_quantity), (350, 1))
        count = len(self.events)
        self.trader._apply_order(position, row, self.now, entry=False)
        self.trader._persist()
        self.assertEqual(len(self.events), count)
        self.assertEqual(position.partial_realized_pnl, 50)
        self.assertEqual(self.events[-1]["new_realized_pnl"], 50)
        self.assertEqual(self.events[-1]["partial_realized_pnl"], 50)
        self.assertIsNone(self.events[-1]["realized_pnl"])
        self.apply(position, entry=False, filled=3, price=3.8)
        self.assertEqual(position.status, "CLOSED")
        self.assertAlmostEqual(position.realized_pnl, 240)
        self.assertEqual(self.events[-1]["remaining_quantity"], 0)
        self.assertAlmostEqual(self.events[-1]["realized_pnl"], 240)
        self.assertAlmostEqual(self.events[-1]["new_fill_notional_usd"], 790)
        self.restart()
        self.assertAlmostEqual(self.trader.statistics()["realized_pnl"], 240)
        self.assertEqual(len(self.trader.closed_positions), 1)

    def test_partial_exit_cancel_then_retry_preserves_total_proceeds(self):
        position = self.open_filled()
        self.start_exit(position)
        first_id = position.exit_client_order_id
        self.apply(position, entry=False, state="partially_filled", filled=1, price=3.5)
        self.apply(position, entry=False, state="canceled", filled=1, price=3.5)
        event = self.events[-1]
        self.assertEqual((event["kind"], event["requested_quantity"], event["filled_quantity"], event["remaining_quantity"]),
                         ("cancelled", 3, 1, 2))
        self.assertIsNone(event["realized_pnl"])
        self.assertEqual(position.status, "OPEN")
        self.client.remote_quantity = 2
        self.start_exit(position)
        self.assertNotEqual(position.exit_client_order_id, first_id)
        self.apply(position, entry=False, filled=2, price=4, quantity=2)
        self.assertEqual(position.realized_pnl, 250)
        self.assertEqual(position.exit_proceeds_usd, 1150)
        self.assertEqual(self.events[-1]["requested_quantity"], 2)
        self.assertEqual(self.events[-1]["remaining_quantity"], 0)
        self.assertEqual(sum(position.exit_realized_by_day.values()), 250)

    def test_cancellation_request_unknown_keeps_remaining_entry_reservation(self):
        position = self.open_pending()
        self.apply(position, state="partially_filled", filled=1, price=3)
        self.client.cancel_error = SubmissionUnknown("synthetic cancellation uncertain")
        self.trader._cancel_entry(position)
        self.assertEqual(position.reserved_debit, 902)
        self.assertEqual(position.status, "PENDING_ENTRY")
        self.apply(position, state="canceled", filled=1, price=3)
        self.assertEqual(position.reserved_debit, 302)
        self.assertEqual(position.status, "OPEN")
        self.assertEqual(self.events[-1]["kind"], "cancelled")

    def test_rejected_and_expired_entry_release_only_after_terminal_confirmation(self):
        for state, kind in (("rejected", "rejected"), ("expired", "cancelled")):
            with self.subTest(state=state):
                self.signal.signal_id = state
                position = self.open_pending()
                self.apply(position, state=state, filled=0)
                self.assertEqual(position.status, "CANCELLED")
                self.assertEqual(position.reserved_debit, 0)
                self.assertEqual(self.events[-1]["kind"], kind)

    def test_unknown_broker_status_does_not_apply_reported_fills_or_release_budget(self):
        position = self.open_pending()
        self.apply(position, state="new_vendor_status", filled=3, price=3)
        self.assertEqual(position.status, "ENTRY_UNKNOWN")
        self.assertEqual(position.filled_quantity, 0)
        self.assertEqual(position.reserved_debit, 902)
        self.assertEqual(self.events[-1]["kind"], "unknown")
        self.assertEqual(self.events[-1]["broker_status"], "new_vendor_status")
        self.assertIs(self.trader.open_position(self.signal, self.quote, self.now), position)
        self.assertEqual(len(self.client.submissions), 1)

    def test_unknown_exit_state_retains_position_and_unconfirmed_profit(self):
        position = self.open_filled()
        self.start_exit(position)
        self.apply(position, entry=False, state="new_vendor_status", filled=3, price=9)
        self.assertEqual(position.status, "EXIT_UNKNOWN")
        self.assertEqual(position.remaining_quantity, 3)
        self.assertEqual(position.exit_proceeds_usd, 0)
        self.assertIsNone(position.realized_pnl)
        self.assertEqual(self.trader.statistics()["realized_pnl"], 0)
        self.assertEqual(position.reserved_debit, 902)

    def test_contradictory_full_exit_does_not_create_realized_profit(self):
        position = self.open_filled()
        self.start_exit(position)
        before = asdict(position)
        with self.assertRaisesRegex(ValueError, "矛盾"):
            self.trader._apply_order(position, self.row(position, entry=False, state="partially_filled", filled=3, price=9), self.now, entry=False)
        self.assertEqual(asdict(position), before)
        self.assertEqual(self.trader.statistics()["realized_pnl"], 0)

    def test_malformed_exit_timestamp_cannot_partially_mutate_ledger(self):
        position = self.open_filled()
        self.start_exit(position)
        before = asdict(position)
        with self.assertRaises(ValueError):
            self.trader._apply_order(position, self.row(position, entry=False, state="partially_filled", filled=1, price=4, stamp="broken"), self.now, entry=False)
        self.assertEqual(asdict(position), before)
        self.restart()
        restored = self.trader.positions[0]
        self.assertEqual(restored.exit_filled_quantity, 0)
        self.assertEqual(restored.exit_proceeds_usd, 0)

    def test_future_fill_timestamp_is_not_applied(self):
        position = self.open_pending()
        before = asdict(position)
        with self.assertRaisesRegex(ValueError, "未来"):
            self.trader._apply_order(position, self.row(position, stamp=(self.now + timedelta(hours=1)).isoformat()), self.now, entry=True)
        self.assertEqual(asdict(position), before)

    def test_fill_regression_and_negative_incremental_amount_fail_without_mutation(self):
        position = self.open_filled()
        self.start_exit(position)
        self.apply(position, entry=False, state="partially_filled", filled=1, price=8)
        for filled, price in ((0, 8), (2, 3)):
            before = asdict(position)
            with self.subTest(filled=filled), self.assertRaises(AlpacaLedgerError):
                self.trader._apply_order(position, self.row(position, entry=False, state="partially_filled", filled=filled, price=price), self.now, entry=False)
            self.assertEqual(asdict(position), before)

    def test_wrong_order_ownership_does_not_mutate_ledger(self):
        position = self.open_pending()
        before = asdict(position)
        row = self.row(position)
        row["client_order_id"] = "other-strategy"
        with self.assertRaises(AlpacaLedgerError):
            self.trader._apply_order(position, row, self.now, entry=True)
        self.assertEqual(asdict(position), before)

    def test_crash_after_entry_intent_recovers_unknown_without_resubmission(self):
        self.client.submit_error = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.trader.open_position(self.signal, self.quote, self.now)
        self.restart()
        position = self.trader.positions[0]
        self.assertEqual(position.status, "ENTRY_UNKNOWN")
        self.client.submit_error = None
        self.trader.poll(self.now, self.client.get_clock())
        self.assertIs(self.trader.open_position(self.signal, self.quote, self.now), position)
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(position.reserved_debit, 902)

    def test_crash_after_exit_intent_recovers_unknown_without_duplicate_sell(self):
        position = self.open_filled()
        self.client.submit_error = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.trader._request_exit(position, self.now, "SYNTHETIC_EXIT")
        self.restart()
        position = self.trader.positions[0]
        self.assertEqual(position.status, "EXIT_UNKNOWN")
        self.trader.poll(self.now, self.client.get_clock())
        self.trader.close_all(self.now, "RUNNER_STOP")
        self.assertEqual(len(self.client.submissions), 2)
        self.assertEqual(position.remaining_quantity, 3)
        self.assertIsNone(position.realized_pnl)

    def test_entry_submission_unknown_reconciles_same_client_id(self):
        self.client.submit_error = SubmissionUnknown("synthetic timeout")
        position = self.trader.open_position(self.signal, self.quote, self.now)
        self.assertEqual(position.status, "ENTRY_UNKNOWN")
        self.assertEqual(position.reserved_debit, 902)
        self.assertEqual(self.events[-1]["kind"], "unknown")
        self.client.submit_error = None
        self.client.orders[position.entry_client_order_id] = self.row(position)
        self.client.remote_quantity = 3
        self.trader.poll(self.now, self.client.get_clock())
        self.assertEqual(position.status, "OPEN")
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(position.entry_client_order_id, self.client.submissions[0]["client_order_id"])

    def test_malformed_exit_submission_preserves_unknown_intent_without_fake_pnl(self):
        position = self.open_filled()
        self.client.submit_response = {"status": "filled", "filled_qty": "3", "filled_avg_price": "4", "updated_at": "broken"}
        self.trader._request_exit(position, self.now, "SYNTHETIC_EXIT")
        self.assertEqual(position.status, "EXIT_UNKNOWN")
        self.assertEqual(position.exit_filled_quantity, 0)
        self.assertIsNone(position.realized_pnl)
        self.restart()
        self.assertEqual(self.trader.positions[0].status, "EXIT_UNKNOWN")

    def test_submission_not_sent_produces_no_broker_notice(self):
        self.client.submit_error = SubmissionNotSent("synthetic local guard")
        self.assertIsNone(self.trader.open_position(self.signal, self.quote, self.now))
        self.assertEqual(self.trader.positions[0].status, "CANCELLED")
        self.assertEqual(self.events, [])

    def test_rejected_submission_notification_has_requested_quantity(self):
        self.client.submit_error = AlpacaError("synthetic rejection", status=422)
        self.assertIsNone(self.trader.open_position(self.signal, self.quote, self.now))
        self.assertEqual(self.events[-1]["kind"], "rejected")
        self.assertEqual(self.events[-1]["requested_quantity"], 3)
        self.assertEqual(self.events[-1]["remaining_quantity"], 0)

    def test_unconfirmed_exit_never_exports_closed_csv_or_profit(self):
        position = self.open_filled()
        self.start_exit(position)
        self.trader.close_all(self.now, "RUNNER_STOP")
        self.assertEqual(len(self.client.submissions), 2)
        path = self.trader.export_trades_csv()
        self.assertEqual(len(path.read_text(encoding="utf-8-sig").splitlines()), 1)
        self.assertEqual(self.trader.statistics()["closed_realized_pnl"], 0)

    def test_partial_fill_restart_retains_amounts_and_deduplicates(self):
        position = self.open_filled()
        self.start_exit(position)
        row = self.apply(position, entry=False, state="partially_filled", filled=1, price=4)
        self.client.remote_quantity = 2
        count = len(self.events)
        self.restart()
        self.trader.poll(self.now, self.client.get_clock())
        restored = self.trader.positions[0]
        self.assertEqual(restored.exit_proceeds_usd, 400)
        self.assertEqual(restored.exit_filled_quantity, 1)
        self.assertEqual(restored.partial_realized_pnl, 100)
        self.assertEqual(len(self.events), count)

    def test_crash_between_ledger_commit_and_outbox_enqueue_replays_notice(self):
        position = self.open_pending()
        self.events.clear()
        self.trader.notification_sink = lambda event: (_ for _ in ()).throw(SimulatedCrash())
        self.trader._apply_order(position, self.row(position, state="partially_filled", filled=1, price=3), self.now, entry=True)
        with self.assertRaises(SimulatedCrash):
            self.trader._persist()
        payload = json.loads(self.trader.state_path.read_text())
        self.assertEqual(payload["positions"][0]["filled_quantity"], 1)
        self.assertEqual(len(payload["pending_notifications"]), 1)
        identifier = next(iter(payload["pending_notifications"]))
        self.restart()
        self.trader._persist()
        self.assertEqual([event["event_id"] for event in self.events], [identifier])
        self.assertEqual(json.loads(self.trader.state_path.read_text())["pending_notifications"], {})

    def test_outbox_refusal_survives_restart_with_original_event_id(self):
        self.trader.notification_sink = lambda event: False
        position = self.open_pending()
        pending = json.loads(self.trader.state_path.read_text())["pending_notifications"]
        self.assertEqual(len(pending), 1)
        self.restart()
        self.trader._persist()
        self.assertEqual(self.events[-1]["event_id"], next(iter(pending)))
        self.assertEqual(self.trader._notification_events, {})

    def test_non_boolean_outbox_acknowledgment_cannot_lose_event(self):
        self.trader.notification_sink = lambda event: "accepted"
        self.open_pending()
        self.assertEqual(len(json.loads(self.trader.state_path.read_text())["pending_notifications"]), 1)

    def test_crash_after_enqueue_before_ack_can_safely_replay_same_id(self):
        position = self.open_pending()
        self.events.clear()
        self.trader._apply_order(position, self.row(position, state="partially_filled", filled=1, price=3), self.now, entry=True)
        save = self.trader._save_state
        calls = 0
        def fail_ack():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AlpacaLedgerError("synthetic interrupted acknowledgement")
            save()
        with patch.object(self.trader, "_save_state", side_effect=fail_ack):
            with self.assertRaises(AlpacaLedgerError):
                self.trader._persist()
        delivered = self.events[-1]["event_id"]
        self.assertIn(delivered, self.trader._notification_events)
        self.restart()
        self.trader._persist()
        self.assertEqual(self.events[-1]["event_id"], delivered)

    def test_corrupt_realized_or_negative_amounts_ledger_refuses_reset(self):
        position = self.open_filled()
        self.trader.release_state_lock()
        payload = json.loads(self.trader.state_path.read_text())
        for key, value in (("realized_pnl", 999), ("exit_proceeds_usd", -1), ("status", "CANCELLED"), ("quantity", True)):
            with self.subTest(key=key):
                broken = deepcopy(payload)
                broken["positions"][0][key] = value
                self.trader.state_path.write_text(json.dumps(broken))
                with self.assertRaises(AlpacaLedgerError):
                    self.create_trader()
        self.assertEqual(len(self.client.submissions), 1)

    def test_nonempty_ledger_cannot_rebind_to_a_different_account(self):
        self.open_pending()
        self.trader.release_state_lock()
        payload = json.loads(self.trader.state_path.read_text())
        payload["account_id"] = None
        self.trader.state_path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(AlpacaLedgerError, "绑定原账户"):
            self.create_trader()

    def test_terminal_notice_separates_order_limit_and_confirmed_fill_amount(self):
        position = self.open_pending()
        self.apply(position, state="partially_filled", filled=1, price=2.8)
        self.apply(position, state="canceled", filled=1, price=2.8)
        event = self.events[-1]
        self.assertEqual(event["limit_price"], 3)
        self.assertEqual(event["confirmed_fill_avg_price"], 2.8)
        self.assertEqual(event["confirmed_notional_usd"], 280)
        message = _message(_normalise_event(event))
        self.assertIn("委托限价：$3.0000", message)
        self.assertIn("已确认成交均价：$2.8000", message)
        self.assertIn("$280.00", message)
        self.assertIn("$900.00", message)

    def test_unknown_submission_notice_retains_known_limit(self):
        self.client.submit_error = SubmissionUnknown("synthetic timeout")
        self.trader.open_position(self.signal, self.quote, self.now)
        message = _message(_normalise_event(self.events[-1]))
        self.assertIn("委托限价：$3.0000", message)
        self.assertIn("$900.00", message)
        self.assertNotIn("已确认成交均价", message)

    def test_large_exit_average_overflow_cannot_mutate_ledger(self):
        position = self.open_filled()
        self.start_exit(position)
        before = asdict(position)
        with self.assertRaises(ValueError):
            self.trader._apply_order(position, self.row(position, entry=False, price=1e308), self.now, entry=False)
        self.assertEqual(asdict(position), before)

    def test_unknown_status_after_partial_exit_retains_prior_confirmed_amount(self):
        position = self.open_filled()
        self.start_exit(position)
        self.apply(position, entry=False, state="partially_filled", filled=1, price=3.5)
        self.apply(position, entry=False, state="new_vendor_status", filled=2, price=9)
        event = self.events[-1]
        self.assertEqual((event["kind"], event["filled_quantity"], event["remaining_quantity"]), ("unknown", 1, 2))
        self.assertEqual(event["confirmed_fill_avg_price"], 3.5)
        self.assertEqual(event["confirmed_notional_usd"], 350)
        self.assertEqual(event["partial_realized_pnl"], 50)
        self.assertEqual(position.exit_filled_quantity, 1)
        self.assertEqual(position.reserved_debit, 602)
        self.assertIsNone(event["realized_pnl"])
        self.assertIn("$350.00", _message(_normalise_event(event)))

    def test_shared_formatter_rejects_overflow_and_incomplete_money_pair(self):
        self.open_pending()
        event = self.events[-1]
        with self.assertRaises(ValueError):
            _normalise_event(dict(event, price=1e308, limit_price=1e308))
        with self.assertRaises(ValueError):
            _normalise_event(dict(event, confirmed_fill_avg_price=3.0))

    def test_risk_policy_change_does_not_reset_existing_budget(self):
        self.open_pending()
        self.trader.release_state_lock()
        with self.assertRaises(AlpacaLedgerError):
            AlpacaPaperTrader(self.client, self.config, self.root / "ledger.json",
                              policy={"max_daily_entries": 19}, now_fn=lambda: self.now)

    def test_shared_formatter_rejects_fractional_or_impossible_contract_counts(self):
        self.open_pending()
        event = self.events[-1]
        for key, value in (("requested_quantity", 1.5), ("filled_quantity", 4),
                           ("new_fill_quantity", 1), ("price", float("nan"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                _normalise_event(dict(event, **{key: value}))

    def test_shared_formatter_rejects_inconsistent_monetary_totals(self):
        position = self.open_filled()
        event = self.events[-1]
        with self.assertRaises(ValueError):
            _normalise_event(dict(event, cumulative_notional_usd=1))
        with self.assertRaises(ValueError):
            _normalise_event(dict(event, new_fill_notional_usd=1))


if __name__ == "__main__":
    unittest.main()
