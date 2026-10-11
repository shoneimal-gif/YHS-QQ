"""Discord webhook notifications, kept separate from the trading ledger.

The outbox survives restarts and stores only notification events, delivery
receipts, and request schedules. A lost confirmation or a crash after Discord
creates a message can still produce a duplicate when the pending event retries.
Webhook credentials and raw HTTP responses are never written to this state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wecom_notifications import _message, _normalise_event


LOG = logging.getLogger(__name__)
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,24}")
_WEBHOOK_PATH = re.compile(r"/api/(?:v10/)?webhooks/([1-9][0-9]{0,24})/([A-Za-z0-9_-]{1,256})")
_MIN_INTERVAL = 1.0
_HTTP_TIMEOUT = 4.0
_USER_AGENT = "YHS-QQ/1.0 (+https://github.com/shoneimal-gif/YHS-QQ)"


def validate_webhook(url: str) -> str:
    """Accept only the official Discord HTTPS incoming-webhook endpoint."""
    try:
        if not isinstance(url, str) or any(ord(char) < 32 for char in url):
            raise ValueError
        parts = urlsplit(url.strip())
        match = _WEBHOOK_PATH.fullmatch(parts.path)
        if (parts.scheme != "https" or parts.netloc != "discord.com"
                or parts.query or parts.fragment or match is None):
            raise ValueError
        return f"https://discord.com/api/v10/webhooks/{match[1]}/{match[2]}"
    except Exception:
        raise ValueError("Discord Webhook 地址无效，请复制 discord.com 的官方频道 Webhook。") from None


def load_webhook(root: Path) -> str | None:
    """Environment overrides the local configuration excluded from Git."""
    if "YHS_DISCORD_WEBHOOK" in os.environ:
        return validate_webhook(os.environ["YHS_DISCORD_WEBHOOK"])
    path = Path(root) / ".private_config" / "discord.json"
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 16384:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return validate_webhook(data["webhook_url"])
    except Exception:
        raise ValueError("无法读取 Discord 通知配置，请检查本地私有配置文件。") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _nonnegative_seconds(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _rate_limit_delay(headers) -> float:
    """Honor the bucket reset duration when the last request used its balance."""
    if headers is None:
        return 0.0
    try:
        remaining = int(headers.get("X-RateLimit-Remaining", ""))
        reset_after = _nonnegative_seconds(headers.get("X-RateLimit-Reset-After"))
        if remaining == 0 and reset_after is not None:
            return reset_after
    except (ValueError, TypeError, OverflowError):
        pass
    return 0.0


def _retry_after(headers, payload: dict | None = None) -> float:
    """Discord supplies seconds in Retry-After or the 429 JSON retry_after."""
    candidates = []
    header_value = headers.get("Retry-After") if headers is not None else None
    duration = _nonnegative_seconds(header_value)
    if duration is not None:
        candidates.append(duration)
    elif header_value:
        try:
            deadline = parsedate_to_datetime(header_value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
            if math.isfinite(seconds):
                candidates.append(max(0.0, seconds))
        except (ValueError, TypeError, OverflowError):
            pass
    if isinstance(payload, dict):
        duration = _nonnegative_seconds(payload.get("retry_after"))
        if duration is not None:
            candidates.append(duration)
    reset_delay = _rate_limit_delay(headers)
    if reset_delay > 0:
        candidates.append(reset_delay)
    # A malformed 429 response still gets a conservative retry interval.
    return max(_MIN_INTERVAL, max(candidates)) if candidates else 60.0


@dataclass(frozen=True)
class _Receipt:
    message_id: str
    blocked_for: float


class _DeliveryError(Exception):
    def __init__(self, status: int | None = None, *, retry_after: float | None = None,
                 blocked_for: float = 0.0, permanent: bool = False):
        self.status = status
        self.retry_after = retry_after
        self.blocked_for = blocked_for
        self.permanent = permanent
        super().__init__("Discord 未确认创建通知消息。")


def _post(webhook: str, content: str) -> _Receipt:
    canonical = validate_webhook(webhook)
    webhook_id = _WEBHOOK_PATH.fullmatch(urlsplit(canonical).path)[1]
    if len(content) > 2000:
        content = content[:1990] + "\n（已截断）"
    body = json.dumps({"content": content, "username": "YHS Paper",
                       "allowed_mentions": {"parse": []}},
                      ensure_ascii=False).encode("utf-8")
    request = Request(canonical + "?wait=true", data=body, method="POST",
                      headers={"Content-Type": "application/json; charset=utf-8",
                               "User-Agent": _USER_AGENT})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=_HTTP_TIMEOUT) as response:
            blocked_for = _rate_limit_delay(response.headers)
            if response.status != 200:
                raise _DeliveryError(response.status, blocked_for=blocked_for)
            raw = response.read(65537)
            if len(raw) > 65536:
                raise _DeliveryError(200, blocked_for=blocked_for)
            result = json.loads(raw.decode("utf-8"))
            if (not isinstance(result, dict)
                    or not isinstance(result.get("id"), str)
                    or not _SNOWFLAKE.fullmatch(result["id"])
                    or result.get("webhook_id") != webhook_id
                    or not isinstance(result.get("channel_id"), str)
                    or not _SNOWFLAKE.fullmatch(result["channel_id"])):
                raise _DeliveryError(200, blocked_for=blocked_for)
            return _Receipt(result["id"], blocked_for)
    except HTTPError as exc:
        payload = None
        try:
            if exc.code == 429:
                try:
                    raw = exc.read(65537)
                    if len(raw) <= 65536:
                        payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    pass
            blocked_for = _rate_limit_delay(exc.headers)
            retry = _retry_after(exc.headers, payload) if exc.code == 429 else None
            status = exc.code
        finally:
            exc.close()
        permanent = (300 <= status < 400 or
                     (400 <= status < 500 and status not in {408, 425, 429}))
        raise _DeliveryError(status, retry_after=retry,
                             blocked_for=blocked_for, permanent=permanent) from None
    except _DeliveryError:
        raise
    except Exception:
        raise _DeliveryError() from None


class DiscordNotifier:
    """A serial daemon sender whose outbox does not alter broker execution."""

    def __init__(self, webhook: str, state_path: Path):
        self._webhook = validate_webhook(webhook)
        self._path = Path(state_path)
        self._condition = threading.Condition()
        self._stopping = False
        self._storage_failed = False
        self._halted_status = None
        self._pending = []
        self._sent = {}
        self._blocked_until = 0.0
        self._next_request_at = 0.0
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if (not isinstance(data, dict) or type(data.get("version")) is not int
                        or data["version"] != 1 or not isinstance(data.get("sent_ids"), dict)
                        or not isinstance(data.get("pending"), list)):
                    raise ValueError
                for identity, tail in data["sent_ids"].items():
                    if (not isinstance(identity, str) or not identity or len(identity) > 240
                            or not isinstance(tail, str) or not re.fullmatch(r"[0-9]{1,8}", tail)):
                        raise ValueError
                self._sent = dict(data["sent_ids"])
                for name in ("blocked_until", "next_request_at"):
                    value = _nonnegative_seconds(data.get(name, 0))
                    if value is None:
                        raise ValueError
                    setattr(self, "_" + name, value)
                seen = set(self._sent)
                for item in data["pending"]:
                    event = _normalise_event(item["event"])
                    attempts = max(0, int(item.get("attempts", 0)))
                    due = _nonnegative_seconds(item.get("next_attempt", 0))
                    if due is None:
                        raise ValueError
                    if event["event_id"] not in seen:
                        seen.add(event["event_id"])
                        self._pending.append({"event": event, "attempts": attempts,
                                              "next_attempt": due})
            # A manual restart retries a corrected configuration once. Existing
            # server rate deadlines remain intact across that restart.
            self._save()
        except Exception:
            raise ValueError("Discord 通知队列无法读取或保存，通知已停用。") from None
        self._worker = threading.Thread(target=self._run, name="discord-notifications", daemon=True)
        self._worker.start()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        payload = {"version": 1, "pending": self._pending, "sent_ids": self._sent,
                   "blocked_until": self._blocked_until,
                   "next_request_at": self._next_request_at,
                   "halted_http_status": self._halted_status}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    def enqueue(self, event: dict) -> bool:
        try:
            event = _normalise_event(event)
            with self._condition:
                identity = event["event_id"]
                if identity in self._sent or any(
                        item["event"]["event_id"] == identity for item in self._pending):
                    return True
                if self._stopping or self._storage_failed:
                    return False
                item = {"event": event, "attempts": 0, "next_attempt": 0.0}
                self._pending.append(item)
                try:
                    self._save()
                except Exception:
                    self._pending.remove(item)
                    self._storage_failed = True
                    self._condition.notify_all()
                    raise
                self._condition.notify_all()
            return True
        except Exception as exc:
            LOG.warning("Discord 通知入队失败，类型=%s；通知队列需检查", type(exc).__name__)
            return False

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and not self._storage_failed:
                    if self._halted_status is not None:
                        self._condition.wait(timeout=60)
                        continue
                    now = time.time()
                    eligible_at = max(self._blocked_until, self._next_request_at)
                    item = next((item for item in self._pending
                                 if item["next_attempt"] <= now), None)
                    if item is not None and eligible_at <= now:
                        break
                    if item is None:
                        item_due = min((item["next_attempt"] for item in self._pending), default=now + 60)
                        delay = max(eligible_at, item_due) - now
                    else:
                        delay = eligible_at - now
                    self._condition.wait(timeout=max(0.01, min(delay, 60)))
                if self._stopping or self._storage_failed:
                    return
                # Persist a reservation before HTTP, so a restart observes an
                # outstanding request's minimum spacing and timeout allowance.
                self._next_request_at = time.time() + _HTTP_TIMEOUT + _MIN_INTERVAL
                try:
                    self._save()
                except Exception as exc:
                    self._storage_failed = True
                    LOG.warning("Discord 通知队列保存失败，发送已暂停，类型=%s", type(exc).__name__)
                    self._condition.notify_all()
                    return
            receipt = None
            failure = None
            try:
                receipt = _post(self._webhook, _message(item["event"]))
            except _DeliveryError as exc:
                failure = exc
                LOG.warning("Discord 通知未确认创建，HTTP状态=%s", exc.status)
            except Exception:
                failure = _DeliveryError()
                LOG.warning("Discord 通知未确认创建，稍后重试")
            with self._condition:
                # close() can return while HTTP finishes. The old sender must
                # never overwrite a queue now owned by a restarted executor.
                if self._stopping or self._storage_failed:
                    return
                now = time.time()
                self._next_request_at = now + _MIN_INTERVAL
                if failure is None:
                    self._pending.remove(item)
                    self._sent[item["event"]["event_id"]] = receipt.message_id[-8:]
                    self._blocked_until = max(self._blocked_until, now + receipt.blocked_for)
                else:
                    item["attempts"] += 1
                    delay = min(600, 30 * 2 ** min(item["attempts"] - 1, 5))
                    if failure.status == 429:
                        delay = failure.retry_after if failure.retry_after is not None else 60.0
                        self._blocked_until = max(self._blocked_until, now + delay)
                    self._blocked_until = max(self._blocked_until, now + failure.blocked_for)
                    item["next_attempt"] = now + delay
                    if failure.permanent:
                        self._halted_status = failure.status
                        LOG.warning("Discord Webhook 配置或权限错误，发送已暂停；请修正配置后重启")
                try:
                    self._save()
                except Exception as exc:
                    self._storage_failed = True
                    LOG.warning("Discord 通知队列保存失败，发送已暂停，类型=%s", type(exc).__name__)
                self._condition.notify_all()

    def close(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while (self._pending and self._worker.is_alive()
                   and not self._storage_failed and self._halted_status is None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            self._stopping = True
            self._condition.notify_all()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=max(0, deadline - time.monotonic()))


def send_test_notification(webhook: str) -> str:
    """Only the explicit user test command should call this function."""
    receipt = _post(webhook, "YHS QQ｜Alpaca Paper\nDiscord 通知测试：连接正常。此次仅发送测试通知，未提交订单。")
    return receipt.message_id[-8:]
