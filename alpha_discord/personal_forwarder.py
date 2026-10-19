"""Durable, informational forwarding of authenticated live AlphaAgent signals.

This module has no broker integration. It accepts the normalized event contract
from the personal collector, never a Discord token or an arbitrary message body.
Webhook POSTs have no idempotency guarantee: a persisted attempt whose result is
unknown is never sent again. The collector can safely repeat ``enqueue`` after a
restart. Historical messages and edits cannot create a new signal notification.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


_SOURCE_GUILD = "1434960637561409689"
_SOURCE_CHANNEL = "1492437553171660921"
_SOURCE_AUTHOR = "1462345436852654295"
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,19}\Z")
_WEBHOOK_PATH = re.compile(r"/api/(?:v[0-9]+/)?webhooks/([1-9][0-9]{0,19})/([A-Za-z0-9._-]{1,256})\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_TIER = re.compile(r"T[1-9][0-9]{0,2}\Z")
_MAX_AGE = 120.0
_FUTURE_TOLERANCE = 5.0
_DISCORD_EPOCH_MS = 1420070400000


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snowflake(value: Any) -> str | None:
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value) and int(value) < 2**64:
        return value
    return None


def _created(message_id: str) -> float:
    return ((int(message_id) >> 22) + _DISCORD_EPOCH_MS) / 1000.0


def _number(value: Any, *, maximum: str = "999999999.999999", positive: bool = True) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal(maximum) or (positive and number <= 0):
            return None
        return number
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _stamp(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return stamp.timestamp() if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class SignalForwarder:
    """One destination, one durable outbox, and no automatic ambiguous retries.

    Event contract: ``event_kind``, ``classification``, ``observer_only=True``,
    ``eligible_for_orders=False``, ``source`` (the exact authenticated original
    IDs and direct_personal_account provenance), and ``parsed`` (parse_message's
    complete result). Fresh CREATE additionally needs ``received_at_utc``.
    UPDATE/DELETE use the same source identity and only invalidate a prior CREATE.
    """

    def __init__(self, webhook: str, runtime: Path,
                 target_guild_id: str = "1111111111111111111",
                 target_channel_id: str = "2222222222222222222") -> None:
        try:
            parts = urlsplit(webhook)
            match = _WEBHOOK_PATH.fullmatch(parts.path)
            valid = (parts.scheme == "https" and parts.netloc == "discord.com"
                     and parts.username is None and parts.password is None
                     and not parts.query and not parts.fragment and match is not None)
        except (TypeError, ValueError):
            valid = False
        if not valid or not _snowflake(target_guild_id) or not _snowflake(target_channel_id):
            raise ValueError("invalid_forwarder_destination")
        self._webhook = webhook
        self._webhook_id = match.group(1)  # type: ignore[union-attr]
        self._target_guild = target_guild_id
        self._target_channel = target_channel_id
        self._runtime = Path(runtime)
        self._runtime.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False
        self._running = False
        self._verified = False
        self._next_validation = 0.0
        self._next_send_epoch = 0.0
        self._last_http_status: int | None = None
        self._last_category: str | None = None
        self._last_success_utc: str | None = None
        self._db = sqlite3.connect(str(self._runtime / "forwarder.sqlite3"), timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                source_key TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                first_revision TEXT, latest_revision TEXT, invalidated INTEGER NOT NULL DEFAULT 0,
                created_epoch REAL NOT NULL, updated_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                event_key TEXT PRIMARY KEY, source_key TEXT NOT NULL, revision TEXT,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_epoch REAL NOT NULL DEFAULT 0,
                attempted_utc TEXT, completed_utc TEXT, forwarded_message_id TEXT,
                outcome TEXT, http_status INTEGER
            );
            CREATE INDEX IF NOT EXISTS forwarder_pending ON outbox(status, next_attempt_epoch);
            CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        """)
        binding = hashlib.sha256(json.dumps({
            "webhook_sha256": hashlib.sha256(webhook.encode("utf-8")).hexdigest(),
            "target_guild_id": target_guild_id, "target_channel_id": target_channel_id,
            "source_guild_id": _SOURCE_GUILD, "source_channel_id": _SOURCE_CHANNEL,
            "source_author_id": _SOURCE_AUTHOR, "schema": 1,
        }, sort_keys=True).encode("utf-8")).hexdigest()
        old = self._db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
        if old is not None and old[0] != binding:
            self._db.close()
            self._closed = True
            raise ValueError("forwarder_destination_binding_changed")
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding', ?)", (binding,))
            # A process may have died after the remote service accepted a POST.
            self._db.execute("UPDATE outbox SET status='unknown', outcome='restart_after_persisted_attempt', "
                             "completed_utc=? WHERE status='attempting'", (_utc(),))
            for source in self._db.execute("SELECT source_key FROM sources WHERE invalidated=1").fetchall():
                self._queue_correction(source[0])
        rate_limit = self._db.execute("SELECT value FROM metadata WHERE key='rate_limit_until'").fetchone()
        if rate_limit is not None:
            try:
                self._next_send_epoch = float(rate_limit[0])
            except (ValueError, OverflowError):
                raise ValueError("invalid_durable_forwarder_rate_limit") from None
        self._write_status()

    def _count(self, key: str) -> None:
        self._db.execute("INSERT INTO counters VALUES (?,1) ON CONFLICT(key) DO UPDATE SET value=value+1", (key,))

    def _halt(self, reason: str) -> None:
        self._db.execute("INSERT INTO metadata VALUES ('halt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (reason,))

    def _halt_reason(self) -> str | None:
        row = self._db.execute("SELECT value FROM metadata WHERE key='halt'").fetchone()
        return row[0] if row is not None else None

    @staticmethod
    def _identity(event: Mapping[str, Any]) -> tuple[str, str] | None:
        source = event.get("source")
        if (event.get("observer_only") is not True or event.get("eligible_for_orders") is not False
                or not isinstance(source, Mapping)
                or source.get("guild_id") != _SOURCE_GUILD
                or source.get("channel_id") != _SOURCE_CHANNEL
                or source.get("author_id") != _SOURCE_AUTHOR
                or source.get("source_authenticated") is not True
                or source.get("source_author_authenticated") is not True
                or source.get("provenance_mode") != "direct_personal_account"):
            return None
        message_id = _snowflake(source.get("message_id"))
        if message_id is None:
            return None
        return f"discord:{_SOURCE_GUILD}:{_SOURCE_CHANNEL}:{message_id}", message_id

    @staticmethod
    def _payload(event: Mapping[str, Any], message_id: str) -> dict[str, Any] | None:
        parsed = event.get("parsed")
        if (not isinstance(parsed, Mapping) or parsed.get("parsed") is not True
                or parsed.get("metadata_valid") is not True or parsed.get("errors") != []
                or parsed.get("eligible_for_orders") is not False
                or parsed.get("action_kind") not in ("open", "close")):
            return None
        parsed_source = parsed.get("source")
        if not isinstance(parsed_source, Mapping) or any(parsed_source.get(key) != value for key, value in (
                ("guild_id", _SOURCE_GUILD), ("channel_id", _SOURCE_CHANNEL),
                ("author_id", _SOURCE_AUTHOR), ("message_id", message_id))):
            return None
        if parsed_source.get("author_is_bot") is not True:
            return None
        contract = parsed.get("contract")
        if not isinstance(contract, Mapping):
            return None
        symbol, right = contract.get("underlying"), contract.get("right")
        expiry, occ = contract.get("expiration"), contract.get("occ_symbol")
        strike = _number(contract.get("strike_decimal"), maximum="99999.999")
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]{1,6}", symbol)
                or right not in ("C", "P") or strike is None
                or strike * 1000 != (strike * 1000).to_integral_value()
                or not isinstance(expiry, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", expiry)):
            return None
        try:
            expiration = datetime.strptime(expiry, "%Y-%m-%d")
        except ValueError:
            return None
        if occ != symbol + expiration.strftime("%y%m%d") + right + f"{int(strike * 1000):08d}":
            return None
        tier = parsed.get("signal_tier")
        if tier is not None and (not isinstance(tier, str) or not _TIER.fullmatch(tier)):
            return None
        action = str(parsed["action_kind"]).upper()
        reference_name = "entry_reference_price_decimal" if action == "OPEN" else "exit_reference_price_decimal"
        price = _number(parsed.get(reference_name), maximum="999999.999999")
        if price is None:
            return None
        source_size = "未注明"
        if parsed.get("quantity_mode") == "unknown_size_multiplier":
            multiplier = _number(parsed.get("size_multiplier"), maximum="1000")
            if multiplier is None:
                return None
            source_size = f"{multiplier:f}x（源消息标记，不代表本账户张数）"
        elif parsed.get("quantity_mode") == "explicit_contracts":
            quantity = parsed.get("requested_contracts")
            if type(quantity) is not int or not 1 <= quantity <= 999999:
                return None
            source_size = f"{quantity} 张（源消息数量）"
        lines = [f"{symbol} {right} {strike:f} {expiry}", f"合约：{occ}",
                 f"{'Entry' if action == 'OPEN' else 'Exit'} 源参考价：${price:f}",
                 f"源消息规模：{source_size}"]
        source_url = f"https://discord.com/channels/{_SOURCE_GUILD}/{_SOURCE_CHANNEL}/{message_id}"
        return {
            "username": "羊叔信号同步",
            "content": "**AlphaAgent 信号同步｜仅同步，未确认成交**\n" + source_url,
            "allowed_mentions": {"parse": []},
            "embeds": [{"title": ((tier + " ") if tier else "") + action + "｜原频道信号",
                        "description": "\n".join(lines), "url": source_url,
                        "color": 3066993 if action == "OPEN" else 15158332,
                        "footer": {"text": "源参考价和规模不代表模拟盘成交、持仓或收益。"}}],
        }

    def _queue_correction(self, source_key: str) -> None:
        original = self._db.execute("SELECT status FROM outbox WHERE event_key=?", ("create:" + source_key,)).fetchone()
        if original is None or original[0] not in ("sent", "unknown"):
            return
        source = self._db.execute("SELECT message_id FROM sources WHERE source_key=?", (source_key,)).fetchone()
        if source is None:
            return
        source_url = f"https://discord.com/channels/{_SOURCE_GUILD}/{_SOURCE_CHANNEL}/{source[0]}"
        payload = {"username": "羊叔信号同步", "allowed_mentions": {"parse": []},
                   "content": "**AlphaAgent 信号状态变更｜仅同步，未确认成交**\n"
                              "原消息已被修改或删除，先前同步不再代表当前有效信号。"
                              "此通知不会创建交易指令。\n" + source_url}
        self._db.execute("INSERT OR IGNORE INTO outbox "
                         "(event_key,source_key,kind,payload_json,status) VALUES (?,?, 'correction',?,'pending')",
                         ("correction:" + source_key, source_key, json.dumps(payload, ensure_ascii=False)))

    async def enqueue(self, event: dict[str, Any]) -> bool:
        """Persist one observation. False safely acknowledges a rejected/duplicate event."""
        if self._closed:
            raise RuntimeError("forwarder_closed")
        async with self._lock:
            identity = self._identity(event) if isinstance(event, Mapping) else None
            if identity is None:
                with self._db:
                    self._count("rejected_source")
                return False
            source_key, message_id = identity
            kind = event.get("event_kind")
            revision = event.get("revision_sha256")
            if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
                revision = None
            now = time.time()
            created = _created(message_id)
            source = self._db.execute("SELECT * FROM sources WHERE source_key=?", (source_key,)).fetchone()
            if kind in ("MESSAGE_UPDATE", "MESSAGE_DELETE"):
                with self._db:
                    self._db.execute("INSERT INTO sources (source_key,message_id,latest_revision,invalidated,created_epoch,updated_utc) "
                                     "VALUES (?,?,?,1,?,?) ON CONFLICT(source_key) DO UPDATE SET "
                                     "latest_revision=excluded.latest_revision,invalidated=1,updated_utc=excluded.updated_utc",
                                     (source_key, message_id, revision, created, _utc()))
                    self._db.execute("UPDATE outbox SET status='cancelled',outcome='source_changed_before_delivery',completed_utc=? "
                                     "WHERE event_key=? AND status='pending'", (_utc(), "create:" + source_key))
                    self._queue_correction(source_key)
                    self._count("source_invalidations")
                self._wake.set()
                self._write_status()
                return True
            parsed = event.get("parsed")
            received = _stamp(event.get("received_at_utc"))
            payload = self._payload(event, message_id)
            valid = (kind == "MESSAGE_CREATE" and event.get("classification") == "fresh_observation"
                     and revision is not None and isinstance(parsed, Mapping)
                     and revision == parsed.get("revision_sha256") and received is not None
                     and -_FUTURE_TOLERANCE <= now - received <= _MAX_AGE
                     and -_FUTURE_TOLERANCE <= now - created <= _MAX_AGE
                     and received >= created - _FUTURE_TOLERANCE and payload is not None)
            if not valid:
                with self._db:
                    self._count("rejected_not_fresh_signal")
                return False
            if source is not None:
                with self._db:
                    if source["first_revision"] not in (None, revision):
                        self._db.execute("UPDATE sources SET invalidated=1,latest_revision=?,updated_utc=? WHERE source_key=?",
                                         (revision, _utc(), source_key))
                        self._db.execute("UPDATE outbox SET status='cancelled',outcome='source_revision_conflict',completed_utc=? "
                                         "WHERE event_key=? AND status='pending'", (_utc(), "create:" + source_key))
                        self._queue_correction(source_key)
                    self._count("duplicate_or_invalidated_source")
                self._wake.set()
                return False
            with self._db:
                self._db.execute("INSERT INTO sources VALUES (?,?,?,?,0,?,?)",
                                 (source_key, message_id, revision, revision, created, _utc()))
                self._db.execute("INSERT INTO outbox (event_key,source_key,revision,kind,payload_json,status) "
                                 "VALUES (?,?,?,'create',?,'pending')",
                                 ("create:" + source_key, source_key, revision, json.dumps(payload, ensure_ascii=False)))
                self._count("accepted_live_signals")
            self._wake.set()
            self._write_status()
            return True

    def _request(self, *, post: bool, payload: str | None = None) -> dict[str, Any]:
        """Return fixed categories only; never retain headers, URLs, or raw errors."""
        address = self._webhook + ("?wait=true" if post else "")
        headers = {"User-Agent": "YHS-QQ-Signal-Synchronizer/1.0"}
        data = payload.encode("utf-8") if payload is not None else None
        if post:
            headers["Content-Type"] = "application/json"
        request = Request(address, data=data, headers=headers, method="POST" if post else "GET")
        opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(request, timeout=10) as response:
                status = int(response.status)
                raw = response.read(65537)
            if len(raw) > 65536:
                return {"category": "invalid_acknowledgement", "status": status}
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                return {"category": "invalid_acknowledgement", "status": status}
            if post:
                message_id = _snowflake(result.get("id"))
                if (status in (200, 201) and message_id and result.get("channel_id") == self._target_channel
                        and result.get("webhook_id") == self._webhook_id):
                    return {"category": "success", "status": status, "message_id": message_id}
                return {"category": "invalid_acknowledgement", "status": status}
            if (status == 200 and result.get("id") == self._webhook_id
                    and result.get("channel_id") == self._target_channel
                    and result.get("guild_id") == self._target_guild and result.get("type") == 1):
                return {"category": "destination_verified", "status": status}
            return {"category": "destination_mismatch", "status": status}
        except HTTPError as exc:
            status = int(exc.code)
            if status == 429:
                try:
                    raw = exc.read(4096)
                    details = json.loads(raw.decode("utf-8"))
                    delay = _number(details.get("retry_after"), maximum="86400") if isinstance(details, dict) else None
                except (ValueError, UnicodeError, OSError):
                    delay = None
                return {"category": "rate_limited", "status": status,
                        "retry_after": max(1.0, float(delay)) if delay is not None else 5.0}
            if status in (401, 403, 404):
                return {"category": "webhook_unavailable", "status": status}
            if 300 <= status < 400:
                return {"category": "redirect_rejected", "status": status}
            return {"category": "known_rejection" if 400 <= status < 500 else "remote_result_unknown", "status": status}
        except (URLError, OSError, TimeoutError, ValueError, UnicodeError, HTTPException):
            return {"category": "transport_or_ack_unknown", "status": None}

    async def _validate_destination(self) -> None:
        outcome = await asyncio.to_thread(self._request, post=False)
        async with self._lock:
            self._last_http_status = outcome.get("status")
            self._last_category = str(outcome["category"])
            if outcome["category"] == "destination_verified":
                self._verified = True
            elif outcome["category"] in ("destination_mismatch", "webhook_unavailable", "redirect_rejected", "known_rejection"):
                with self._db:
                    self._halt(str(outcome["category"]))
            else:
                self._next_validation = time.time() + float(outcome.get("retry_after", 10.0))
            self._write_status()

    async def _deliver_one(self) -> bool:
        async with self._lock:
            now = time.time()
            if now < self._next_send_epoch:
                return False
            rows = self._db.execute("SELECT o.*,s.invalidated,s.latest_revision,s.created_epoch "
                                    "FROM outbox o JOIN sources s USING(source_key) WHERE o.status='pending' "
                                    "AND o.next_attempt_epoch<=? ORDER BY CASE o.kind WHEN 'correction' THEN 0 ELSE 1 END,o.rowid LIMIT 1",
                                    (now,)).fetchall()
            if not rows:
                return False
            row = rows[0]
            if row["kind"] == "create":
                invalid = row["invalidated"] or row["latest_revision"] != row["revision"]
                age = now - row["created_epoch"]
                if invalid or not -_FUTURE_TOLERANCE <= age <= _MAX_AGE:
                    with self._db:
                        self._db.execute("UPDATE outbox SET status=?,outcome=?,completed_utc=? WHERE event_key=?",
                                         ("cancelled" if invalid else "expired", "source_changed" if invalid else "source_age_exceeded",
                                          _utc(), row["event_key"]))
                    self._write_status()
                    return True
            with self._db:
                # This commit MUST precede the request. An interrupted attempt
                # cannot safely be distinguished from an accepted remote POST.
                self._db.execute("UPDATE outbox SET status='attempting',attempts=attempts+1,attempted_utc=? WHERE event_key=?",
                                 (_utc(), row["event_key"]))
            payload = row["payload_json"]
        outcome = await asyncio.to_thread(self._request, post=True, payload=payload)
        async with self._lock:
            category = str(outcome["category"])
            self._last_category = category
            self._last_http_status = outcome.get("status")
            if category == "success":
                status = "sent"
                self._last_success_utc = _utc()
            elif category == "rate_limited":
                status = "pending"
            elif category in ("webhook_unavailable", "redirect_rejected", "known_rejection"):
                status = "rejected"
            else:
                status = "unknown"
            with self._db:
                self._db.execute("UPDATE outbox SET status=?,outcome=?,http_status=?,forwarded_message_id=?,"
                                 "completed_utc=?,next_attempt_epoch=? WHERE event_key=?",
                                 (status, category, outcome.get("status"), outcome.get("message_id"),
                                  _utc() if status != "pending" else None,
                                  time.time() + float(outcome.get("retry_after", 0)), row["event_key"]))
                if category in ("webhook_unavailable", "redirect_rejected"):
                    self._halt(category)
                if category == "rate_limited":
                    self._next_send_epoch = time.time() + float(outcome.get("retry_after", 5.0))
                    self._db.execute("INSERT INTO metadata VALUES ('rate_limit_until',?) ON CONFLICT(key) "
                                     "DO UPDATE SET value=excluded.value", (str(self._next_send_epoch),))
                source = self._db.execute("SELECT invalidated FROM sources WHERE source_key=?", (row["source_key"],)).fetchone()
                if source is not None and source[0]:
                    if status == "pending" and row["kind"] == "create":
                        self._db.execute("UPDATE outbox SET status='cancelled',outcome='source_changed_before_retry',completed_utc=? "
                                         "WHERE event_key=?", (_utc(), row["event_key"]))
                    self._queue_correction(row["source_key"])
            self._write_status()
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        """Drain until requested to stop; never cancel a remote POST mid-attempt."""
        if self._closed or self._running:
            raise RuntimeError("forwarder_not_available")
        self._running = True
        last_status_update = 0.0
        try:
            while not stop_event.is_set():
                if time.monotonic() - last_status_update >= 15:
                    self._write_status()
                    last_status_update = time.monotonic()
                if not self._halt_reason():
                    if not self._verified and time.time() >= self._next_validation:
                        await self._validate_destination()
                    if self._verified and not self._halt_reason() and not stop_event.is_set():
                        if await self._deliver_one():
                            continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            self._write_status()

    def stats(self) -> dict[str, Any]:
        if self._closed:
            return {"closed": True, "observer_only": True, "orders_submitted": False}
        counts = {row[0]: row[1] for row in self._db.execute("SELECT status,count(*) FROM outbox GROUP BY status")}
        counters = {row[0]: row[1] for row in self._db.execute("SELECT key,value FROM counters")}
        return {"mode": "alpha_personal_signal_forwarder", "observer_only": True,
                "orders_submitted": False, "target_guild_id": self._target_guild,
                "target_channel_id": self._target_channel, "destination_verified": self._verified,
                "running": self._running, "halted": self._halt_reason() is not None,
                "halt_category": self._halt_reason(), "outbox": counts, "counters": counters,
                "last_http_status": self._last_http_status, "last_category": self._last_category,
                "last_success_utc": self._last_success_utc, "updated_at_utc": _utc(),
                "unknown_results_are_not_retried": True, "max_source_age_seconds": _MAX_AGE}

    def _write_status(self) -> None:
        if self._closed:
            return
        destination = self._runtime / "forwarder_status.json"
        temporary = self._runtime / "forwarder_status.json.tmp"
        temporary.write_text(json.dumps(self.stats(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)

    def close(self) -> None:
        if self._running:
            raise RuntimeError("stop_forwarder_before_close")
        if not self._closed:
            self._write_status()
            self._db.close()
            self._closed = True

    async def aclose(self) -> None:
        self.close()
