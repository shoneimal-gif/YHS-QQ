"""Offline Discord HTTP and durable queue checks; no credentials or network."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import discord_notifications as discord


WEBHOOK = "https://discord.com/api/webhooks/12345/TEST_ONLY"
CANONICAL = "https://discord.com/api/v10/webhooks/12345/TEST_ONLY"


def event(identity="order:submitted", *, contract="TEST261016C00100000"):
    return {"event_id": identity, "kind": "submitted", "side": "buy",
            "ticker": "TEST", "contract_symbol": contract,
            "timestamp": "2026-10-09T10:00:00-04:00",
            "requested_quantity": 3, "filled_quantity": 0,
            "remaining_quantity": 0, "price": 2.5, "right": "C",
            "expiration": "2026-10-16", "strike": 100,
            "order_type": "limit"}


class Response(io.BytesIO):
    def __init__(self, payload=None, *, raw=None, status=200, headers=None):
        payload = payload if payload is not None else {
            "id": "9876543210", "webhook_id": "12345", "channel_id": "67890"}
        super().__init__(raw if raw is not None else json.dumps(payload).encode())
        self.status = status
        self.headers = headers or {}


class Clock:
    def __init__(self):
        self.wall = 1_800_000_000.0
        self.elapsed = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds, *, wall_only=False):
        self.wall += seconds
        if not wall_only:
            self.elapsed += seconds


class DiscordHTTPTests(unittest.TestCase):
    def post_response(self, response, content="合成通知"):
        with patch.object(discord, "build_opener") as opener:
            opener.return_value.open.return_value = response
            receipt = discord._post(WEBHOOK, content)
            request = opener.return_value.open.call_args.args[0]
            return receipt, request, opener.return_value.open.call_args.kwargs

    def test_official_webhook_validation(self):
        self.assertEqual(discord.validate_webhook(WEBHOOK), CANONICAL)
        invalid = ["http://discord.com/api/webhooks/12345/TEST_ONLY",
                   "https://discord.com.evil.test/api/webhooks/12345/TEST_ONLY",
                   WEBHOOK + "?wait=true", WEBHOOK + "#fragment",
                   "https://user@discord.com/api/webhooks/12345/TEST_ONLY",
                   "https://discord.com/api/webhooks/12345/TEST_ONLY\n"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                discord.validate_webhook(value)

    def test_receipt_wait_mentions_timeout_and_rate_headers(self):
        receipt, request, options = self.post_response(Response(headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.75"}))
        self.assertEqual(receipt, discord._Receipt("9876543210", 2.75))
        self.assertEqual(request.full_url, CANONICAL + "?wait=true")
        body = json.loads(request.data)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(body["content"], "合成通知")
        self.assertEqual(options["timeout"], 4.0)

    def test_long_content_is_bounded(self):
        _, request, _ = self.post_response(Response(), content="合" * 3000)
        self.assertLessEqual(len(json.loads(request.data)["content"]), 2000)

    def test_unconfirmed_success_is_retryable(self):
        responses = [Response(status=204), Response(raw=b"broken-json"),
                     Response(raw=b"x" * 65537),
                     Response({"id": "0", "webhook_id": "12345", "channel_id": "67890"}),
                     Response({"id": "9876543210", "webhook_id": "different", "channel_id": "67890"}),
                     Response({"id": "9876543210", "webhook_id": "12345", "channel_id": 67890})]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(discord._DeliveryError) as result:
                self.post_response(response)
            self.assertFalse(result.exception.permanent)

    def test_429_honors_largest_server_delay(self):
        error = HTTPError(CANONICAL, 429, "synthetic", {
            "Retry-After": "2.5", "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "4.5"}, io.BytesIO(b'{"retry_after": 3.5}'))
        with patch.object(discord, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(discord._DeliveryError) as result:
                discord._post(WEBHOOK, "合成通知")
        self.assertEqual(result.exception.retry_after, 4.5)
        self.assertEqual(result.exception.blocked_for, 4.5)
        self.assertFalse(result.exception.permanent)
        self.assertTrue(error.fp.closed)

    def test_malformed_429_keeps_conservative_delay(self):
        self.assertEqual(discord._retry_after({}, {"retry_after": float("nan")}), 60)
        self.assertEqual(discord._retry_after({"Retry-After": "invalid"}), 60)
        self.assertEqual(discord._retry_after({"Retry-After": "0"}), 1)

    def test_permanent_and_transient_http_failures(self):
        for status, permanent in [(302, True), (400, True), (401, True), (403, True),
                                  (404, True), (408, False), (425, False), (503, False)]:
            error = HTTPError(CANONICAL, status, "synthetic", {}, io.BytesIO())
            with self.subTest(status=status), patch.object(discord, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(discord._DeliveryError) as result:
                    discord._post(WEBHOOK, "合成通知")
                self.assertEqual(result.exception.permanent, permanent)
                self.assertTrue(error.fp.closed)

    def test_network_exception_is_redacted(self):
        with patch.object(discord, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("TEST_SECRET_MARKER")
            with self.assertRaises(discord._DeliveryError) as result:
                discord._post(WEBHOOK, "合成通知")
        self.assertNotIn("TEST_SECRET_MARKER", str(result.exception))

    def test_environment_overrides_synthetic_local_configuration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".private_config" / "discord.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"webhook_url": WEBHOOK}), encoding="utf-8")
            with patch.object(discord.os, "environ", {}):
                self.assertEqual(discord.load_webhook(Path(root)), CANONICAL)
            with patch.object(discord.os, "environ", {"YHS_DISCORD_WEBHOOK": "invalid"}):
                with self.assertRaises(ValueError):
                    discord.load_webhook(Path(root))


class DiscordQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "discord_outbox.json"
        self.clock = Clock()
        self.clock_patch = patch.object(discord, "time", self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        self.notifiers = []
        self.addCleanup(self.stop_workers)

    def create(self, *, inactive=False):
        if inactive:
            with patch.object(discord.DiscordNotifier, "_run", return_value=None):
                notifier = discord.DiscordNotifier(WEBHOOK, self.path)
        else:
            notifier = discord.DiscordNotifier(WEBHOOK, self.path)
        self.notifiers.append(notifier)
        return notifier

    def stop_workers(self):
        for notifier in self.notifiers:
            notifier.close(timeout=0)
            notifier._worker.join(timeout=2)
            self.assertFalse(notifier._worker.is_alive(), "offline test leaked a worker")

    def wait_for(self, notifier, predicate):
        deadline = time.monotonic() + 2
        with notifier._condition:
            while not predicate():
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "sender did not reach expected state")
                notifier._condition.wait(timeout=min(remaining, 0.01))

    def advance(self, notifier, seconds, *, wall_only=False):
        with notifier._condition:
            self.clock.advance(seconds, wall_only=wall_only)
            notifier._condition.notify_all()

    def assert_calls_stable(self, notifier, post, count):
        # Give the real worker an opportunity to wake after the fake clock
        # changes; an immediate assertion could miss an early network request.
        with notifier._condition:
            notifier._condition.wait(timeout=0.03)
        self.assertEqual(post.call_count, count)

    def test_pending_dedup_persistence_and_secret_field_filtering(self):
        notifier = self.create(inactive=True)
        sample = event()
        sample["unexpected_secret"] = "TEST_SECRET_MARKER"
        self.assertTrue(notifier.enqueue(sample))
        self.assertTrue(notifier.enqueue(sample))
        self.assertEqual(len(notifier._pending), 1)
        saved = self.path.read_text(encoding="utf-8")
        self.assertNotIn("TEST_SECRET_MARKER", saved)
        self.assertNotIn("TEST_ONLY", saved)
        notifier.close(timeout=0)
        resumed = self.create(inactive=True)
        self.assertTrue(resumed.enqueue(sample))
        self.assertEqual(len(resumed._pending), 1)

    def test_sent_receipt_dedup_survives_restart(self):
        with patch.object(discord, "_post", return_value=discord._Receipt("9876543210", 0)) as post:
            notifier = self.create()
            self.assertTrue(notifier.enqueue(event()))
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
            notifier.close(timeout=0)
            resumed = self.create()
            self.assertTrue(resumed.enqueue(event()))
            self.assertEqual(post.call_count, 1)
            self.assertEqual(resumed._sent[event()["event_id"]], "76543210")
            self.assertEqual(json.loads(self.path.read_text())["pending"], [])

    def test_corrupt_state_is_preserved_and_fails_closed(self):
        cases = [b"broken-json", b'{"version": true, "pending": [], "sent_ids": {}}',
                 b'{"version": 1, "pending": [], "sent_ids": {}, "blocked_until": "10"}',
                 b'{"version": 1, "pending": [], "sent_ids": {}, "next_request_at": NaN}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.create()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_invalid_saved_attempt_counts_fail_closed(self):
        for attempts in [-1, True, "2", 1.5]:
            raw = json.dumps({"version": 1, "pending": [
                {"event": event(), "attempts": attempts, "next_attempt": 0}], "sent_ids": {}})
            with self.subTest(attempts=attempts):
                self.path.write_text(raw)
                with self.assertRaises(ValueError):
                    self.create()
                self.assertEqual(self.path.read_text(), raw)

    def test_storage_failure_prevents_network_and_preserves_existing_queue(self):
        notifier = self.create(inactive=True)
        self.assertTrue(notifier.enqueue(event()))
        old = self.path.read_bytes()
        with patch.object(notifier, "_save", side_effect=OSError("synthetic-disk-full")):
            self.assertFalse(notifier.enqueue(event("second")))
        self.assertTrue(notifier._storage_failed)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(len(notifier._pending), 1)
        self.assertFalse(notifier.enqueue(event("third")))

    def test_reservation_save_failure_never_starts_http(self):
        with patch.object(discord, "_post") as post:
            notifier = self.create()
            save, calls = notifier._save, []
            def save_until_reservation():
                calls.append(None)
                if len(calls) == 2:
                    raise OSError("synthetic-reservation-write-failure")
                save()
            with patch.object(notifier, "_save", side_effect=save_until_reservation):
                self.assertTrue(notifier.enqueue(event()))
                self.wait_for(notifier, lambda: notifier._storage_failed)
            post.assert_not_called()
            self.assertEqual(len(json.loads(self.path.read_text())["pending"]), 1)

    def test_receipt_save_failure_retains_durable_pending_event(self):
        with patch.object(discord, "_post", return_value=discord._Receipt("9876543210", 0)):
            notifier = self.create()
            save, calls = notifier._save, []
            def save_until_receipt():
                calls.append(None)
                if len(calls) == 3:
                    raise OSError("synthetic-receipt-write-failure")
                save()
            with patch.object(notifier, "_save", side_effect=save_until_receipt):
                self.assertTrue(notifier.enqueue(event()))
                self.wait_for(notifier, lambda: notifier._storage_failed)
            saved = json.loads(self.path.read_text())
            self.assertEqual(len(saved["pending"]), 1)
            self.assertEqual(saved["sent_ids"], {})

    def test_reservation_is_durable_before_http(self):
        reservations = []
        def post(*_args):
            reservations.append(json.loads(self.path.read_text()))
            return discord._Receipt("9876543210", 0)
        with patch.object(discord, "_post", side_effect=post):
            notifier = self.create()
            notifier.enqueue(event())
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
        self.assertEqual(reservations[0]["pending"][0]["event"]["event_id"], event()["event_id"])
        self.assertEqual(reservations[0]["next_request_at"], self.clock.wall + 5)

    def test_offline_retry_is_ordered_and_survives_restart(self):
        delivered = []
        def post(_webhook, content):
            delivered.append(content)
            if len(delivered) == 1:
                raise discord._DeliveryError()
            return discord._Receipt("9876543210", 0)
        with patch.object(discord, "_post", side_effect=post) as mock_post:
            notifier = self.create()
            notifier.enqueue(event("first", contract="FIRST"))
            notifier.enqueue(event("second", contract="SECOND"))
            self.wait_for(notifier, lambda: notifier._pending[0]["attempts"] == 1)
            self.advance(notifier, 29)
            self.assert_calls_stable(notifier, mock_post, 1)
            notifier.close(timeout=0)
            resumed = self.create()
            self.assertEqual(resumed._pending[0]["attempts"], 1)
            self.advance(resumed, 1)
            self.wait_for(resumed, lambda: len(resumed._sent) == 1)
            self.assertIn("FIRST", delivered[1])
            self.advance(resumed, 1)
            self.wait_for(resumed, lambda: len(resumed._sent) == 2)
        self.assertIn("SECOND", delivered[2])

    def test_successful_bucket_delay_and_minimum_spacing(self):
        receipts = [discord._Receipt("9876543210", 7), discord._Receipt("9876543211", 0)]
        with patch.object(discord, "_post", side_effect=receipts) as post:
            notifier = self.create()
            notifier.enqueue(event("first"))
            notifier.enqueue(event("second"))
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
            self.advance(notifier, 6)
            self.assert_calls_stable(notifier, post, 1)
            self.advance(notifier, 1)
            self.wait_for(notifier, lambda: len(notifier._sent) == 2)
            self.assertEqual(post.call_count, 2)

    def test_minimum_spacing_is_preserved_across_restart(self):
        with patch.object(discord, "_post", return_value=discord._Receipt("9876543210", 0)) as post:
            notifier = self.create()
            notifier.enqueue(event("first"))
            notifier.enqueue(event("second"))
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
            notifier.close(timeout=0)
            resumed = self.create()
            self.advance(resumed, 0.5)
            self.assert_calls_stable(resumed, post, 1)
            self.advance(resumed, 0.5)
            self.wait_for(resumed, lambda: len(resumed._sent) == 2)

    def test_wall_clock_jump_cannot_bypass_rate_limit(self):
        with patch.object(discord, "_post", return_value=discord._Receipt("9876543210", 7)) as post:
            notifier = self.create()
            notifier.enqueue(event("first"))
            notifier.enqueue(event("second"))
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
            self.advance(notifier, 3600, wall_only=True)
            self.assert_calls_stable(notifier, post, 1)
            self.advance(notifier, 7)
            self.wait_for(notifier, lambda: len(notifier._sent) == 2)

    def test_429_blocks_entire_queue_until_server_retry(self):
        outcomes = [discord._DeliveryError(429, retry_after=12),
                    discord._Receipt("9876543210", 0), discord._Receipt("9876543211", 0)]
        def post(*_args):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(discord, "_post", side_effect=post) as mock_post:
            notifier = self.create()
            notifier.enqueue(event("first"))
            notifier.enqueue(event("second"))
            self.wait_for(notifier, lambda: notifier._pending[0]["attempts"] == 1)
            self.advance(notifier, 11)
            self.assert_calls_stable(notifier, mock_post, 1)
            self.advance(notifier, 1)
            self.wait_for(notifier, lambda: len(notifier._sent) == 1)
            self.advance(notifier, 1)
            self.wait_for(notifier, lambda: len(notifier._sent) == 2)

    def test_permanent_error_keeps_queue_and_retries_after_restart(self):
        with patch.object(discord, "_post", side_effect=discord._DeliveryError(404, permanent=True)):
            notifier = self.create()
            notifier.enqueue(event())
            self.wait_for(notifier, lambda: notifier._halted_status == 404)
            saved = json.loads(self.path.read_text())
            self.assertEqual(saved["halted_http_status"], 404)
            self.assertEqual(len(saved["pending"]), 1)
            notifier.close(timeout=0)
        with patch.object(discord, "_post", return_value=discord._Receipt("9876543210", 0)):
            resumed = self.create()
            self.advance(resumed, 30)
            self.wait_for(resumed, lambda: len(resumed._sent) == 1)

    def test_close_refuses_new_events_and_keeps_undelivered_work(self):
        notifier = self.create(inactive=True)
        notifier.enqueue(event())
        notifier.close(timeout=0)
        self.assertFalse(notifier.enqueue(event("after-close")))
        self.assertEqual(len(json.loads(self.path.read_text())["pending"]), 1)

    def test_close_drains_existing_work_and_rejects_concurrent_new_event(self):
        started, finish = threading.Event(), threading.Event()
        def blocked_post(*_args):
            started.set()
            self.assertTrue(finish.wait(timeout=2))
            return discord._Receipt("9876543210", 0)
        try:
            with patch.object(discord, "_post", side_effect=blocked_post):
                notifier = self.create()
                notifier.enqueue(event())
                self.assertTrue(started.wait(timeout=2))
                closer = threading.Thread(target=lambda: notifier.close(timeout=5))
                closer.start()
                self.wait_for(notifier, lambda: notifier._closing)
                self.assertFalse(notifier._stopping)
                self.assertFalse(notifier.enqueue(event("during-close")))
                finish.set()
                closer.join(timeout=2)
                self.assertFalse(closer.is_alive())
                self.assertEqual(json.loads(self.path.read_text())["pending"], [])
        finally:
            finish.set()

    def test_close_during_http_does_not_overwrite_restarted_queue(self):
        started, finish = threading.Event(), threading.Event()
        def blocked_post(*_args):
            started.set()
            self.assertTrue(finish.wait(timeout=2))
            return discord._Receipt("9876543210", 0)
        try:
            with patch.object(discord, "_post", side_effect=blocked_post):
                notifier = self.create()
                notifier.enqueue(event())
                self.assertTrue(started.wait(timeout=2))
                notifier.close(timeout=0)
                resumed = self.create(inactive=True)
                self.assertTrue(resumed.enqueue(event("new-owner")))
                new_state = self.path.read_bytes()
                finish.set()
                notifier._worker.join(timeout=2)
                self.assertEqual(self.path.read_bytes(), new_state)
                self.assertEqual(len(json.loads(new_state)["pending"]), 2)
        finally:
            finish.set()


if __name__ == "__main__":
    unittest.main()
