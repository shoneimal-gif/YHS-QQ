"""主入口离线验证：不访问 YHS，不提交真实订单，不断言机器性能。"""
from __future__ import annotations

import csv
import json
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

import main as app
from config import SystemConfig, eastern_timezone
from detector import DataContractError, SignalDetector
from mock_replay import MockYHSClient
from paper_trader import PaperTrader
from yhs_client import APIError, RequestCancelled


def without_simulation_flags(value):
    if isinstance(value, dict):
        return {key: without_simulation_flags(item) for key, item in value.items() if key != "simulated"}
    if isinstance(value, list):
        return [without_simulation_flags(item) for item in value]
    return value


class MainCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="main-test-", dir=Path(__file__).parent,
                                                ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = SystemConfig(api_key="", output_dir=self.directory)
        self.day = date(2026, 10, 9)
        self.start = datetime(2026, 10, 9, 9, 30, tzinfo=eastern_timezone())

    def test_full_synthetic_replay_and_request_arithmetic(self) -> None:
        report = app.run_replay(self.config, self.day)
        self.assertEqual(report["observation_steps"], 78)
        self.assertEqual(report["total_trades"], 5)
        self.assertEqual(report["directional_accuracy_pct"], 60.0)
        self.assertTrue(report["simulated_data"])
        self.assertEqual(report["api_quota_used"], 0)
        counts = report["mock_request_counts"]
        self.assertEqual(counts["volume_top"], 1)
        self.assertEqual(counts["heatmap"], 57 * 78)
        self.assertEqual(counts["flow"], 11)
        self.assertEqual(counts["option_chain"], 5)
        self.assertEqual(sum(counts.values()), 4463)
        with (self.directory / "trades_history.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row["exit_reason"] for row in rows},
                         {"HARD_STOP", "BREAKEVEN_STOP", "TRAILING_STOP", "HARD_TAKE_PROFIT", "SESSION_CLOSE"})
        self.assertTrue((self.directory / "summary.json").exists())
        self.assertTrue((self.directory / "detector_state.json").exists())

    def test_replay_refuses_existing_ledger(self) -> None:
        (self.directory / "replay_state.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "已有回放账本"):
            app.run_replay(self.config, self.day)

    def test_live_cli_without_api_key_returns_failure(self) -> None:
        with patch.dict("os.environ", {"YHS_API_KEY": ""}), patch("builtins.print"):
            try:
                self.assertEqual(app.main(["--live", "--output-dir", str(self.directory)]), 1)
            finally:
                # Windows 不允许移除仍被 FileHandler 打开的测试目录。
                for handler in list(logging.getLogger().handlers):
                    if isinstance(handler, logging.FileHandler):
                        handler.close()
                        logging.getLogger().removeHandler(handler)

    def test_remaining_weekdays_at_month_end(self) -> None:
        self.assertEqual(app.remaining_weekdays(date(2026, 10, 26)), 5)
        self.assertEqual(app.remaining_weekdays(date(2026, 10, 30)), 1)
        self.assertEqual(app.remaining_weekdays(date(2026, 10, 31)), 1)

    def test_pool_budget_preserves_priority_and_never_exceeds_share(self) -> None:
        mock = MockYHSClient(self.config)
        pool = SignalDetector(self.config).filter_pool(mock.fetch_volume_top())

        class Budget:
            def budget_status(self):
                return {"normal_available": 5000}

        fitted = app.fit_pool_budget(pool, self.config, Budget(), date(2026, 10, 26))
        self.assertEqual(len(fitted), 16)
        self.assertEqual(fitted, pool[:16])
        cost = len(fitted) * 57 + 1 + self.config.daily_flow_request_cap + self.config.daily_chain_request_cap
        self.assertLessEqual(cost, 1000)

    def _recorded_file(self, *, frame_hour=9, frame_minute=35) -> Path:
        mock = MockYHSClient(self.config)
        mock.set_time(self.start)
        universe = without_simulation_flags(mock.fetch_volume_top())
        heatmap = without_simulation_flags(mock.fetch_heatmap("NVDA"))
        flow = without_simulation_flags(mock.fetch_flow("NVDA"))
        decision_time = self.start.replace(hour=frame_hour, minute=frame_minute, second=30)
        if frame_hour >= 16:
            heatmap["data"]["generated_at"] = self.start.replace(hour=15, minute=55).isoformat()
            flow["data"]["trades"][0]["timestamp"] = (decision_time - timedelta(seconds=30)).isoformat()
        # 链报价晚于采集 Heatmap，决策时间必须保留这一先后关系。
        mock.set_time(decision_time.replace(second=20))
        chain = without_simulation_flags(mock.fetch_option_chain("NVDA"))
        path = self.directory / "source.jsonl"
        events = [
            {"kind": "universe", "session_date": self.day.isoformat(), "payload": universe},
            {"kind": "frame", "time": decision_time.isoformat(), "ticker": "NVDA",
             "heatmap": heatmap, "flow": flow, "chain": chain},
        ]
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        return path

    def test_single_recorded_frame_uses_decision_time_and_offline_data(self) -> None:
        source = self._recorded_file()
        report = app.run_replay(self.config, self.day, replay_file=source)
        self.assertEqual(report["source"], "recorded_yhs_jsonl")
        self.assertFalse(report["simulated_data"])
        self.assertEqual(report["observation_steps"], 1)
        self.assertEqual(report["total_trades"], 1)
        self.assertEqual(report["api_quota_used"], 0)
        self.assertEqual(report["mock_request_counts"], {})

    def test_recorded_stage_times_preserve_quote_causality(self) -> None:
        source = self._recorded_file()
        events = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
        event = events[1]
        event["time"] = self.start.replace(minute=35).isoformat()
        event["flow_received_at"] = self.start.replace(minute=35, second=10).isoformat()
        event["chain_received_at"] = self.start.replace(minute=35, second=30).isoformat()
        source.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
        report = app.run_replay(self.config, self.day, replay_file=source)
        self.assertEqual(report["total_trades"], 1)
        with (self.directory / "trades_history.csv").open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["entry_time"], event["chain_received_at"])

    def test_flow_and_chain_budgets_reserved_before_request(self) -> None:
        fixture = MockYHSClient(self.config)
        fixture.set_time(self.start)
        detector = SignalDetector(self.config)
        candidate = next(item for item in detector.filter_pool(fixture.fetch_volume_top()) if item.ticker == "NVDA")
        heatmap = fixture.fetch_heatmap("NVDA")
        trader = PaperTrader(self.config)
        session = app.new_session(self.day)
        durable = []

        class FlowFailure:
            def fetch_flow(inner, *args):
                self.assertEqual(durable[-1]["flow_calls"], 1)
                raise APIError("模拟请求中崩溃")

        with self.assertRaises(APIError):
            app.process_frame(FlowFailure(), detector, trader, candidate, heatmap, self.start, session,
                              offline=True, persist_session=lambda: durable.append(dict(session)))
        self.assertEqual(session["flow_calls"], 1)
        self.assertEqual(len(durable), 1)

        # 使用另一观察时刻，避免同一 Heatmap 样本幂等过滤遮盖链预算。
        later = self.start + timedelta(seconds=1)
        heatmap["data"]["generated_at"] = later.isoformat()
        class ChainFailure:
            def fetch_flow(inner, *args):
                return fixture.fetch_flow("NVDA")

            def fetch_option_chain(inner, *args):
                self.assertEqual(durable[-1]["chain_calls"], 1)
                raise APIError("模拟链请求中崩溃")

        with self.assertRaises(APIError):
            app.process_frame(ChainFailure(), detector, trader, candidate, heatmap, later, session,
                              offline=True, persist_session=lambda: durable.append(dict(session)))
        self.assertEqual(session["chain_calls"], 1)
        self.assertEqual(session["flow_calls"], 2)

    def test_recorded_frame_after_close_is_rejected(self) -> None:
        source = self._recorded_file(frame_hour=16, frame_minute=1)
        # 不能处理收盘后帧，再把退出时间回填为 16:00。
        with self.assertRaises(DataContractError):
            app.run_replay(self.config, self.day, replay_file=source)

    def test_live_timer_cancels_an_in_progress_data_request(self) -> None:
        config = SystemConfig(api_key="test-placeholder", output_dir=self.directory)
        clock = {"now": datetime(2026, 10, 9, 15, 59, tzinfo=eastern_timezone())}
        state = {"timer": None, "client": None}

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"].astimezone(tz) if tz else clock["now"]

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval, self.function = interval, function
                self.started = self.cancelled = False
                self.daemon = False
                state["timer"] = self

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

        class Client(MockYHSClient):
            def __init__(self, settings):
                super().__init__(settings)
                self.close_calls = 0
                state["client"] = self

            def budget_status(self):
                return {"normal_available": 95000, "available": 100000}

            def fetch_heatmap(self, ticker, **kwargs):
                clock["now"] = clock["now"].replace(hour=16, minute=0)
                state["timer"].function()
                raise RequestCancelled("模拟 202 等待在收盘时取消")

            def close(self):
                self.close_calls += 1

        with patch.object(app, "datetime", FakeDateTime), patch.object(app, "YHSClient", Client), \
                patch.object(app.threading, "Timer", FakeTimer), patch.object(app.os_signal, "signal", return_value=None):
            report = app.run_live(config)
        self.assertFalse(report["failed"])
        self.assertEqual(report["exit_reason"], "SESSION_CLOSE")
        self.assertTrue(state["timer"].started)
        self.assertTrue(state["timer"].cancelled)
        self.assertEqual(state["timer"].interval, 60.0)
        self.assertGreaterEqual(state["client"].close_calls, 2)
        self.assertEqual(state["client"].calls.get("volume_top"), 1)

    def test_failed_daily_ranking_claim_is_not_repeated_on_restart(self) -> None:
        config = SystemConfig(api_key="test-placeholder", output_dir=self.directory)
        frozen = datetime(2026, 10, 9, 15, 59, tzinfo=eastern_timezone())
        calls = {"volume": 0}

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen.astimezone(tz) if tz else frozen

        class FakeTimer:
            def __init__(self, *args):
                self.daemon = False

            def start(self):
                pass

            def cancel(self):
                pass

        class Client(MockYHSClient):
            def budget_status(self):
                return {"normal_available": 95000, "available": 100000}

            def fetch_volume_top(self, limit=100, **kwargs):
                calls["volume"] += 1
                raise APIError("模拟榜单请求失败")

        with patch.object(app, "datetime", FakeDateTime), patch.object(app, "YHSClient", Client), \
                patch.object(app.threading, "Timer", FakeTimer), patch.object(app.os_signal, "signal", return_value=None):
            first = app.run_live(config)
            second = app.run_live(config)
        self.assertTrue(first["failed"])
        self.assertEqual(second["exit_reason"], "EMPTY_UNIVERSE")
        self.assertEqual(calls["volume"], 1)
        session = json.loads((self.directory / "sessions" / f"{self.day}.json").read_text(encoding="utf-8"))
        self.assertTrue(session["volume_claimed"])


if __name__ == "__main__":
    unittest.main()
