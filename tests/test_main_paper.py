"""Paper 入口、通知选择、关停和锁的离线集成边界。"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as app
from alpaca_trader import AlpacaRiskConfig
from config import SystemConfig, eastern_timezone
from detector import DataContractError


class PaperEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yhs-main-paper-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = SystemConfig(api_key="synthetic-yhs", output_dir=self.root / "output")
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=eastern_timezone())
        self.clock = {"is_open": False, "timestamp": self.now.isoformat()}
        self.broker = Mock()
        self.broker.get_account.return_value = {"id": "synthetic-paper-account"}
        self.trader = Mock()
        self.trader.policy = AlpacaRiskConfig()
        self.trader.open_positions = []
        self.trader.notification_sink = None
        self.trader.statistics.return_value = {
            "network_ok": True, "total_trades": 0, "directional_accuracy_pct": 0,
            "avg_peak_mfe_pct": 0, "realized_pnl": 0,
        }
        self.trader.export_trades_csv.side_effect = self.export

    def export(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("contract_symbol,quantity\n", encoding="utf-8")
        return path

    def run_closed(self, *, execute=False, webhook=None, notifier=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(app, "__file__", str(self.root / "main.py")))
            stack.enter_context(patch.object(app, "_load_alpaca_client", return_value=self.broker))
            stack.enter_context(patch.object(app, "_alpaca_session", return_value=(self.now, self.clock, False)))
            stack.enter_context(patch("alpaca_trader.AlpacaPaperTrader", return_value=self.trader))
            yhs = stack.enter_context(patch.object(app, "YHSClient", side_effect=AssertionError("休市不应读取YHS")))
            legacy_wecom = stack.enter_context(patch("wecom_notifications.load_webhook", side_effect=AssertionError("不应读取私有微信配置")))
            legacy_push = stack.enter_context(patch("pushplus_notifications.load_token", side_effect=AssertionError("不应读取私有pushplus配置")))
            load = stack.enter_context(patch("discord_notifications.load_webhook", return_value=webhook))
            constructor = stack.enter_context(patch("discord_notifications.DiscordNotifier", return_value=notifier))
            report = app.run_alpaca_paper(self.config, execute_paper=execute)
            yhs.assert_not_called()
            legacy_wecom.assert_not_called()
            legacy_push.assert_not_called()
        return report, load, constructor

    def test_market_closed_preview_never_loads_notifications(self):
        report, load, constructor = self.run_closed()
        self.assertEqual(report["exit_reason"], "MARKET_CLOSED")
        self.assertFalse(report["execute_paper"])
        self.assertFalse(report["pending_remote_state"])
        load.assert_not_called()
        constructor.assert_not_called()
        self.trader.poll.assert_called_once_with(self.now, self.clock, allow_new_entries=False)
        self.trader.release_state_lock.assert_called_once()
        self.broker.close.assert_called_once()

    def test_execute_enables_only_discord_and_closes_queue(self):
        notifier = Mock()
        notifier.enqueue.return_value = True
        report, load, constructor = self.run_closed(execute=True, webhook="synthetic-placeholder", notifier=notifier)
        load.assert_called_once_with(self.root)
        digest = hashlib.sha256(b"synthetic-paper-account").hexdigest()
        constructor.assert_called_once_with("synthetic-placeholder", self.root / "runtime" / "alpaca_accounts" / digest / "discord_outbox.json")
        self.assertTrue(report["execute_paper"])
        event = {"event_id": "synthetic-event"}
        self.assertTrue(self.trader.notification_sink(event))
        notifier.enqueue.assert_called_once_with(event)
        notifier.close.assert_called_once_with(timeout=5)

    def test_missing_cloud_discord_credentials_do_not_enable_channel(self):
        _, load, constructor = self.run_closed(execute=True)
        load.assert_called_once()
        constructor.assert_not_called()
        self.assertIsNone(self.trader.notification_sink)

    def test_notification_enqueue_failure_is_not_acknowledged(self):
        notifier = Mock()
        notifier.enqueue.side_effect = OSError("synthetic-storage-failure")
        self.run_closed(execute=True, webhook="synthetic-placeholder", notifier=notifier)
        self.assertFalse(self.trader.notification_sink({"event_id": "synthetic-event"}))

    def test_unhealthy_reconciliation_keeps_pending_remote_state(self):
        self.trader.statistics.return_value["network_ok"] = False
        report, _, _ = self.run_closed()
        self.assertTrue(report["failed"])
        self.assertTrue(report["pending_remote_state"])

    def test_same_account_lock_does_not_depend_on_output_dir(self):
        digest = hashlib.sha256(b"synthetic-paper-account").hexdigest()
        with app.runner_lock(self.root / "runtime" / "alpaca_accounts" / digest):
            with self.assertRaisesRegex(RuntimeError, "已有运行进程"):
                self.run_closed()
        self.broker.close.assert_called_once()

    def test_lock_released_after_exception_without_deleting_state(self):
        directory = self.root / "lock"
        with self.assertRaisesRegex(ValueError, "synthetic"):
            with app.runner_lock(directory):
                (directory / "ledger.json").write_text('{"cash": 100000}', encoding="utf-8")
                raise ValueError("synthetic")
        with app.runner_lock(directory):
            self.assertEqual(json.loads((directory / "ledger.json").read_text())["cash"], 100000)

    def test_shutdown_does_not_claim_unconfirmed_exit(self):
        self.trader.open_positions = [Mock()]
        with patch.object(app, "BROKER_RECONCILE_SECONDS", 0.05), patch.object(app, "BROKER_POLL_SECONDS", 0.01):
            report = app._reconcile_broker_shutdown(self.trader, self.broker, lambda: self.now, "USER_INTERRUPT")
        self.assertTrue(report["shutdown_pending"])
        self.assertFalse(report["shutdown_reconciled"])
        self.trader.close_all.assert_called_once_with(self.now, "USER_INTERRUPT")
        self.assertGreaterEqual(self.trader.poll.call_count, 1)

    def test_shutdown_accepts_healthy_confirmation_only(self):
        report = app._reconcile_broker_shutdown(self.trader, self.broker, lambda: self.now, "SESSION_CLOSE")
        self.assertTrue(report["shutdown_reconciled"])
        self.assertFalse(report["shutdown_pending"])
        self.assertEqual(report["shutdown_reconcile_errors"], 0)

    def test_half_day_calendar_sets_early_close(self):
        frozen = datetime(2026, 11, 27, 10, 0, tzinfo=eastern_timezone())
        clock = {"is_open": True, "timestamp": frozen.isoformat()}
        self.broker.get_clock.return_value = clock
        self.broker.get_calendar.return_value = [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}]
        with patch.object(app, "datetime", wraps=datetime) as dt:
            dt.now.return_value = frozen
            _, _, trading_day = app._alpaca_session(self.config, self.broker)
        self.assertTrue(trading_day)
        self.assertEqual(self.config.session_bounds(date(2026, 11, 27))[1].hour, 13)

    def test_calendar_absolute_time_must_be_zoned(self):
        with self.assertRaises(DataContractError):
            app._calendar_bound(date(2026, 11, 27), "2026-11-27T13:00:00")


if __name__ == "__main__":
    unittest.main()
