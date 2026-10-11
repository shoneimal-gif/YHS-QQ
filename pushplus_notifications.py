"""Private-WeChat order notices through PushPlus, outside the trading ledger.

PushPlus success confirms service acceptance, not final WeChat delivery. The
durable outbox uses best-effort deduplication: an interrupted accepted request
can be repeated after restart. Tokens never appear in this module's state.
"""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from wecom_notifications import _message, _normalise_event


LOG = logging.getLogger(__name__)
_ENDPOINT = "https://www.pushplus.plus/send"
_CHINA = timezone(timedelta(hours=8))
_INTERVAL = 12.5
_DAILY_LIMIT = 180
_PERMANENT_CODES = {400, 401, 403, 903, 905}


def validate_token(token: str) -> str:
    """Accept an opaque ASCII token without exposing it in validation errors."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token.strip()):
        raise ValueError("PushPlus Token 无效，请检查个人通知 Token。")
    return token.strip()


def load_token(root: Path) -> str | None:
    """An explicit environment value overrides Git-ignored local settings."""
    if "YHS_PUSHPLUS_TOKEN" in os.environ:
        return validate_token(os.environ["YHS_PUSHPLUS_TOKEN"])
    path = Path(root) / ".private_config" / "pushplus.json"
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 16384:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return validate_token(data["token"])
    except Exception:
        raise ValueError("无法读取 PushPlus 通知配置，请检查本地私有配置文件。") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _AcceptanceError(Exception):
    def __init__(self, *, status: int | None = None, code: int | None = None):
        self.status = status
        self.code = code
        super().__init__("PushPlus 未确认受理通知。")


def _post(token: str, title: str, content: str) -> str:
    """Return only a safe receipt suffix; never follow a token-bearing redirect."""
    body = json.dumps({"token": token, "title": title[:100], "content": content,
                       "template": "txt", "channel": "wechat"},
                      ensure_ascii=False).encode("utf-8")
    request = Request(_ENDPOINT, data=body,
                      headers={"Content-Type": "application/json; charset=utf-8"},
                      method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=4) as response:
            if response.status != 200:
                raise _AcceptanceError(status=response.status)
            raw = response.read(32769)
            if len(raw) > 32768:
                raise _AcceptanceError()
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict) or type(result.get("code")) is not int:
                raise _AcceptanceError()
            if result["code"] != 200:
                raise _AcceptanceError(code=result["code"])
            serial = result.get("data")
            if (not isinstance(serial, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", serial)
                    or token in serial):
                raise _AcceptanceError()
            return serial[-8:]
    except HTTPError as exc:
        raise _AcceptanceError(status=exc.code) from None
    except _AcceptanceError:
        raise
    except Exception:
        raise _AcceptanceError() from None


def _title(event: dict) -> str:
    statuses = {"submitted": "已提交", "cancelled": "取消或到期",
                "rejected": "失败或拒绝", "unknown": "状态未知"}
    status = statuses.get(event["kind"])
    if event["kind"] == "filled":
        status = ("全部成交" if event["filled_quantity"] >= event["requested_quantity"]
                  else "部分成交")
    operation = "买入" if event["side"] == "buy" else "卖出"
    parts = ["YHS QQ", "Alpaca Paper", event["ticker"], operation + status]
    right = {"C": "Call", "P": "Put"}.get(event.get("right"))
    if right:
        parts.append(right)
    if event.get("expiration"):
        parts.append(event["expiration"])
    if "strike" in event:
        parts.append("$" + format(event["strike"], "g"))
    return "｜".join(parts)[:100]


def _today(now: float) -> date:
    return datetime.fromtimestamp(now, _CHINA).date()


def _next_midnight(day: date) -> float:
    return datetime.combine(day + timedelta(days=1), datetime_time.min, _CHINA).timestamp()


class PushPlusNotifier:
    """A serial daemon sender with durable pre-request daily accounting."""

    def __init__(self, token: str, state_path: Path):
        self._token = validate_token(token)
        self._path = Path(state_path)
        self._condition = threading.Condition()
        self._stopping = False
        self._disabled = False
        self._pending = []
        self._accepted = {}
        self._request_date = _today(time.time())
        self._request_count = 0
        self._blocked_until = 0.0
        self._last_request_at = 0.0
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if (type(data.get("version")) is not int or data["version"] != 1
                        or not isinstance(data.get("pending"), list)
                        or not isinstance(data.get("accepted"), dict)):
                    raise ValueError
                self._request_date = date.fromisoformat(data["request_date"])
                self._request_count = data["request_count"]
                if type(self._request_count) is not int or self._request_count < 0:
                    raise ValueError
                self._blocked_until = float(data.get("blocked_until", 0))
                self._last_request_at = float(data.get("last_request_at", 0))
                if not all(math.isfinite(value) and value >= 0
                           for value in (self._blocked_until, self._last_request_at)):
                    raise ValueError
                for identity, serial_tail in data["accepted"].items():
                    if (not isinstance(identity, str) or not identity or len(identity) > 240
                            or not isinstance(serial_tail, str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", serial_tail)):
                        raise ValueError
                    self._accepted[identity] = serial_tail
                seen = set(self._accepted)
                for item in data["pending"]:
                    event = _normalise_event(item["event"])
                    attempts = item.get("attempts", 0)
                    due = float(item.get("next_attempt", 0))
                    if (type(attempts) is not int or attempts < 0
                            or not math.isfinite(due) or due < 0):
                        raise ValueError
                    if event["event_id"] not in seen:
                        seen.add(event["event_id"])
                        self._pending.append({"event": event, "attempts": attempts,
                                              "next_attempt": due})
        except Exception:
            raise ValueError("PushPlus 通知队列无法读取，通知已停用。") from None
        self._worker = threading.Thread(target=self._run, name="pushplus-notifications", daemon=True)
        self._worker.start()

    def _save(self) -> None:
        # Callers hold the condition and check _stopping before writing.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        payload = {"version": 1, "pending": self._pending, "accepted": self._accepted,
                   "request_date": self._request_date.isoformat(),
                   "request_count": self._request_count,
                   "blocked_until": self._blocked_until,
                   "last_request_at": self._last_request_at}
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
                if identity in self._accepted or any(
                        item["event"]["event_id"] == identity for item in self._pending):
                    return True
                if self._stopping:
                    return False
                item = {"event": event, "attempts": 0, "next_attempt": 0.0}
                self._pending.append(item)
                try:
                    self._save()
                except Exception:
                    self._pending.remove(item)
                    raise
                self._condition.notify_all()
            return True
        except Exception as exc:
            LOG.warning("PushPlus 通知入队失败，类型=%s", type(exc).__name__)
            return False

    def _allowed_at(self, now: float) -> float:
        allowed = max(self._blocked_until, self._last_request_at + _INTERVAL)
        day = _today(now)
        if day < self._request_date:
            # A backwards clock change must not grant a fresh daily budget.
            allowed = max(allowed, _next_midnight(self._request_date))
        elif day == self._request_date and self._request_count >= _DAILY_LIMIT:
            allowed = max(allowed, _next_midnight(day))
        return allowed

    def _reserve_request(self, now: float) -> bool:
        """Persist consumption before network I/O, including retries/failures."""
        previous = (self._request_date, self._request_count, self._last_request_at)
        day = _today(now)
        if day > self._request_date:
            self._request_date = day
            self._request_count = 0
        self._request_count += 1
        self._last_request_at = now
        try:
            self._save()
            return True
        except Exception as exc:
            self._request_date, self._request_count, self._last_request_at = previous
            self._disabled = True
            LOG.warning("PushPlus 请求计数保存失败，通知停发至下次启动，类型=%s",
                        type(exc).__name__)
            return False

    def _run(self) -> None:
        last_finished = 0.0
        while True:
            with self._condition:
                while not self._stopping:
                    if self._disabled:
                        self._condition.wait(timeout=60)
                        continue
                    now = time.time()
                    item = next((value for value in self._pending
                                 if value["next_attempt"] <= now), None)
                    delay = max(0.0, self._allowed_at(now) - now,
                                _INTERVAL - (time.monotonic() - last_finished))
                    if item is not None and delay == 0:
                        if self._reserve_request(now):
                            break
                        continue
                    if item is None:
                        retry_delay = min((value["next_attempt"] - now for value in self._pending),
                                          default=60)
                        delay = max(delay, retry_delay)
                    self._condition.wait(timeout=max(0.01, min(delay, 60)))
                if self._stopping:
                    return
            failure = None
            serial_tail = None
            try:
                serial_tail = _post(self._token, _title(item["event"]), _message(item["event"]))
            except Exception as exc:
                failure = exc
            last_finished = time.monotonic()
            with self._condition:
                # The account owner can release its lease after bounded close.
                # Never overwrite a restarted owner's outbox when old I/O ends.
                if self._stopping:
                    return
                if failure is None:
                    self._pending.remove(item)
                    self._accepted[item["event"]["event_id"]] = serial_tail
                    LOG.info("PushPlus 已受理通知，流水号尾号=%s；最终送达需在微信确认", serial_tail)
                else:
                    item["attempts"] += 1
                    now = time.time()
                    status = getattr(failure, "status", None)
                    code = getattr(failure, "code", None)
                    if status == 429 or code == 900:
                        self._blocked_until = max(self._blocked_until, _next_midnight(_today(now)))
                        item["next_attempt"] = self._blocked_until
                        LOG.warning("PushPlus 触发服务限额，保留通知并暂停至中国时间次日，HTTP状态=%s，响应码=%s",
                                    status, code)
                    elif status in {400, 401, 403} or code in _PERMANENT_CODES:
                        self._disabled = True
                        LOG.warning("PushPlus 身份或配置异常，保留通知并停发至下次启动，HTTP状态=%s，响应码=%s",
                                    status, code)
                    else:
                        item["next_attempt"] = now + min(600, 30 * 2 ** min(item["attempts"] - 1, 5))
                        LOG.warning("PushPlus 未确认受理，稍后重试，HTTP状态=%s，响应码=%s", status, code)
                try:
                    self._save()
                except Exception as exc:
                    self._disabled = True
                    LOG.warning("PushPlus 通知状态保存失败，停发至下次启动，类型=%s", type(exc).__name__)
                self._condition.notify_all()

    def close(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._pending and self._worker.is_alive() and not self._disabled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            self._stopping = True
            self._condition.notify_all()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=max(0, deadline - time.monotonic()))


def send_test_notification(token: str) -> str:
    """Explicit manual test; receipt suffix confirms acceptance, not delivery."""
    return _post(validate_token(token), "YHS QQ｜Alpaca Paper｜个人微信通知测试",
                 "YHS QQ｜Alpaca Paper\n个人微信通知测试：仅测试推送连接，没有提交交易订单。\n"
                 "服务受理不等于最终送达，请在个人微信的 PushPlus 公众号中确认。")
