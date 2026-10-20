"""Independent Discord outbox for stock Paper events, with no trading writes.

The caller supplies share and dollar fields; this module applies no option
contract multiplier. A missing delivery receipt is not proof of non-delivery.
Such events stay unknown and a retry visibly warns that it may be a duplicate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from .common import atomic_json


LOG = logging.getLogger(__name__)
_WEBHOOK = re.compile(r"/api/(?:v10/)?webhooks/([1-9][0-9]{0,24})/([A-Za-z0-9_-]{1,256})")
_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,240}")
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,24}")
_CATEGORIES = {"timeout", "network_error", "tls_error", "rate_limit", "authentication",
               "permission", "webhook_missing", "redirect_rejected", "http_error",
               "server_error", "receipt_unknown", "state_error", "dependency_missing"}
_MAX_STATE_BYTES = 8 * 1024 * 1024
_MAX_PENDING = 1000
_HEADER = "Stock Quant | Alpaca Paper"
_DUPLICATE_NOTE = "通知回执曾丢失；本次重试可能重复，请以订单编号和券商记录为准。"


def validate_webhook(webhook: str) -> str:
    """Accept an official Discord HTTPS webhook, with no credentials in errors."""
    try:
        if not isinstance(webhook, str) or any(ord(char) < 32 for char in webhook):
            raise ValueError
        parsed = urlsplit(webhook.strip())
        match = _WEBHOOK.fullmatch(parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "discord.com"
                or parsed.query or parsed.fragment or match is None):
            raise ValueError
        return f"https://discord.com/api/v10/webhooks/{match[1]}/{match[2]}"
    except Exception:
        raise ValueError("Use an official Discord HTTPS channel webhook") from None


def _text(value: str, limit: int) -> str:
    # Remove control characters and unpaired surrogates; never manufacture links.
    clean = " ".join("".join(char for char in value
                             if ord(char) >= 32 and ord(char) != 127
                             and not 0xD800 <= ord(char) <= 0xDFFF).split())
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _clip_units(value: str, limit: int) -> str:
    if _units(value) <= limit:
        return value
    return value.encode("utf-16-le")[:max(0, limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def _event(event_id: str, title: str, fields: dict) -> dict:
    if not isinstance(event_id, str) or not _ID.fullmatch(event_id):
        raise ValueError("Invalid notification event")
    if not isinstance(title, str) or not title.strip() or not isinstance(fields, dict) or len(fields) > 16:
        raise ValueError("Invalid notification event")
    copied = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 64:
            raise ValueError("Invalid notification field")
        if any(word in key.lower() for word in ("secret", "password", "token", "webhook", "api_key", "private_key")):
            raise ValueError("Credential fields are not notification data")
        if type(value) is bool:
            rendered = "是" if value else "否"
        elif type(value) in (int, float):
            if not math.isfinite(value):
                raise ValueError("Invalid notification number")
            rendered = str(value)
        elif isinstance(value, str):
            rendered = value
        else:
            raise ValueError("Use text, finite numbers or booleans in notification fields")
        clean_key = _text(key, 64)
        if not clean_key or clean_key in copied:
            raise ValueError("Invalid notification field")
        copied[clean_key] = _text(rendered, 512)
    return {"event_id": event_id, "title": _text(title, 160), "fields": copied}


def _message(event: dict, unknown: bool, header: str = _HEADER) -> str:
    lines = [header, event["title"]]
    if unknown:
        lines.append(_DUPLICATE_NOTE)
    # Reserve room for an identity even if callers supply many long fields.
    identity = "事件编号：" + event["event_id"]
    remaining = 1900 - _units("\n".join(lines + [identity]))
    for key, value in event["fields"].items():
        if remaining <= 2:
            break
        line = _clip_units(key + "：" + value, remaining - 1)
        lines.append(line)
        remaining -= _units(line) + 1
    lines.append(identity)
    return "\n".join(lines)


def _seconds(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _backoff(attempt: int) -> float:
    return min(300.0, 5.0 * 2 ** min(max(attempt - 1, 0), 6))


def _rate_delay(headers, body: dict | None = None) -> float:
    durations = []
    for raw in (headers.get("Retry-After"), headers.get("X-RateLimit-Reset-After"),
                body.get("retry_after") if isinstance(body, dict) else None):
        duration = _seconds(raw)
        if duration is not None:
            durations.append(duration)
    if not durations and headers.get("Retry-After"):
        try:
            date = parsedate_to_datetime(headers["Retry-After"])
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            durations.append(max(0.0, (date - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            pass
    return max(1.0, max(durations)) if durations else 60.0


class StockNotifier:
    """One serial sender; enqueue acceptance and delivery are separate facts.

    Notification failures only affect this outbox. No broker reference, account
    credentials, environment credential lookup or trading-state callback exists.
    A permanently rejected webhook stays halted across restarts for correction.
    """

    def __init__(self, webhook: str, state_path: Path, *, header: str = _HEADER):
        if not isinstance(header, str) or not header.strip() or len(header) > 120:
            raise ValueError("Invalid notification heading")
        self._header = _text(header, 120)
        self._webhook = validate_webhook(webhook)
        self._webhook_id = _WEBHOOK.fullmatch(urlsplit(self._webhook).path)[1]
        self._token = _WEBHOOK.fullmatch(urlsplit(self._webhook).path)[2]
        self._path = Path(state_path)
        self._condition = threading.Condition(threading.RLock())
        self._closing = False
        self._state_ok = True
        self._state = {"version": 1, "sent_ids": [], "pending": [], "receipts": {},
                       "halted": False, "halt_category": None}
        try:
            self._load()
        except Exception:
            # Preserve an unreadable original file instead of replacing evidence.
            self._state_ok = False
            self._state["halted"] = True
            self._state["halt_category"] = "state_error"
            self._warn("state_error")
        self._thread = threading.Thread(target=self._run, name="stock-discord", daemon=True)
        self._thread.start()

    @staticmethod
    def _warn(category: str) -> None:
        safe = category if category in _CATEGORIES else "network_error"
        LOG.warning("Stock Discord notification unavailable: category=%s", safe)

    def _check_secret(self, event: dict) -> None:
        raw = json.dumps(event, ensure_ascii=False)
        if self._webhook in raw or (len(self._token) >= 8 and self._token in raw):
            raise ValueError("Credentials are not notification data")
        if re.search(r"https?://[^\s]*discord[^\s]*/(?:api/)?[^\s]*webhooks/", raw, re.IGNORECASE):
            raise ValueError("Webhook URLs are not notification data")

    def _load(self) -> None:
        if self._path.is_symlink():
            raise ValueError("Linked notification state")
        if not self._path.exists():
            atomic_json(self._path, self._state)
            return
        if not self._path.is_file() or self._path.stat().st_size > _MAX_STATE_BYTES:
            raise ValueError("Invalid notification state")
        state = json.loads(self._path.read_text(encoding="utf-8-sig"))
        if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
                or type(state.get("halted")) is not bool
                or not isinstance(state.get("sent_ids"), list)
                or not isinstance(state.get("pending"), list)
                or len(state["pending"]) > _MAX_PENDING):
            raise ValueError("Invalid notification state")
        sent = state["sent_ids"]
        if len(set(sent)) != len(sent) or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in sent):
            raise ValueError("Invalid notification identities")
        category = state.get("halt_category")
        if category is not None and category not in _CATEGORIES:
            raise ValueError("Invalid notification category")
        receipts = state.get("receipts", {})
        if not isinstance(receipts, dict) or any(key not in sent or not isinstance(value, str)
                                                or not _SNOWFLAKE.fullmatch(value)
                                                for key, value in receipts.items()):
            raise ValueError("Invalid notification receipts")
        restored = []
        seen = set(sent)
        for item in state["pending"]:
            if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
                raise ValueError("Invalid pending notification")
            event = _event(**item["event"])
            self._check_secret(event)
            event_id = event["event_id"]
            if event_id in seen:
                raise ValueError("Duplicate notification identity")
            seen.add(event_id)
            attempts, next_at = item.get("attempts"), _seconds(item.get("next_attempt_at"))
            status, last_error = item.get("status"), item.get("error_category")
            if (type(attempts) is not int or not 0 <= attempts <= 10 ** 9 or next_at is None
                    or status not in ("pending", "sending", "unknown")
                    or (last_error is not None and last_error not in _CATEGORIES)):
                raise ValueError("Invalid notification schedule")
            unknown = status in ("sending", "unknown")
            restored.append({"event": event, "attempts": attempts,
                             "next_attempt_at": max(next_at, time.time() + 5.0) if unknown else next_at,
                             "status": "unknown" if unknown else "pending",
                             "error_category": "receipt_unknown" if status == "sending" else last_error})
        self._state = {"version": 1, "sent_ids": sent, "pending": restored, "receipts": receipts,
                       "halted": state["halted"], "halt_category": category}
        atomic_json(self._path, self._state)

    def _save(self) -> bool:
        try:
            if len(json.dumps(self._state, ensure_ascii=False, allow_nan=False).encode("utf-8")) > _MAX_STATE_BYTES:
                raise ValueError("Notification state capacity reached")
            atomic_json(self._path, self._state)
            return True
        except Exception:
            self._state_ok = False
            self._state["halted"] = True
            self._state["halt_category"] = "state_error"
            self._warn("state_error")
            return False

    def enqueue(self, event_id: str, title: str, fields: dict) -> bool:
        try:
            event = _event(event_id, title, fields)
            self._check_secret(event)
            with self._condition:
                if self._closing or not self._state_ok:
                    return False
                if event_id in self._state["sent_ids"] or any(item["event"]["event_id"] == event_id
                                                             for item in self._state["pending"]):
                    return True
                if len(self._state["pending"]) >= _MAX_PENDING:
                    return False
                item = {"event": event, "attempts": 0, "next_attempt_at": 0.0,
                        "status": "pending", "error_category": None}
                self._state["pending"].append(item)
                if not self._save():
                    self._state["pending"].remove(item)
                    return False
                self._condition.notify_all()
                return True
        except Exception:
            return False

    def _deliver(self, session, requests, item: dict) -> tuple[str | None, str | None, float, bool, bool]:
        """Return receipt, fixed category, delay, permanent halt, unknown result."""
        category, delay, permanent, unknown = None, 0.0, False, False
        try:
            with session.post(self._webhook, params={"wait": "true"},
                              json={"content": _message(item["event"], item["status"] == "unknown", self._header),
                                    "allowed_mentions": {"parse": []}},
                              timeout=5.0, allow_redirects=False, stream=True) as response:
                status = response.status_code
                if status in (401, 403, 404):
                    category = {401: "authentication", 403: "permission", 404: "webhook_missing"}[status]
                    return None, category, _backoff(item["attempts"]), True, False
                if 300 <= status < 400:
                    return None, "redirect_rejected", _backoff(item["attempts"]), False, False
                raw = bytearray()
                try:
                    for part in response.iter_content(chunk_size=4096):
                        raw.extend(part)
                        if len(raw) > 65536:
                            break
                except Exception:
                    if status == 429:
                        return None, "rate_limit", _rate_delay(response.headers), False, False
                    raise
                try:
                    body = json.loads(raw) if len(raw) <= 65536 else None
                except (ValueError, TypeError):
                    body = None
                if status == 200:
                    receipt = body.get("id") if isinstance(body, dict) else None
                    if (isinstance(receipt, str) and _SNOWFLAKE.fullmatch(receipt)
                            and str(body.get("webhook_id")) == self._webhook_id):
                        delay = (_rate_delay(response.headers)
                                 if response.headers.get("X-RateLimit-Remaining") == "0" else 1.0)
                        return receipt, None, delay, False, False
                    category, unknown = "receipt_unknown", True
                elif status == 429:
                    category, delay = "rate_limit", _rate_delay(response.headers, body)
                elif status >= 500:
                    category, unknown = "server_error", True
                else:
                    category = "http_error"
        except requests.exceptions.Timeout:
            category, unknown = "timeout", True
        except requests.exceptions.SSLError:
            category, unknown = "tls_error", True
        except Exception:
            category, unknown = "network_error", True
        return None, category, delay or _backoff(item["attempts"]), permanent, unknown

    def _run(self) -> None:
        session = None
        try:
            import requests
            from requests.adapters import HTTPAdapter
            session = requests.Session()
            session.trust_env = False
            session.mount("https://", HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1))
            session.mount("http://", HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1))
            session.headers.update({"User-Agent": "StockQuant-Paper/1.0"})
        except Exception:
            with self._condition:
                self._state["halted"] = True
                self._state["halt_category"] = "dependency_missing"
                if self._state_ok:
                    self._save()
            self._warn("dependency_missing")
            if session is not None:
                session.close()
            return
        try:
            while True:
                with self._condition:
                    if self._closing:
                        return
                    if not self._state_ok or self._state["halted"] or not self._state["pending"]:
                        self._condition.wait(timeout=30.0)
                        continue
                    queued = self._state["pending"][0]
                    wait_for = queued["next_attempt_at"] - time.time()
                    if wait_for > 0:
                        self._condition.wait(timeout=min(wait_for, 30.0))
                        continue
                    # Persist the ambiguous in-flight phase before an HTTP write.
                    item = deepcopy(queued)
                    queued["attempts"] += 1
                    item["attempts"] = queued["attempts"]
                    queued["status"] = "sending"
                    if not self._save():
                        continue
                receipt, category, delay, permanent, unknown = self._deliver(session, requests, item)
                with self._condition:
                    queued = self._state["pending"][0]
                    if receipt is not None:
                        before = deepcopy(self._state)
                        event_id = queued["event"]["event_id"]
                        self._state["sent_ids"].append(event_id)
                        self._state["receipts"][event_id] = receipt
                        self._state["pending"].pop(0)
                        if self._state["pending"]:
                            self._state["pending"][0]["next_attempt_at"] = max(
                                self._state["pending"][0]["next_attempt_at"], time.time() + delay)
                        if not self._save():
                            self._state = before
                            self._state["halted"] = True
                            self._state["halt_category"] = "state_error"
                            self._state["pending"][0]["status"] = "unknown"
                            self._state["pending"][0]["error_category"] = "receipt_unknown"
                    else:
                        queued["status"] = "unknown" if unknown or item["status"] == "unknown" else "pending"
                        queued["error_category"] = category
                        queued["next_attempt_at"] = time.time() + delay
                        if permanent:
                            self._state["halted"] = True
                            self._state["halt_category"] = category
                        self._save()
                        self._warn(category)
        except Exception:
            # A notification programming/storage failure cannot enter broker state.
            with self._condition:
                self._state["halted"] = True
                self._state["halt_category"] = "state_error"
                if self._state_ok:
                    self._save()
            self._warn("state_error")
        finally:
            try:
                session.close()
            except Exception:
                self._warn("network_error")

    def close(self, timeout: float = 5) -> None:
        """Stop scheduling; retain pending events without delaying broker shutdown."""
        wait_for = _seconds(timeout)
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=min(5.0, wait_for if wait_for is not None else 0.0))
