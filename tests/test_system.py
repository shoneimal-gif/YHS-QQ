"""离线行为验证：数据门槛、信号因果性、纸面资金及全日回放。"""
from __future__ import annotations

import csv
import importlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

from config import SystemConfig, eastern_timezone
from detector import Candidate, DataContractError, Signal, SignalDetector, parse_heatmap, select_entry_quote
from paper_trader import CSV_FIELDS, PaperStateError, PaperTrader


class SystemCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=SYSTEM_DIR / "tests", prefix=".run-", ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = SystemConfig(output_dir=self.directory)
        self.now = datetime(2026, 10, 9, 9, 35, tzinfo=eastern_timezone())
        self.detector = SignalDetector(self.config)

    def candidate(self, bias: str = "BULL", ticker: str = "TEST") -> Candidate:
        return Candidate(ticker, 100.0, 100000, 100000, 5.0, 0.5, bias, "stock", 2.0)

    def heatmap(self, *, spot: float = 100.1, now: datetime | None = None,
                positive: float = 100.0, negative: float = 103.0,
                simulated: bool = True) -> dict:
        stamp = now or self.now
        return {"simulated": simulated, "data": {
            "ticker": "TEST", "spot_usd": spot, "generated_at": stamp.isoformat(),
            "session_date_et": stamp.astimezone(eastern_timezone()).date().isoformat(),
            "cells": [
                {"expiration": "2026-10-16", "strike_usd": positive, "net_dealer_gex_usd": 1000000},
                {"expiration": "2026-10-16", "strike_usd": negative, "net_dealer_gex_usd": -800000},
            ],
        }}

    def signal(self, identity: str = "paper1", action: str = "BUY_CALL") -> Signal:
        right = "C" if action == "BUY_CALL" else "P"
        return Signal("TEST", action, 100.0, 100.0, 100.0, 103.0,
                      "2026-10-16", self.now, f"TEST261016{right}00100000", identity)

    def quote(self, *, right: str = "C", mid: float = 10.0, gamma: float = 0.0,
              timestamp: datetime | None = None) -> dict:
        return {"contract_symbol": f"TEST261016{right}00100000", "strike": 100,
                "expiration": "2026-10-16", "right": right, "bid": mid - 0.1,
                "ask": mid + 0.1, "mid": mid, "delta": 0.5, "gamma": gamma,
                "timestamp": (timestamp or self.now).isoformat()}

    def pool_row(self, ticker: str = "TEST", **updates) -> dict:
        row = {"ticker": ticker, "asset_type": "stock", "spot_usd": 100,
               "total_volume": 1000, "total_oi": 1000, "oi_change_pct": 0,
               "pcr": 0.5}
        row.update(updates)
        return row

    def flow_row(self, **updates) -> dict:
        row = {"ticker": "TEST", "timestamp": self.now.isoformat(), "right": "C",
               "side": "ASK", "premium_usd": 50000, "volume": 1000,
               "open_interest": 1000, "expiration": "2026-10-16",
               "contract_symbol": "TEST261016C00100000"}
        row.update(updates)
        return row

    def evaluate(self, **updates) -> Signal | None:
        return SignalDetector(self.config).evaluate_full_signal(
            self.candidate(), self.heatmap(), {"data": {"trades": [self.flow_row(**updates)]}}, self.now)


class PoolTests(SystemCase):
    def test_etfs_only_whitelist_and_indexes_removed(self) -> None:
        rows = [self.pool_row("SPY", asset_type="etf"),
                self.pool_row("QQQ", asset_type="etf"),
                self.pool_row("IWM", asset_type="etf"),
                self.pool_row("SPX", asset_type="stock"),
                self.pool_row("TEST")]
        self.assertEqual({c.ticker for c in self.detector.filter_pool({"data": {"items": rows}})}, {"SPY", "QQQ", "TEST"})

    def test_unknown_type_requires_explicit_symbol_mapping(self) -> None:
        row = self.pool_row()
        del row["asset_type"]
        self.assertEqual(self.detector.filter_pool([row]), [])
        self.config.symbol_types["TEST"] = "stock"
        self.assertEqual([c.ticker for c in self.detector.filter_pool([row])], ["TEST"])

    def test_twenty_dollar_boundary(self) -> None:
        rows = [self.pool_row("LOW", spot_usd=19.99), self.pool_row("BOUND", spot_usd=20)]
        self.assertEqual([c.ticker for c in self.detector.filter_pool(rows)], ["BOUND"])

    def test_oi_percent_is_fraction_and_threshold_inclusive(self) -> None:
        rows = [self.pool_row("PASS", total_volume=50, oi_change_pct=0.05),
                self.pool_row("FAIL", total_volume=50, oi_change_pct=0.049)]
        result = self.detector.filter_pool(rows)
        self.assertEqual([c.ticker for c in result], ["PASS"])
        self.assertAlmostEqual(result[0].oi_change_pct, 5.0)

    def test_volume_oi_threshold_is_inclusive(self) -> None:
        rows = [self.pool_row("PASS", total_volume=650), self.pool_row("FAIL", total_volume=649)]
        self.assertEqual([c.ticker for c in self.detector.filter_pool(rows)], ["PASS"])

    def test_top100_is_selected_before_cleaning_without_backfill(self) -> None:
        rows = [self.pool_row(f"BAD{i}", asset_type="etf", total_volume=10000 - i) for i in range(100)]
        rows.append(self.pool_row("LOWRANK", total_volume=4000, total_oi=5000))
        self.assertEqual(self.detector.filter_pool(rows), [])

    def test_pcr_bias_thresholds(self) -> None:
        rows = [self.pool_row("BULL", pcr=0.6), self.pool_row("BEAR", pcr=1.6), self.pool_row("NEUTRAL", pcr=1.0)]
        self.assertEqual({c.ticker: c.bias for c in self.detector.filter_pool(rows)}, {"BULL": "BULL", "BEAR": "BEAR", "NEUTRAL": "NEUTRAL"})


class StructureTests(SystemCase):
    def test_official_cells_select_net_signed_nodes_within_one_expiry(self) -> None:
        payload = self.heatmap(simulated=False)
        payload["data"]["generated_at"] = (self.now - timedelta(minutes=5)).isoformat()
        payload["data"]["cells"].extend([
            {"expiration": "2026-10-16", "strike_usd": 99, "net_dealer_gex_usd": 300000},
            {"expiration": "2026-10-16", "strike_usd": 104, "net_dealer_gex_usd": -100000},
            {"expiration": "2026-12-18", "strike_usd": 80, "net_dealer_gex_usd": -90000000},
        ])
        snap = parse_heatmap(payload, "TEST", self.now, self.config)
        self.assertEqual((snap.positive_strike, snap.negative_strike, snap.expiration), (100, 103, "2026-10-16"))
        self.assertEqual(snap.timestamp, self.now - timedelta(minutes=1))

    def test_unfinished_official_five_minute_segment_is_rejected(self) -> None:
        with self.assertRaises(DataContractError):
            parse_heatmap(self.heatmap(simulated=False), "TEST", self.now + timedelta(minutes=4, seconds=59), self.config)

    def test_stale_snapshot_wrong_day_or_missing_cells_rejected(self) -> None:
        cases = []
        stale = self.heatmap()
        stale["data"]["generated_at"] = (self.now - timedelta(seconds=901)).isoformat()
        cases.append(stale)
        wrong_day = self.heatmap()
        wrong_day["data"]["session_date_et"] = "2026-10-08"
        cases.append(wrong_day)
        legacy = self.heatmap()
        del legacy["data"]["cells"]
        legacy["data"]["gex_by_strike"] = {"100": 123}
        cases.append(legacy)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(DataContractError):
                parse_heatmap(payload, "TEST", self.now, self.config)

    def test_opposite_signs_cannot_be_combined_across_expirations(self) -> None:
        payload = self.heatmap()
        payload["data"]["cells"][1]["expiration"] = "2026-10-12"
        with self.assertRaises(DataContractError):
            parse_heatmap(payload, "TEST", self.now, self.config)

    def test_call_springboard_put_cliff_and_conflicting_pcr(self) -> None:
        call = self.detector.inspect_structure(self.candidate(), self.heatmap(), self.now)
        self.assertEqual((call.action, call.structure), ("BUY_CALL", "SPRINGBOARD"))
        put = self.detector.inspect_structure(self.candidate("BEAR"), self.heatmap(spot=100, positive=100, negative=97), self.now)
        self.assertEqual((put.action, put.structure), ("BUY_PUT", "CLIFF"))
        self.assertIsNone(self.detector.inspect_structure(self.candidate("BEAR"), self.heatmap(), self.now))

    def test_fourth_independent_touch_rejected_but_duplicate_time_not_counted(self) -> None:
        detector = SignalDetector(self.config, self.directory / "detector_state.json")
        at = self.now
        for expected in range(1, 5):
            payload = self.heatmap(spot=100.1, now=at)
            result = detector.inspect_structure(self.candidate(), payload, at)
            self.assertEqual(next(iter(detector.touches.values()))["count"], expected)
            self.assertEqual(result is None, expected == 4)
            detector.inspect_structure(self.candidate(), payload, at)
            self.assertEqual(next(iter(detector.touches.values()))["count"], expected)
            if expected < 4:
                at += timedelta(minutes=1)
                detector.inspect_structure(self.candidate(), self.heatmap(spot=101, now=at), at)
                at += timedelta(minutes=1)
        recovered = SignalDetector(self.config, self.directory / "detector_state.json")
        self.assertEqual(next(iter(recovered.touches.values()))["count"], 4)
        self.assertIsNone(recovered.inspect_structure(self.candidate(), self.heatmap(spot=100.1, now=at), at))

    def test_near_node_continuous_observations_are_single_touch(self) -> None:
        for minute in range(4):
            at = self.now + timedelta(minutes=minute)
            self.assertIsNotNone(self.detector.inspect_structure(self.candidate(), self.heatmap(spot=100.2, now=at), at))
        self.assertEqual(next(iter(self.detector.touches.values()))["count"], 1)


class FlowAndQuoteTests(SystemCase):
    def test_flow_exact_15_minute_boundary_is_included(self) -> None:
        self.assertIsNotNone(self.evaluate(timestamp=(self.now - timedelta(minutes=15)).isoformat()))
        self.assertIsNone(self.evaluate(timestamp=(self.now - timedelta(minutes=15, seconds=1)).isoformat()))
        self.assertIsNone(self.evaluate(timestamp=(self.now + timedelta(seconds=1)).isoformat()))

    def test_ask_premium_and_daily_volume_boundaries(self) -> None:
        self.assertIsNotNone(self.evaluate())
        self.assertIsNone(self.evaluate(side="BID"))
        self.assertIsNone(self.evaluate(premium_usd=49999.99))
        self.assertIsNone(self.evaluate(volume=999))
        self.assertIsNotNone(self.evaluate(side="ABOVE_ASK", premium_usd=50000, volume=1000))

    def test_block_contract_count_cannot_replace_daily_contract_volume(self) -> None:
        row = self.flow_row(contracts=9000)
        del row["volume"]
        result = self.detector.evaluate_full_signal(self.candidate(), self.heatmap(), {"trades": [row]}, self.now)
        self.assertIsNone(result)

    def test_flow_right_and_expiration_must_match_structure(self) -> None:
        self.assertIsNone(self.evaluate(right="P"))
        self.assertIsNone(self.evaluate(expiration="2026-10-12"))
        self.assertIsNone(self.evaluate(ticker="OTHER"))

    def test_chain_requires_source_quote_time_and_exact_flow_contract(self) -> None:
        row = self.quote()
        signal = self.signal()
        self.assertIsNotNone(select_entry_quote({"contracts": [row]}, signal, self.now, self.config))
        for update in ({"timestamp": None}, {"timestamp": (self.now - timedelta(seconds=901)).isoformat()},
                       {"timestamp": (self.now + timedelta(seconds=1)).isoformat()},
                       {"contract_symbol": "TEST261016C00101000"}, {"right": "P"}, {"expiration": "2026-10-12"}):
            candidate = dict(row, **update)
            with self.subTest(update=update):
                self.assertIsNone(select_entry_quote({"contracts": [candidate]}, signal, self.now, self.config))

    def test_simulated_chain_is_offline_only(self) -> None:
        row = self.quote()
        row["simulated"] = True
        payload = {"simulated": True, "data": {"contracts": [row]}}
        self.assertIsNone(select_entry_quote(payload, self.signal(), self.now, self.config))
        selected = select_entry_quote(payload, self.signal(), self.now, self.config, offline=True)
        self.assertTrue(selected["simulated"])


class PaperTraderTests(SystemCase):
    def trader(self, **changes) -> PaperTrader:
        return PaperTrader(SystemConfig(output_dir=self.directory, **changes))

    def test_put_delta_negative_and_mark_anchored_to_entry(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(action="BUY_PUT"), self.quote(right="P", gamma=0.04), self.now)
        self.assertEqual(position.delta, -0.5)
        trader.mark_to_market("TEST", 98, self.now + timedelta(minutes=1))
        self.assertAlmostEqual(position.last_mark, 11.08)
        trader.mark_to_market("TEST", 96, self.now + timedelta(minutes=2))
        self.assertAlmostEqual(position.last_mark, 12.32)
        trader.mark_to_market("TEST", 100, self.now + timedelta(minutes=3))
        self.assertAlmostEqual(position.last_mark, 10)

    def test_slippage_cash_and_closed_pnl_reconcile(self) -> None:
        trader = self.trader()
        position = trader.open_position(self.signal(), self.quote(), self.now)
        self.assertAlmostEqual(position.entry_option_price, 10.2)
        self.assertAlmostEqual(trader.cash, 98980)
        trader.close_all(self.now + timedelta(minutes=1))
        self.assertAlmostEqual(position.exit_option_price, 9.8)
        self.assertAlmostEqual(position.realized_pnl, -40)
        self.assertAlmostEqual(trader.cash, 99960)
        self.assertAlmostEqual(trader.statistics()["equity"], 99960)

    def test_35_percent_moves_stop_to_breakeven_but_observed_fill_can_be_lower(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        self.assertEqual(trader.mark_to_market("TEST", 107, self.now + timedelta(minutes=1)), [])
        self.assertEqual(position.stop_roi, 0)
        closed = trader.mark_to_market("TEST", 99, self.now + timedelta(minutes=2))
        self.assertEqual(closed[0].exit_reason, "BREAKEVEN_STOP")
        self.assertAlmostEqual(position.realized_roi_pct, -5)
        self.assertTrue(position.direction_correct)  # 峰值达到15%，即使最后亏损退出。
        self.assertEqual(trader.statistics()["directional_accuracy_pct"], 100)

    def test_60_percent_trailing_uses_premium_drawdown_not_roi_points(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        trader.mark_to_market("TEST", 112, self.now + timedelta(minutes=1))
        self.assertAlmostEqual(position.peak_roi_pct, 60)
        self.assertAlmostEqual(position.stop_roi, 0.28)
        self.assertEqual(trader.mark_to_market("TEST", 110, self.now + timedelta(minutes=2)), [])
        closed = trader.mark_to_market("TEST", 105, self.now + timedelta(minutes=3))
        self.assertEqual(closed[0].exit_reason, "TRAILING_STOP")
        self.assertAlmostEqual(closed[0].realized_roi_pct, 25)
        self.assertAlmostEqual(trader.cash, 100250)

    def test_alternative_trailing_roi_points(self) -> None:
        trader = self.trader(slippage_pct=0, trailing_mode="roi")
        position = trader.open_position(self.signal(), self.quote(), self.now)
        trader.mark_to_market("TEST", 112, self.now + timedelta(minutes=1))
        self.assertAlmostEqual(position.stop_roi, 0.40)

    def test_150_percent_hard_take_profit(self) -> None:
        trader = self.trader(slippage_pct=0)
        trader.open_position(self.signal(), self.quote(), self.now)
        closed = trader.mark_to_market("TEST", 130, self.now + timedelta(minutes=1))
        self.assertEqual(closed[0].exit_reason, "HARD_TAKE_PROFIT")
        self.assertAlmostEqual(closed[0].realized_pnl, 1500)
        self.assertAlmostEqual(trader.cash, 101500)

    def test_hard_stop_and_direction_peak_threshold(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        trader.mark_to_market("TEST", 102, self.now + timedelta(minutes=1))
        closed = trader.mark_to_market("TEST", 92, self.now + timedelta(minutes=2))
        self.assertEqual(closed[0].exit_reason, "HARD_STOP")
        self.assertAlmostEqual(position.peak_roi_pct, 10)
        self.assertFalse(position.direction_correct)
        self.assertLessEqual(position.max_adverse_roi_pct, -40)

    def test_eod_uses_last_observation_and_marks_stale(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        observed_at = self.now + timedelta(minutes=1)
        trader.mark_to_market("TEST", 101, observed_at)
        trader.close_all(self.now + timedelta(hours=2))
        self.assertEqual(position.last_time, observed_at)
        self.assertEqual(position.exit_spot, 101)
        self.assertEqual(position.exit_option_price, 10.5)
        self.assertTrue(position.stale)
        self.assertEqual(position.exit_reason, "SESSION_CLOSE")

    def test_source_observation_time_and_decision_exit_time_are_separate(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        observed_at = self.now + timedelta(minutes=4)
        decided_at = self.now + timedelta(minutes=5)
        closed = trader.mark_to_market("TEST", 130, decided_at, observed_at=observed_at)
        self.assertEqual(closed[0].exit_reason, "HARD_TAKE_PROFIT")
        self.assertEqual(position.last_time, observed_at)
        self.assertEqual(position.exit_time, decided_at)
        self.assertFalse(position.stale)

    def test_source_observations_must_advance_and_not_come_from_future(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        observed_at = self.now + timedelta(minutes=1)
        trader.mark_to_market("TEST", 101, self.now + timedelta(minutes=2), observed_at=observed_at)
        for source_time in (self.now, observed_at, self.now + timedelta(minutes=4)):
            self.assertEqual(trader.mark_to_market("TEST", 130, self.now + timedelta(minutes=3), observed_at=source_time), [])
            self.assertEqual(position.last_time, observed_at)
            self.assertAlmostEqual(position.last_mark, 10.5)
            self.assertEqual(position.status, "OPEN")

    def test_quote_validation_and_risk_gates(self) -> None:
        trader = self.trader(max_trade_cost_usd=1500)
        for update in ({"contract_symbol": "FAKE"}, {"right": "P"}, {"strike": 101},
                       {"expiration": "2026-10-12"}, {"bid": 11, "ask": 10},
                       {"timestamp": (self.now - timedelta(hours=1)).isoformat()}):
            with self.subTest(update=update):
                self.assertIsNone(trader.open_position(self.signal(), dict(self.quote(), **update), self.now))
        self.assertIsNone(trader.open_position(self.signal(), self.quote(mid=20), self.now))
        self.assertEqual(trader.cash, self.config.initial_cash)
        self.assertEqual(trader.open_positions, [])

    def test_state_restart_deduplication_and_idempotent_csv(self) -> None:
        trader = self.trader(slippage_pct=0)
        position = trader.open_position(self.signal(), self.quote(), self.now)
        self.assertIs(trader.open_position(self.signal(), self.quote(), self.now), position)
        restarted = self.trader(slippage_pct=0)
        self.assertEqual(len(restarted.open_positions), 1)
        restarted.close_all(self.now + timedelta(minutes=1))
        output = restarted.export_trades_csv()
        self.assertEqual(output.name, "trades_history.csv")
        restarted.export_trades_csv()
        final = self.trader(slippage_pct=0)
        self.assertIsNone(final.open_position(self.signal(), self.quote(), self.now))
        with output.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            expected_prefix = ["position_id", "ticker", "action", "contract_symbol", "strike",
                               "entry_time", "entry_spot", "entry_option_price", "exit_time",
                               "exit_spot", "exit_option_price", "realized_pnl", "realized_roi_pct",
                               "peak_roi_pct", "max_adverse_roi_pct", "direction_correct", "exit_reason"]
            self.assertEqual(reader.fieldnames[:len(expected_prefix)], expected_prefix)
            self.assertEqual(reader.fieldnames, CSV_FIELDS)
            self.assertEqual(len(list(reader)), 1)
        self.assertEqual(final.statistics()["total_trades"], 1)
        self.assertAlmostEqual(final.cash, 100000)

    def test_corrupt_or_inconsistent_state_does_not_reset_cash(self) -> None:
        trader = self.trader()
        trader.open_position(self.signal(), self.quote(), self.now)
        original = json.loads(trader.state_path.read_text(encoding="utf-8"))
        for payload in ("{broken", json.dumps(dict(original, cash=123))):
            trader.state_path.write_text(payload, encoding="utf-8")
            with self.assertRaises(PaperStateError):
                self.trader()
            self.assertEqual(trader.state_path.read_text(encoding="utf-8"), payload)

    def test_state_save_failure_rolls_back_cash_and_positions(self) -> None:
        trader = self.trader()
        with patch.object(trader, "_persist", side_effect=PaperStateError("模拟磁盘写入失败")):
            with self.assertRaises(PaperStateError):
                trader.open_position(self.signal(), self.quote(), self.now)
        self.assertEqual(trader.cash, self.config.initial_cash)
        self.assertEqual(trader.open_positions, [])

    def test_stop_new_entries_blocks_late_signals_after_session_close(self) -> None:
        trader = self.trader(slippage_pct=0)
        trader.open_position(self.signal(), self.quote(), self.now)
        trader.stop_new_entries()
        trader.close_all(self.now + timedelta(minutes=1))
        closed_cash = trader.cash
        self.assertIsNone(trader.open_position(self.signal(identity="late-entry"), self.quote(), self.now))
        self.assertEqual(trader.open_positions, [])
        self.assertEqual(trader.statistics()["total_trades"], 1)
        self.assertEqual(trader.cash, closed_cash)
        trader.stop_new_entries()  # 重复停止可以安全调用。

    def test_generic_close_all_does_not_disable_new_entries(self) -> None:
        trader = self.trader(slippage_pct=0)
        trader.open_position(self.signal(), self.quote(), self.now)
        trader.close_all(self.now + timedelta(minutes=1))
        second = trader.open_position(self.signal(identity="second-entry"), self.quote(), self.now)
        self.assertIsNotNone(second)
        self.assertEqual(len(trader.open_positions), 1)

    def test_entry_transaction_and_session_timer_close_are_serialized(self) -> None:
        trader = self.trader(slippage_pct=0)
        saving_entry = threading.Event()
        release_entry = threading.Event()
        closing_started = threading.Event()
        closing_finished = threading.Event()
        errors = []
        original_persist = trader._persist

        def paused_persist() -> None:
            if threading.current_thread().name == "entry-test":
                saving_entry.set()
                if not release_entry.wait(2):
                    raise PaperStateError("测试开仓等待超时")
            original_persist()

        def enter() -> None:
            try:
                trader.open_position(self.signal(), self.quote(), self.now)
            except Exception as exc:
                errors.append(exc)

        def close() -> None:
            closing_started.set()
            try:
                trader.close_all(self.now + timedelta(minutes=1))
            except Exception as exc:
                errors.append(exc)
            finally:
                closing_finished.set()

        entry_thread = threading.Thread(target=enter, name="entry-test", daemon=True)
        close_thread = threading.Thread(target=close, name="session-close-test", daemon=True)
        with patch.object(trader, "_persist", side_effect=paused_persist):
            entry_thread.start()
            try:
                self.assertTrue(saving_entry.wait(1))
                close_thread.start()
                self.assertTrue(closing_started.wait(1))
                self.assertFalse(closing_finished.wait(0.05))
            finally:
                release_entry.set()
                entry_thread.join(2)
                if close_thread.ident is not None:
                    close_thread.join(2)
        self.assertFalse(entry_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(trader.statistics()["total_trades"], 1)
        self.assertEqual(trader.statistics()["open_positions"], 0)
        self.assertAlmostEqual(trader.cash, 100000)
        self.assertEqual(self.trader(slippage_pct=0).statistics()["total_trades"], 1)


class ReplayIntegrationTests(SystemCase):
    @unittest.skipUnless((SYSTEM_DIR / "main.py").exists(), "主入口尚未写入；完成后自动启用全日回放检查")
    def test_full_day_offline_replay_all_exit_paths_and_repeat_no_duplicate(self) -> None:
        main = importlib.import_module("main")
        with patch("urllib.request.urlopen", side_effect=AssertionError("离线回放禁止联网")), patch("socket.create_connection", side_effect=AssertionError("离线回放禁止联网")):
            report = main.run_replay(self.config, date(2026, 10, 9), seed=7)
        self.assertIsInstance(report, dict)
        output = self.directory / "trades_history.csv"
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["exit_reason"] for row in rows}, {"HARD_TAKE_PROFIT", "BREAKEVEN_STOP", "TRAILING_STOP", "HARD_STOP", "SESSION_CLOSE"})
        aapl = next(row for row in rows if row["ticker"] == "AAPL")
        self.assertGreaterEqual(float(aapl["peak_roi_pct"]), 15)
        self.assertEqual(aapl["direction_correct"], "True")
        first_ids = {row["position_id"] for row in rows}
        with patch("urllib.request.urlopen", side_effect=AssertionError("离线回放禁止联网")), patch("socket.create_connection", side_effect=AssertionError("离线回放禁止联网")):
            with self.assertRaisesRegex(RuntimeError, "已有回放账本"):
                main.run_replay(self.config, date(2026, 10, 9), seed=7)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            repeated = list(csv.DictReader(stream))
        self.assertEqual(len(repeated), 5)
        self.assertEqual({row["position_id"] for row in repeated}, first_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
