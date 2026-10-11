"""客户端离线验证：无网络、无密钥、无真实额度消耗。"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import SystemConfig
from yhs_client import APIError, AuthenticationError, QuotaExceeded, RequestCancelled, SchemaError, YHSClient


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b'{"data": []}'):
        super().__init__(body)
        self.status, self.headers = status, headers or {}

    def getcode(self) -> int:
        return self.status


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory(prefix="client_test_", dir=Path(__file__).resolve().parent)
        self.root = Path(self.folder.name)
        self.clock = FakeClock()
        self.starts: list[float] = []
        self.urls: list[str] = []

    def tearDown(self) -> None:
        self.folder.cleanup()

    def build(self, responses: list, **overrides) -> YHSClient:
        self.config = SystemConfig(api_key="OFFLINE_MOCK", output_dir=self.root, **overrides)
        source = iter(responses)
        def opener(req, timeout):
            self.starts.append(self.clock.now())
            self.urls.append(req.full_url)
            value = next(source)
            if isinstance(value, BaseException):
                raise value
            return FakeResponse(*value)
        return YHSClient(self.config, self.clock.sleep, self.clock.now, opener)

    def test_202_and_errors_are_free_then_success_costs_one(self) -> None:
        client = self.build([(202,), (202,), (503,), (429, {"Retry-After": "4"}), (200,)])
        client.fetch_flow("SPY")
        status = client.budget_status()
        self.assertEqual((status["charged_200"], status["uncertain"], status["pending"]), (1, 0, 0))
        self.assertIn("tickers=SPY", self.urls[0])
        self.assertIn("min_premium_usd=50000", self.urls[0])

    def test_rolling_50_rpm_and_1_2_second_spacing(self) -> None:
        client = self.build([(200,)] * 65)
        for _ in range(65):
            client.fetch_heatmap("AAPL")
        self.assertTrue(all(b-a >= 1.2-0.0001 for a,b in zip(self.starts,self.starts[1:])))
        self.assertTrue(all(sum(t-60 < s <= t for s in self.starts) <= 50 for t in self.starts))
        self.assertEqual(client.budget_status()["charged_200"], 65)

    def test_network_unknown_retains_safety_budget(self) -> None:
        client = self.build([URLError("offline simulated loss"), (200,)])
        client.fetch_heatmap("SPY")
        status = client.budget_status()
        self.assertEqual(status["uncertain"], 1)
        self.assertEqual(status["charged_200"], 1)

    def test_normal_reserve_and_essential_hard_limit(self) -> None:
        client = self.build([(200,)]*4, monthly_quota=4, quota_reserve=1)
        for _ in range(3):
            client.fetch_heatmap("SPY")
        with self.assertRaises(QuotaExceeded):
            client.fetch_heatmap("SPY")
        client.fetch_heatmap("SPY", category="essential")
        with self.assertRaises(QuotaExceeded):
            client.fetch_heatmap("SPY", category="essential")
        self.assertEqual(client.budget_status()["available"], 0)
        self.assertEqual(len(self.starts), 4)

    def test_zero_header_stops_202_but_discover_is_free(self) -> None:
        client = self.build([(202, {"X-Quota-Remaining":"0"}), (200,)])
        with self.assertRaises(QuotaExceeded):
            client.fetch_heatmap("SPY")
        client.discover()
        self.assertEqual(client.budget_status()["charged_200"], 0)
        self.assertEqual(len(self.starts), 2)

    def test_discover_monthly_remaining_without_headers(self) -> None:
        client = self.build([(200, {}, b'{"data":{"quota":{"monthly_remaining":0}}}')])
        client.discover()
        with self.assertRaises(QuotaExceeded):
            client.fetch_heatmap("SPY")

    def test_bad_success_json_still_counts(self) -> None:
        client = self.build([(200, {}, b"malformed")])
        with self.assertRaises(SchemaError):
            client.fetch_heatmap("SPY")
        self.assertEqual(client.budget_status()["charged_200"], 1)

    def test_401_402_403_do_not_retry_or_charge(self) -> None:
        for code in (401,402,403):
            with self.subTest(status=code):
                self.starts.clear()
                client = self.build([(code,)])
                with self.assertRaises(AuthenticationError):
                    client.fetch_heatmap("SPY")
                self.assertEqual(len(self.starts), 1)
                self.assertEqual(client.budget_status()["charged_200"], 0)

    def test_429_quota_exhausted_without_header_breaks(self) -> None:
        client = self.build([(429, {}, b'{"error":{"code":"QUOTA_EXHAUSTED"}}')])
        with self.assertRaises(QuotaExceeded):
            client.fetch_heatmap("SPY")
        self.assertEqual(client.budget_status()["server_quota_remaining"], 0)

    def test_rate_zero_waits_for_header_reset(self) -> None:
        client = self.build([])
        reset = int(client._now()+30)
        source = iter([(200, {"X-RateLimit-Remaining":"0", "X-RateLimit-Reset":str(reset)}), (200,)])
        def opener(req, timeout):
            self.starts.append(self.clock.now())
            return FakeResponse(*next(source))
        client._opener = opener
        client.fetch_heatmap("SPY")
        client.fetch_heatmap("SPY")
        self.assertGreaterEqual(self.starts[1]-self.starts[0], 29)

    def test_429_respects_reset_even_without_remaining_header(self) -> None:
        client = self.build([])
        reset = int(client._now()+30)
        source = iter([(429, {"X-RateLimit-Reset":str(reset), "Retry-After":"1"}), (200,)])
        def opener(req, timeout):
            self.starts.append(self.clock.now())
            return FakeResponse(*next(source))
        client._opener = opener
        client.fetch_heatmap("SPY")
        self.assertGreaterEqual(self.starts[1]-self.starts[0], 29)

    def test_client_restart_keeps_monthly_counter(self) -> None:
        client = self.build([(200,)])
        client.fetch_heatmap("SPY")
        restarted = YHSClient(self.config, self.clock.sleep, self.clock.now, lambda *a, **k: FakeResponse(200))
        self.assertEqual(restarted.budget_status()["charged_200"], 1)

    def test_cancel_free_202_loop(self) -> None:
        client = self.build([(202,)])
        def cancel_sleep(seconds):
            client.close()
            self.clock.sleep(seconds)
        client._sleep_fn = cancel_sleep
        with self.assertRaises(RequestCancelled):
            client.fetch_heatmap("SPY")
        self.assertEqual(client.budget_status()["pending"], 0)

    def test_shared_durable_in_flight_cap_includes_metadata(self) -> None:
        client = self.build([(200,)])
        with client._state_transaction() as state:
            month, _ = client._month(state)
            for n in range(3):
                state["pending"][str(n)] = {"month":month,"pid":os.getpid(),"billable":False,"path":"/discover"}
        def cancel_sleep(seconds):
            client.close()
        client._sleep_fn = cancel_sleep
        with self.assertRaises(RequestCancelled):
            client.discover()
        self.assertEqual(len(self.starts), 0)

    def test_in_flight_slot_remains_until_response_body_is_closed(self) -> None:
        client = self.build([])
        snapshots = []
        class BodyCheckedResponse(FakeResponse):
            def read(response, amount=-1):
                snapshots.append(client.budget_status())
                return super().read(amount)
            def close(response):
                snapshots.append(client.budget_status())
                super().close()
        client._opener = lambda *a, **k: BodyCheckedResponse(200)
        client.fetch_heatmap("SPY")
        self.assertEqual([row["in_flight"] for row in snapshots], [1,1])
        self.assertEqual([row["charged_200"] for row in snapshots], [1,1])
        self.assertEqual([row["pending"] for row in snapshots], [0,0])
        self.assertEqual(client.budget_status()["in_flight"],0)

    def test_202_exponential_backoff_is_capped(self) -> None:
        client = self.build([(202,)]*8+[(200,)], initial_backoff_seconds=1.2,max_backoff_seconds=4.8)
        client.fetch_heatmap("SPY")
        intervals = [b-a for a,b in zip(self.starts,self.starts[1:])]
        self.assertAlmostEqual(intervals[0],1.2,places=4)
        self.assertAlmostEqual(intervals[1],2.4,places=4)
        self.assertTrue(all(abs(v-4.8)<0.0001 for v in intervals[2:]))
        self.assertEqual(client.budget_status()["charged_200"],1)

    def test_four_threads_and_client_instances_share_three_in_flight_limit(self) -> None:
        config = SystemConfig(api_key="OFFLINE_MOCK", output_dir=self.root)
        lock, three_active, release = threading.Lock(), threading.Event(), threading.Event()
        active = peak = 0
        failures = []
        class HeldResponse(FakeResponse):
            def read(response, amount=-1):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak,active)
                    if active == 3:
                        three_active.set()
                try:
                    if not release.wait(10):
                        raise TimeoutError("offline concurrency test release timed out")
                    return super().read(amount)
                finally:
                    with lock:
                        active -= 1
        clients = [YHSClient(config, opener=lambda *a, **k: HeldResponse(200)) for _ in range(4)]
        def work(client):
            try:
                client.fetch_heatmap("SPY")
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=work,args=(client,),daemon=True) for client in clients]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(three_active.wait(6),"three independent requests should enter response body")
            self.assertEqual(clients[0].budget_status()["in_flight"],3)
        finally:
            release.set()
            for thread in threads:
                thread.join(6)
            for client in clients:
                client.close()
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(peak,3)
        self.assertEqual(clients[0].budget_status()["charged_200"],4)
        self.assertEqual(clients[0].budget_status()["in_flight"],0)

    def test_abandoned_process_pending_recovered_as_uncertain(self) -> None:
        client = self.build([])
        with client._state_transaction() as state:
            month, _ = client._month(state)
            state["pending"]["dead"] = {"month":month,"pid":0,"billable":True,"path":"/fake"}
        status = client.budget_status()
        self.assertEqual(status["uncertain"], 1)
        self.assertEqual(status["in_flight"], 0)

    def test_corrupt_state_is_not_silently_reset(self) -> None:
        client = self.build([])
        client.state_path.write_text("broken",encoding="utf-8")
        with self.assertRaises(APIError):
            client.budget_status()
        self.assertEqual(client.state_path.read_text(encoding="utf-8"), "broken")

    def test_chain_uses_verified_route_expiration_and_one_page(self) -> None:
        client = self.build([(200,{},b'{"data":{"contracts":[]},"_meta":{"next_cursor":"more"}}')])
        client.fetch_option_chain("NVDA", "2026-10-16")
        self.assertEqual(len(self.starts), 1)
        self.assertIn("/v1/options/chain-snapshot/NVDA?expiration=2026-10-16&limit=400", self.urls[0])
        with self.assertRaises(APIError):
            client.fetch_option_chain("NVDA")

    def test_volume_top_has_no_unsupported_limit_query(self) -> None:
        client = self.build([(200,)])
        client.fetch_volume_top(100)
        self.assertTrue(self.urls[0].endswith("/v1/market/options-volume-rank"))
        self.assertNotIn("?",self.urls[0])

    def test_external_and_plain_http_urls_rejected(self) -> None:
        client = self.build([])
        with self.assertRaises(ValueError):
            client.get("https://other.example/private")
        with self.assertRaises(ValueError):
            YHSClient(SystemConfig(api_base_url="http://api.example",output_dir=self.root/"invalid"))

    def test_month_rollover_resets_local_success_and_server_header(self) -> None:
        client = self.build([(200, {"X-Quota-Remaining":"0", "X-Quota-Reset-At":"2026-11-01T00:00:00Z"}), (200,)])
        client._wall_offset = datetime(2026,10,31,23,59,0,tzinfo=timezone.utc).timestamp()
        client.fetch_heatmap("SPY")
        self.assertEqual(client.quota_remaining,0)
        self.clock.value += 120
        client.fetch_heatmap("SPY")
        status = client.budget_status()
        self.assertEqual(status["month"],"2026-11")
        self.assertEqual(status["charged_200"],1)


if __name__ == "__main__":
    unittest.main()
