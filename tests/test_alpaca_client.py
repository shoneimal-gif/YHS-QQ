"""Alpaca REST 离线契约测试：全部网络响应和凭据均为合成替身。"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib import error, parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alpaca_client
from alpaca_client import (AlpacaError, AlpacaPaperClient, SubmissionNotSent,
                           SubmissionUnknown, load_alpaca_credentials)


CONTRACT = "AAPL261016C00200000"
ORDER_ID = "12345678-1234-1234-1234-123456789abc"
CLIENT_ID = "yhs-offline-open-1"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}

    def getcode(self) -> int:
        return self.status


class AlpacaClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.requests = []
        self.responses = []
        # 单独合成 Key 保证每项测试拥有独立限频窗口，不接触环境中的凭据。
        self.api_key = "OFFLINE-" + uuid.uuid4().hex
        self.secret_key = "OFFLINE-SECRET-NEVER-REAL"

    def client(self, outcomes: list, **options) -> AlpacaPaperClient:
        source = iter(outcomes)

        def opener(req, timeout):
            self.requests.append((req, self.clock.now(), timeout))
            value = next(source)
            if isinstance(value, BaseException):
                raise value
            response = FakeResponse(*value)
            self.responses.append(response)
            return response

        settings = dict(clock_fn=self.clock.now, sleep_fn=self.clock.sleep,
                        opener=opener, max_get_retries=2)
        settings.update(options)
        client = AlpacaPaperClient(self.api_key, self.secret_key, **settings)
        self.addCleanup(client.close)
        return client

    @staticmethod
    def order(**updates) -> dict:
        payload = {"symbol": CONTRACT, "qty": 2, "side": "buy", "type": "limit",
                   "time_in_force": "day", "limit_price": "1.25",
                   "client_order_id": CLIENT_ID, "position_intent": "buy_to_open"}
        payload.update(updates)
        return payload

    def test_paper_and_market_data_use_fixed_hosts(self) -> None:
        client = self.client([(200, b'{"id":"offline-account"}'),
                              (200, b'{"quotes":{}}')])
        client.get_account()
        client.get_option_quotes([CONTRACT])
        self.assertTrue(client.paper_only)
        self.assertEqual(client.base_url, "https://paper-api.alpaca.markets")
        self.assertEqual(client.data_base_url, "https://data.alpaca.markets")
        self.assertEqual([parse.urlsplit(req.full_url).hostname for req, _, _ in self.requests],
                         ["paper-api.alpaca.markets", "data.alpaca.markets"])

    def test_external_live_and_unknown_routes_fail_before_network(self) -> None:
        client = self.client([])
        for path in ("https://api.alpaca.markets/v2/orders", "//api.alpaca.markets/v2/orders",
                     "/v2/../orders", "/v2/orders?status=all", "/v2/orders#fragment",
                     "/v2/unknown", "/v2/account/../../orders"):
            with self.subTest(path=path), self.assertRaises(AlpacaError):
                client._request("GET", path)
        self.assertEqual(self.requests, [])

    def test_modified_live_base_url_is_refused_before_network(self) -> None:
        client = self.client([])
        with mock.patch.object(alpaca_client, "PAPER_BASE_URL", "https://api.alpaca.markets"):
            with self.assertRaises(AlpacaError):
                client.get_account()
        self.assertEqual(self.requests, [])

    def test_data_host_is_read_only_and_redirect_is_not_followed(self) -> None:
        client = self.client([])
        with self.assertRaises(AlpacaError):
            client._request("POST", "/v1beta1/options/quotes/latest", data=True,
                            payload=self.order())
        redirect = alpaca_client._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://api.alpaca.markets/v2/orders")
        self.assertIsNone(redirect)
        self.assertEqual(self.requests, [])

    def test_get_network_and_server_errors_retry_only_reads(self) -> None:
        client = self.client([error.URLError("synthetic offline disconnect"),
                              (503, b'{"code":503,"message":"offline unavailable"}'),
                              (200, b'{"is_open":false}')])
        self.assertEqual(client.get_clock(), {"is_open": False})
        self.assertEqual([req.get_method() for req, _, _ in self.requests], ["GET"] * 3)
        self.assertEqual([at for _, at, _ in self.requests], [0.0, 1.0, 3.0])
        self.assertTrue(all(response.closed for response in self.responses))

    def test_get_429_obeys_retry_after(self) -> None:
        client = self.client([(429, b'{"message":"offline limited"}', {"Retry-After": "4"}),
                              (200, b'{"is_open":true}')])
        self.assertEqual(client.get_clock(), {"is_open": True})
        self.assertEqual([at for _, at, _ in self.requests], [0.0, 4.0])

    def test_get_retry_budget_stops_after_configured_attempts(self) -> None:
        client = self.client([error.URLError("offline")] * 3)
        with self.assertRaises(AlpacaError):
            client.get_account()
        self.assertEqual(len(self.requests), 3)

    def test_post_timeout_is_unknown_and_never_retried(self) -> None:
        client = self.client([TimeoutError("synthetic offline lost response")])
        with self.assertRaises(SubmissionUnknown) as caught:
            client.submit_order(self.order())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(caught.exception.client_order_id, CLIENT_ID)
        self.assertEqual(caught.exception.operation, "POST")

    def test_post_http_408_and_5xx_are_unknown_without_retry(self) -> None:
        for status in (408, 500, 503):
            with self.subTest(status=status):
                self.requests.clear()
                client = self.client([(status, b'{"message":"synthetic failure"}')])
                with self.assertRaises(SubmissionUnknown) as caught:
                    client.submit_order(self.order())
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(self.requests), 1)

    def test_post_success_with_unusable_body_is_unknown_without_retry(self) -> None:
        for status, body in ((201, b""), (200, b"bad-json"), (200, b"1"),
                             (200, b"[]"), (200, b'{"status":"accepted"}')):
            with self.subTest(status=status, body=body):
                self.requests.clear()
                client = self.client([(status, body)])
                with self.assertRaises(SubmissionUnknown) as caught:
                    client.submit_order(self.order())
                self.assertEqual(caught.exception.client_order_id, CLIENT_ID)
                self.assertEqual(len(self.requests), 1)

    def test_accepted_order_remains_accepted_not_filled(self) -> None:
        response = {"id": ORDER_ID, "client_order_id": CLIENT_ID,
                    "status": "accepted", "filled_qty": "0"}
        client = self.client([(201, json.dumps(response).encode())])
        self.assertEqual(client.submit_order(self.order()), response)
        self.assertEqual(len(self.requests), 1)

    def test_write_rejection_is_not_retried(self) -> None:
        client = self.client([(422, b'{"code":42210000,"message":"synthetic rejected"}')])
        with self.assertRaises(AlpacaError) as caught:
            client.submit_order(self.order())
        self.assertNotIsInstance(caught.exception, SubmissionUnknown)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(len(self.requests), 1)

    def test_replace_disconnect_preserves_order_identity_and_never_retries(self) -> None:
        client = self.client([ConnectionError("synthetic disconnect")])
        with self.assertRaises(SubmissionUnknown) as caught:
            client.replace_order(ORDER_ID, {"qty": 1, "client_order_id": CLIENT_ID})
        self.assertEqual(caught.exception.order_id, ORDER_ID)
        self.assertEqual(caught.exception.client_order_id, CLIENT_ID)
        self.assertEqual(caught.exception.operation, "PATCH")
        self.assertEqual(len(self.requests), 1)

    def test_cancel_204_means_accepted_then_caller_must_query_terminal_state(self) -> None:
        pending = {"id": ORDER_ID, "status": "pending_cancel", "filled_qty": "1"}
        client = self.client([(204, b""), (200, json.dumps(pending).encode())])
        self.assertIsNone(client.cancel_order(ORDER_ID))
        # 取消受理并没有凭空生成 canceled/filled 或丢弃已有部分成交。
        self.assertEqual(client.get_order(ORDER_ID), pending)
        self.assertEqual([req.get_method() for req, _, _ in self.requests], ["DELETE", "GET"])

    def test_cancel_response_loss_is_unknown_and_never_retried(self) -> None:
        client = self.client([error.URLError("synthetic response lost")])
        with self.assertRaises(SubmissionUnknown) as caught:
            client.cancel_order(ORDER_ID)
        self.assertEqual(caught.exception.order_id, ORDER_ID)
        self.assertEqual(caught.exception.operation, "DELETE")
        self.assertEqual(len(self.requests), 1)

    def test_submission_guard_runs_after_rate_wait_and_before_send(self) -> None:
        client = self.client([(200, b'{"id":"offline-account"}')])
        client.get_account()
        checked_at = []

        def guard():
            checked_at.append(self.clock.now())
            raise RuntimeError("synthetic signal expired while rate-limited")

        with self.assertRaises(SubmissionNotSent):
            client.submit_order(self.order(), before_send=guard)
        self.assertEqual(checked_at, [0.5])
        self.assertEqual(len(self.requests), 1)

    def test_guard_success_is_called_once_immediately_before_send(self) -> None:
        response = json.dumps({"id": ORDER_ID}).encode()
        client = self.client([(201, response)])
        checked_at = []
        client.submit_order(self.order(), before_send=lambda: checked_at.append(self.clock.now()))
        self.assertEqual(checked_at, [self.requests[0][1]])

    def test_guard_failure_redacts_exception_and_prevents_network(self) -> None:
        client = self.client([])

        def guard():
            raise RuntimeError(self.api_key + " " + self.secret_key)

        with self.assertRaises(SubmissionNotSent) as caught:
            client.submit_order(self.order(), before_send=guard)
        self.assertNotIn(self.api_key, str(caught.exception))
        self.assertNotIn(self.secret_key, str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_noncallable_guard_and_closed_client_send_nothing(self) -> None:
        client = self.client([])
        with self.assertRaises(SubmissionNotSent):
            client.submit_order(self.order(), before_send=False)
        client.close()
        with self.assertRaises(AlpacaError):
            client.submit_order(self.order())
        self.assertEqual(self.requests, [])

    def test_close_during_retry_wait_prevents_next_request(self) -> None:
        client = self.client([error.URLError("synthetic offline")])

        def sleep_then_close(seconds):
            self.clock.sleep(seconds)
            client.close()

        client._sleep_fn = sleep_then_close
        with self.assertRaises(AlpacaError):
            client.get_account()
        self.assertEqual(len(self.requests), 1)

    def test_invalid_order_contract_qty_price_or_intent_never_sends(self) -> None:
        client = self.client([])
        invalid = [{"symbol": "AAPL"}, {"symbol": "https://api.alpaca.markets"},
                   {"qty": 1.5}, {"qty": 0}, {"qty": True},
                   {"limit_price": "NaN"}, {"limit_price": "Infinity"},
                   {"position_intent": "sell_to_close"}, {"legs": []},
                   {"extended_hours": True}, {"client_order_id": "../bad"}]
        for updates in invalid:
            with self.subTest(updates=updates), self.assertRaises(AlpacaError):
                client.submit_order(self.order(**updates))
        self.assertEqual(self.requests, [])

    def test_broker_error_message_does_not_expose_synthetic_credentials(self) -> None:
        body = json.dumps({"message": f"{self.api_key} {self.secret_key} ALPACA_SECRET_KEY=offline",
                           "code": self.secret_key}).encode()
        client = self.client([(401, body)])
        with self.assertLogs("alpaca_client", level="ERROR") as logs:
            with self.assertRaises(AlpacaError) as caught:
                client.get_account()
        for text in [str(caught.exception), *logs.output]:
            self.assertNotIn(self.api_key, text)
            self.assertNotIn(self.secret_key, text)
        self.assertEqual(caught.exception.code, "")
        self.assertEqual(len(self.requests), 1)

    def test_batch_quotes_deduplicate_and_do_not_retry_missing_contracts(self) -> None:
        quote = {"bp": 1.2, "ap": 1.25, "t": "2026-10-09T14:00:00Z"}
        body = json.dumps({"quotes": {CONTRACT: quote}}).encode()
        client = self.client([(200, body)])
        missing = "MSFT261016P00300000"
        self.assertEqual(client.get_option_quotes([CONTRACT, "O:" + CONTRACT, missing]),
                         {CONTRACT: quote})
        self.assertEqual(len(self.requests), 1)
        params = parse.parse_qs(parse.urlsplit(self.requests[0][0].full_url).query)
        self.assertEqual(params["symbols"], [CONTRACT + "," + missing])
        self.assertEqual(params["feed"], ["indicative"])

    def test_order_not_found_returns_none_and_never_submits(self) -> None:
        client = self.client([(404, b'{"message":"synthetic absent"}')])
        self.assertIsNone(client.get_order_by_client_id(CLIENT_ID))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][0].get_method(), "GET")

    def test_unmoving_order_pagination_rejects_incomplete_list(self) -> None:
        page = json.dumps([{"id": ORDER_ID}] * 500).encode()
        client = self.client([(200, page), (200, page)])
        with self.assertRaisesRegex(AlpacaError, "分页未前进"):
            client.get_orders()
        self.assertEqual(len(self.requests), 2)

    def test_synthetic_environment_credentials_override_synthetic_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yhs_alpaca_credentials_") as folder:
            path = Path(folder) / "synthetic.env"
            path.write_text("ALPACA_API_KEY=file-offline\nALPACA_SECRET_KEY=file-offline-secret\n",
                            encoding="utf-8")
            with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "env-offline",
                                              "ALPACA_SECRET_KEY": "env-offline-secret"}, clear=True):
                credentials = load_alpaca_credentials(path)
                self.assertEqual(credentials, {"ALPACA_API_KEY": "env-offline",
                                               "ALPACA_SECRET_KEY": "env-offline-secret"})
                self.assertEqual(os.environ["ALPACA_API_KEY"], "env-offline")


if __name__ == "__main__":
    unittest.main()
