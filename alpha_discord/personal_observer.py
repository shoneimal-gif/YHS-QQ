"""Locally observe the bound AlphaAgent author with a personal Discord session.

This module does not load credentials, send personal-account messages, import a
broker, or grant permission to trade. Its private database is deliberately
incompatible with the announcement collector and copier databases. A separate
forwarder may receive normalized, authenticated observations.

Requires the isolated discord.py-self 2.1.0 environment, not discord.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies
import uuid

import discord

from .common import atomic_json, utc_now
from .parser import parse_message


LOG = logging.getLogger("alpha_discord.personal_observer")
SOURCE_GUILD_ID = "1434960637561409689"
SOURCE_CHANNEL_ID = "1492437553171660921"
SOURCE_AUTHOR_ID = "1462345436852654295"
_MODE = "direct_personal_account"
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_EVENTS = {"MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE", "MESSAGE_DELETE_BULK"}
_MAX_FRAME = 2 * 1024 * 1024
_MAX_MESSAGE = 65536
_MAX_SNAPSHOT = 32768
_MAX_UPDATE_AUDIT = 2 * _MAX_MESSAGE + 4096
_UPDATE_POLICY = "verified_embed_transport_v2"
_PREVIOUS_UPDATE_POLICY = "exact_raw_revision_v1"


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) and int(value) < 2**64 else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _created(message_id: str) -> datetime:
    if _id(message_id) is None:
        raise ValueError("invalid_source_message_id")
    return datetime.fromtimestamp(((int(message_id) >> 22) + 1420070400000) / 1000, timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _content_transport_changes(original: dict[str, Any], candidate: dict[str, Any]) -> list[str] | None:
    """Verify the two observed transport changes against the pristine CREATE.

    This is deliberately not a parser normalization. The original revision
    remains the execution identity; every other raw body/embed key and value,
    including unknown fields, must match with JSON-exact types and ordering.
    """
    if _json(original.get("content", "")) != _json(candidate.get("content", "")):
        return None
    before, after = original.get("embeds"), candidate.get("embeds")
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
        return None
    changed: list[str] = []
    for index, (source, observed) in enumerate(zip(before, after)):
        if not isinstance(source, dict) or not isinstance(observed, dict):
            return None
        checked = dict(observed)
        if "id" in source:
            # Discord may omit this transport ID in REST, never replace it.
            if _id(source["id"]) is None or ("id" in observed and observed["id"] != source["id"]):
                return None
            if "id" not in observed:
                checked["id"] = source["id"]
                changed.append(f"embeds[{index}].id")
        elif "id" in observed:
            return None
        if "content_scan_version" in source:
            old_scan, new_scan = source["content_scan_version"], observed.get("content_scan_version")
            # bool is an int subclass; only actual JSON integers qualify.
            if type(old_scan) is not int or type(new_scan) is not int:
                return None
            if old_scan != new_scan:
                if (old_scan, new_scan) != (0, 4):
                    return None
                checked["content_scan_version"] = old_scan
                changed.append(f"embeds[{index}].content_scan_version")
        elif "content_scan_version" in observed:
            return None
        if _json(source) != _json(checked):
            return None
    return changed


def _authenticated_parsed(data: dict[str, Any], message_id: str) -> dict[str, Any]:
    """Reproduce the observer's parsed proof without changing its raw hash."""
    parsed = parse_message(dict(data, content=data.get("content", "")))
    parsed["source"].update(source_authenticated=True, source_author_authenticated=True,
                            provenance_mode=_MODE, original_author_id=SOURCE_AUTHOR_ID,
                            original_timestamp_utc=_iso(_created(message_id)),
                            message_key_scope="direct_original_message")
    parsed["eligible_for_orders"] = False
    return parsed


def _error(exc: BaseException) -> str:
    # Never expose exception messages, request URLs, raw frames, or HTTP bodies.
    if isinstance(exc, discord.LoginFailure):
        return "authentication_failed"
    if isinstance(exc, discord.Forbidden):
        return "source_permission_denied"
    if isinstance(exc, discord.NotFound):
        return "source_unavailable"
    if isinstance(exc, discord.RateLimited):
        return "rate_limited"
    if isinstance(exc, discord.HTTPException):
        return {401: "authentication_failed", 403: "source_permission_denied", 429: "rate_limited"}.get(exc.status, "discord_http_failed")
    if isinstance(exc, discord.ConnectionClosed):
        return "authentication_failed" if exc.code == 4004 else "gateway_connection_failed"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "request_timeout"
    if isinstance(exc, sqlite3.Error):
        return "storage_failed"
    if isinstance(exc, OSError):
        return "io_or_network_failed"
    return "observer_internal_failed"


def _silence_sdk_logs() -> None:
    names = {"discord", "aiohttp", "curl_cffi", "asyncio"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(("discord.", "aiohttp.", "curl_cffi.")))
    for name in names:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True


def _system_proxy() -> str | None:
    """Reuse the machine's configured HTTP(S) proxy without logging its URL.

    urllib reads Windows Internet Settings as well as proxy environment
    variables. The SDK does not automatically inherit those settings.
    """
    configured = getproxies()
    for protocol in ("https", "http"):
        proxy = configured.get(protocol)
        if proxy is None:
            continue
        if not isinstance(proxy, str) or not proxy or len(proxy) > 4096 or any(char.isspace() or ord(char) < 32 for char in proxy):
            raise ValueError("system_proxy_invalid")
        parts = urlsplit(proxy)
        if parts.scheme.lower() not in ("http", "https"):
            continue
        if not parts.hostname or parts.fragment or parts.query or parts.path not in ("", "/"):
            raise ValueError("system_proxy_invalid")
        # Accessing port validates malformed/out-of-range values. Userinfo,
        # when already configured locally, stays in memory and is never saved.
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("system_proxy_invalid")
        return proxy
    return None


class _ProcessLock:
    def __init__(self, path: Path):
        if path.is_symlink():
            raise ValueError("linked_lock_refused")
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, os.SEEK_END)
                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise

    def close(self) -> None:
        self.file.close()


class _Store:
    def __init__(self, path: Path, binding: dict[str, Any], session_id: str):
        if path.is_symlink():
            raise ValueError("linked_database_refused")
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.session_id = session_id
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS personal_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_messages(
                source_key TEXT PRIMARY KEY,message_id TEXT UNIQUE NOT NULL,
                first_received_utc TEXT NOT NULL,last_received_utc TEXT NOT NULL,
                created_utc TEXT NOT NULL,latest_revision TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,ever_edited INTEGER NOT NULL DEFAULT 0,
                latest_snapshot_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personal_revisions(
                source_key TEXT NOT NULL,revision TEXT NOT NULL,event_kind TEXT NOT NULL,
                received_utc TEXT NOT NULL,classification TEXT NOT NULL,snapshot_json TEXT NOT NULL,
                PRIMARY KEY(source_key,revision,event_kind)
            );
            CREATE TABLE IF NOT EXISTS personal_tombstones(
                message_id TEXT PRIMARY KEY,received_utc TEXT NOT NULL,reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personal_forward_outbox(
                event_id TEXT PRIMARY KEY,source_key TEXT NOT NULL,event_kind TEXT NOT NULL,
                session_id TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,
                received_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS personal_update_checks(
                ticket TEXT PRIMARY KEY,message_id TEXT NOT NULL,received_utc TEXT NOT NULL,
                resolved_utc TEXT,status TEXT NOT NULL,baseline_revision TEXT,
                verified_revision TEXT,audit_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS personal_update_checks_message_status
                ON personal_update_checks(message_id,status);
            CREATE TABLE IF NOT EXISTS personal_raw_proofs(
                message_id TEXT NOT NULL,revision TEXT NOT NULL,event_kind TEXT NOT NULL,
                received_utc TEXT NOT NULL,payload_json TEXT NOT NULL,
                PRIMARY KEY(message_id,revision,event_kind)
            );
        """)
        digest = hashlib.sha256(_json(binding).encode("utf-8")).hexdigest()
        old = self.db.execute("SELECT value FROM personal_metadata WHERE key='binding_sha256'").fetchone()
        if old and old[0] != digest:
            self.db.close()
            raise ValueError("personal_runtime_binding_mismatch")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO personal_metadata VALUES('binding_sha256',?)", (digest,))
            self.db.execute("INSERT OR IGNORE INTO personal_metadata VALUES('schema_version','1')")
            policy = self.db.execute("SELECT value FROM personal_metadata WHERE key='update_verification_policy'").fetchone()
            if policy is None and old is None:
                self.db.execute("INSERT INTO personal_metadata VALUES('update_verification_policy',?)", (_UPDATE_POLICY,))
                policy = self.db.execute("SELECT value FROM personal_metadata WHERE key='update_verification_policy'").fetchone()
            if policy is not None and policy[0] == _PREVIOUS_UPDATE_POLICY:
                # An explicit observer-policy migration only. No source or
                # financial binding, revision, tombstone or clock is rewritten.
                self.db.execute("UPDATE personal_metadata SET value=? WHERE key='update_verification_policy' AND value=?",
                                (_UPDATE_POLICY, _PREVIOUS_UPDATE_POLICY))
                self.db.execute("INSERT OR REPLACE INTO personal_metadata VALUES('update_verification_policy_migration',?)",
                                (_json({"from": _PREVIOUS_UPDATE_POLICY, "to": _UPDATE_POLICY, "at_utc": _iso(utc_now())}),))
                policy = self.db.execute("SELECT value FROM personal_metadata WHERE key='update_verification_policy'").fetchone()
            if policy is None or policy[0] != _UPDATE_POLICY:
                raise ValueError("personal_update_policy_mismatch")
            # Never turn a process restart into a replay of old signal posts.
            self.db.execute("UPDATE personal_forward_outbox SET status='restart_not_replayed' WHERE status='pending' AND event_kind='MESSAGE_CREATE' AND session_id<>?", (session_id,))
        # A crash can interrupt a GET after a real edit. Such tickets are never
        # retried into validity, and a restart never clears old tombstones.
        pending = self.db.execute("SELECT ticket,message_id FROM personal_update_checks WHERE status='pending'").fetchall()
        for item in pending:
            self.reject_update(item["ticket"], item["message_id"], utc_now(), "verification_interrupted_on_restart", forwarding=True)

    def message(self, message_id: str):
        return self.db.execute("SELECT * FROM personal_messages WHERE message_id=?", (message_id,)).fetchone()

    def pending_update(self, message_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM personal_update_checks WHERE message_id=? AND status='pending' LIMIT 1", (message_id,)).fetchone() is not None

    def pending_ticket(self, ticket: str) -> bool:
        return self.db.execute("SELECT 1 FROM personal_update_checks WHERE ticket=? AND status='pending'", (ticket,)).fetchone() is not None

    def _create_baseline(self, message_id: str) -> dict[str, Any] | None:
        row = self.message(message_id)
        if (row is None or row["deleted"] or row["ever_edited"]
                or self.db.execute("SELECT 1 FROM personal_tombstones WHERE message_id=?", (message_id,)).fetchone()):
            return None
        revision = self.db.execute("SELECT snapshot_json FROM personal_revisions WHERE source_key=? AND revision=? AND event_kind='MESSAGE_CREATE' AND classification='fresh_observation'", (row["source_key"], row["latest_revision"])).fetchone()
        count = self.db.execute("SELECT COUNT(DISTINCT revision) FROM personal_revisions WHERE source_key=?", (row["source_key"],)).fetchone()[0]
        if revision is None or count != 1:
            return None
        snapshot = json.loads(revision[0])
        parsed = snapshot.get("parsed")
        source = parsed.get("source") if isinstance(parsed, dict) else None
        if (not isinstance(source, dict) or snapshot.get("classification") != "fresh_observation"
                or parsed.get("metadata_valid") is not True or parsed.get("parsed") is not True or parsed.get("errors") != []
                or parsed.get("revision_sha256") != row["latest_revision"] or source.get("edited_timestamp_utc") is not None
                or source.get("source_authenticated") is not True or source.get("source_author_authenticated") is not True
                or source.get("guild_id") != SOURCE_GUILD_ID or source.get("channel_id") != SOURCE_CHANNEL_ID
                or source.get("message_id") != message_id or source.get("author_id") != SOURCE_AUTHOR_ID
                or source.get("author_is_bot") is not True or source.get("provenance_mode") != _MODE):
            return None
        return parsed

    def _create_raw_proof(self, message_id: str, baseline: dict[str, Any]) -> dict[str, Any] | None:
        """Require the exact durable raw proof of this pristine live CREATE."""
        row = self.message(message_id)
        proof = self.db.execute("SELECT received_utc,payload_json FROM personal_raw_proofs WHERE message_id=? AND revision=? AND event_kind='MESSAGE_CREATE'",
                                (message_id, baseline.get("revision_sha256"))).fetchone()
        if (row is None or proof is None or proof["received_utc"] != row["first_received_utc"]
                or not isinstance(proof["payload_json"], str)
                or len(proof["payload_json"].encode("utf-8")) > _MAX_MESSAGE):
            return None
        raw = json.loads(proof["payload_json"])
        if (not isinstance(raw, dict) or raw.get("id") != message_id or raw.get("edited_timestamp") is not None
                or _json(_authenticated_parsed(raw, message_id)) != _json(baseline)):
            return None
        return raw

    def begin_update(self, message_id: str, received: datetime, data: dict[str, Any], *, forwarding: bool) -> str:
        """Durably close this message's gate before queueing or awaiting GET."""
        baseline = self._create_baseline(message_id)
        ticket = uuid.uuid4().hex
        audit = {"policy": _UPDATE_POLICY, "gateway_payload_sha256": hashlib.sha256(_json(data).encode("utf-8")).hexdigest(),
                 "gateway_edited_timestamp_present": data.get("edited_timestamp") is not None}
        if baseline is not None:
            # A stale verification GET must not override an observed changed
            # content/embed field in the Gateway update itself.
            audit["gateway_supplied"] = {key: data[key] for key in ("content", "embeds", "timestamp", "edited_timestamp", "flags", "message_reference", "message_snapshots") if key in data}
            if isinstance(data.get("author"), dict):
                audit["gateway_supplied"]["author"] = {key: data["author"][key] for key in ("id", "bot") if key in data["author"]}
        with self.db:
            self.db.execute("INSERT INTO personal_update_checks VALUES(?,?,?,NULL,'pending',?,NULL,?)",
                            (ticket, message_id, _iso(received), baseline.get("revision_sha256") if baseline else None, _json(audit)))
        if baseline is None or data.get("edited_timestamp") is not None:
            self.reject_update(ticket, message_id, received, "no_pristine_live_create" if baseline is None else "gateway_edit_timestamp", forwarding=forwarding)
        return ticket

    def reject_update(self, ticket: str, message_id: str, received: datetime, reason: str, *, forwarding: bool,
                      deleted: bool = False, verified_revision: str | None = None,
                      verified_payload: dict[str, Any] | None = None) -> None:
        self.invalidate(message_id, received, "MESSAGE_DELETE" if deleted else "MESSAGE_UPDATE", forwarding=forwarding)
        with self.db:
            row = self.db.execute("SELECT audit_json FROM personal_update_checks WHERE ticket=? AND message_id=?", (ticket, message_id)).fetchone()
            if row is None:
                raise ValueError("update_verification_ticket_missing")
            audit = json.loads(row[0])
            audit["resolution"] = reason
            if verified_payload is not None:
                audit["verification_payload"] = verified_payload
                if len(_json(audit).encode("utf-8")) > _MAX_UPDATE_AUDIT:
                    raise ValueError("update_audit_too_large")
            self.db.execute("UPDATE personal_update_checks SET status='invalidated',resolved_utc=?,verified_revision=?,audit_json=? WHERE ticket=? AND message_id=?",
                            (_iso(utc_now()), verified_revision, _json(audit), ticket, message_id))

    def metadata_update(self, ticket: str, parsed: dict[str, Any], data: dict[str, Any], received: datetime) -> bool:
        message_id = parsed["source"]["message_id"]
        row = self.db.execute("SELECT * FROM personal_update_checks WHERE ticket=? AND message_id=?", (ticket, message_id)).fetchone()
        baseline = self._create_baseline(message_id)
        if (row is None or row["status"] != "pending" or baseline is None
                or data.get("edited_timestamp") is not None
                or row["baseline_revision"] != baseline.get("revision_sha256")
                or _json({k: v for k, v in parsed.items() if k != "revision_sha256"})
                != _json({k: v for k, v in baseline.items() if k != "revision_sha256"})
                or _json(_authenticated_parsed(data, message_id)) != _json(parsed)):
            return False
        proof = self._create_raw_proof(message_id, baseline)
        if proof is None:
            return False
        rest_changes = _content_transport_changes(proof, data)
        if rest_changes is None:
            return False
        audit = json.loads(row["audit_json"])
        supplied = audit.get("gateway_supplied", {})
        if not isinstance(supplied, dict):
            return False
        # Verify all supplied Gateway body/embed fields against the original
        # proof as well as the REST result. Both must reduce to that same exact
        # proof under the finite transport rules; a stale GET cannot erase a
        # real edit seen first on the Gateway. ID presence may differ on each
        # transport, but any present ID must still equal the original ID.
        gateway_body = dict(proof)
        gateway_body.update({key: supplied[key] for key in ("content", "embeds") if key in supplied})
        gateway_changes = _content_transport_changes(proof, gateway_body)
        if gateway_changes is None:
            return False
        for key, value in supplied.items():
            if key in ("content", "embeds"):
                # Full content/embed equality with the GET is already proved
                # transitively above, allowing only the two transport fields.
                continue
            if key == "author":
                if not isinstance(value, dict) or any(_json((data.get("author") or {}).get(k)) != _json(v) for k, v in value.items()):
                    return False
            elif _json(data.get(key)) != _json(value):
                return False
        audit.update(resolution="verified_full_content_and_embed_transport", verification_payload=data,
                     original_create_revision=baseline["revision_sha256"],
                     verified_raw_revision=parsed["revision_sha256"],
                     allowed_transport_field_paths={"gateway": gateway_changes, "rest": rest_changes},
                     verification_received_utc=_iso(received))
        encoded = _json(audit)
        if len(encoded.encode("utf-8")) > _MAX_UPDATE_AUDIT:
            raise ValueError("update_audit_too_large")
        with self.db:
            self.db.execute("UPDATE personal_update_checks SET status='metadata_only',resolved_utc=?,verified_revision=?,audit_json=? WHERE ticket=? AND status='pending'",
                            (_iso(utc_now()), parsed["revision_sha256"], encoded, ticket))
        return True

    def _enqueue(self, event: dict[str, Any]) -> None:
        if self.db.execute("SELECT COUNT(*) FROM personal_forward_outbox WHERE status='pending'").fetchone()[0] >= 1000:
            raise ValueError("forward_queue_full")
        event_id = hashlib.sha256(_json([event["source_key"], event["event_kind"], event["revision_sha256"]]).encode("utf-8")).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO personal_forward_outbox VALUES(?,?,?,?,?,'pending',?)", (event_id, event["source_key"], event["event_kind"], self.session_id, _json(event), event["received_at_utc"]))

    def observe(self, parsed: dict[str, Any], event_kind: str, received: datetime, live: bool, max_age: float, *, forwarding: bool,
                raw_payload: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        source = parsed["source"]
        message_id = source["message_id"]
        key = source["message_key"]
        previous = self.message(message_id)
        tombstone = self.db.execute("SELECT reason FROM personal_tombstones WHERE message_id=?", (message_id,)).fetchone()
        created = _created(message_id)
        age = (received - created).total_seconds()
        deleted = bool(tombstone and tombstone["reason"] == "source_deleted" or previous and previous["deleted"])
        edited = bool(tombstone and tombstone["reason"] == "source_edited" or event_kind == "MESSAGE_UPDATE"
                      or source.get("edited_timestamp_utc") or previous and previous["ever_edited"])
        revision = parsed.get("revision_sha256")
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            revision = hashlib.sha256(_json(parsed).encode("utf-8")).hexdigest()
        if deleted:
            classification = "withdrawn_observation"
        elif edited:
            classification = "edited_observation"
        elif previous:
            classification = "duplicate_observation" if previous["latest_revision"] == revision else "revision_observation"
        elif not live or event_kind != "MESSAGE_CREATE":
            classification = "archived_observation"
        elif age < -5:
            classification = "clock_skew_observation"
        elif age > max_age:
            classification = "stale_observation"
        elif parsed.get("errors") != [] or parsed.get("parsed") is not True:
            classification = "unparsed_observation"
        else:
            classification = "fresh_observation"
        timing = {"source_created_utc": _iso(created), "received_at_utc": _iso(received),
                  "source_age_at_receipt_seconds": round(age, 6),
                  "clock_basis": "host_utc_and_discord_snowflake", "independent_server_clock_verified": False}
        snapshot = {"parsed": parsed, "timing": timing, "classification": classification,
                    "observer_only": True, "eligible_for_orders": False}
        encoded = _json(snapshot)
        if len(encoded.encode("utf-8")) > _MAX_SNAPSHOT:
            raise ValueError("normalized_snapshot_too_large")
        with self.db:
            self.db.execute("""INSERT INTO personal_messages VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET last_received_utc=excluded.last_received_utc,
                latest_revision=excluded.latest_revision,deleted=MAX(personal_messages.deleted,excluded.deleted),
                ever_edited=MAX(personal_messages.ever_edited,excluded.ever_edited),latest_snapshot_json=excluded.latest_snapshot_json""",
                (key, message_id, _iso(received), _iso(received), _iso(created), revision, int(deleted), int(edited), encoded))
            self.db.execute("INSERT OR IGNORE INTO personal_revisions VALUES(?,?,?,?,?,?)", (key, revision, event_kind, _iso(received), classification, encoded))
            if classification == "fresh_observation" and event_kind == "MESSAGE_CREATE" and raw_payload is not None:
                proof = _json(raw_payload)
                if len(proof.encode("utf-8")) > _MAX_MESSAGE:
                    raise ValueError("source_proof_too_large")
                self.db.execute("INSERT OR IGNORE INTO personal_raw_proofs VALUES(?,?,?,?,?)", (message_id, revision, event_kind, _iso(received), proof))
            if forwarding and classification == "fresh_observation":
                self._enqueue({"event_kind": "MESSAGE_CREATE", "classification": classification,
                               "source_key": key, "source": source, "parsed": parsed,
                               "snapshot": snapshot, "received_at_utc": _iso(received),
                               "source_created_utc": _iso(created), "revision_sha256": revision,
                               "observer_only": True, "eligible_for_orders": False})
        return classification, timing

    def invalidate(self, message_id: str, received: datetime, kind: str, *, forwarding: bool) -> None:
        previous = self.message(message_id)
        stamp = _iso(received)
        deleted = kind == "MESSAGE_DELETE"
        with self.db:
            # An UPDATE may arrive before the CREATE or its verification GET
            # may fail. Persist its ID immediately, even without a known
            # AlphaAgent record, so an out-of-order CREATE never becomes fresh.
            # Preserve the distinction between an edit and actual deletion;
            # a deletion remains irreversible if a later update is observed.
            reason = "source_deleted" if deleted else "source_edited"
            self.db.execute("""INSERT INTO personal_tombstones VALUES(?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET received_utc=excluded.received_utc,
                reason=CASE WHEN personal_tombstones.reason='source_deleted' OR excluded.reason='source_deleted'
                            THEN 'source_deleted' ELSE 'source_edited' END""", (message_id, stamp, reason))
            if previous:
                self.db.execute("UPDATE personal_messages SET deleted=MAX(deleted,?),ever_edited=MAX(ever_edited,?),last_received_utc=? WHERE message_id=?", (int(deleted), int(not deleted), stamp, message_id))
                self.db.execute("UPDATE personal_forward_outbox SET status='source_invalidated' WHERE source_key=? AND event_kind='MESSAGE_CREATE' AND status='pending'", (previous["source_key"],))
                if forwarding:
                    snapshot = json.loads(previous["latest_snapshot_json"])
                    parsed = snapshot["parsed"]
                    revision = hashlib.sha256(_json([previous["latest_revision"], kind]).encode("utf-8")).hexdigest()
                    self._enqueue({"event_kind": kind, "kind": "deleted" if deleted else "edited",
                                   "classification": "withdrawn_observation" if deleted else "edited_observation",
                                   "source_key": previous["source_key"], "source": parsed["source"], "parsed": parsed,
                                   "received_at_utc": stamp, "source_created_utc": previous["created_utc"],
                                   "revision_sha256": revision, "observer_only": True, "eligible_for_orders": False})

    def counts(self) -> dict[str, int]:
        return {name: self.db.execute(sql).fetchone()[0] for name, sql in {
            "unique_source_messages": "SELECT COUNT(*) FROM personal_messages",
            "revision_snapshots": "SELECT COUNT(*) FROM personal_revisions",
            "withdrawn_source_messages": "SELECT COUNT(*) FROM personal_messages WHERE deleted=1",
            "forward_events_pending": "SELECT COUNT(*) FROM personal_forward_outbox WHERE status='pending'",
            "forward_events_enqueued": "SELECT COUNT(*) FROM personal_forward_outbox WHERE status='enqueued'",
            "pending_update_verifications": "SELECT COUNT(*) FROM personal_update_checks WHERE status='pending'",
            "metadata_only_updates": "SELECT COUNT(*) FROM personal_update_checks WHERE status='metadata_only'",
        }.items()}


def _rest_payload(raw: Any, message_id: str) -> dict[str, Any]:
    """Bind a raw REST response from our already verified, fixed channel.

    SDK Embed.to_dict adds defaults and discards unknown Discord fields. Keep
    the original REST embed dictionaries so revision equality is meaningful.
    """
    if (not isinstance(raw, dict) or raw.get("id") != message_id
            or raw.get("channel_id") != SOURCE_CHANNEL_ID
            or raw.get("guild_id") not in (None, SOURCE_GUILD_ID)
            or len(_json(raw).encode("utf-8")) > _MAX_MESSAGE):
        raise ValueError("source_rest_response_mismatch")
    return dict(raw, guild_id=SOURCE_GUILD_ID)


class PersonalObserver(discord.Client):
    def __init__(self, config: dict[str, Any], runtime: Path, store: _Store, forwarder: Any, check_only: bool):
        # Do not subscribe to other guilds. Gateway may still supply state for
        # the account; application storage and API reads stay source-bound.
        super().__init__(enable_debug_events=True, max_messages=None,
                         chunk_guilds_at_startup=False, guild_subscriptions=False,
                         member_cache_flags=discord.MemberCacheFlags.none(),
                         allowed_mentions=discord.AllowedMentions.none(),
                         sync_presence=False, max_ratelimit_timeout=60.0,
                         proxy=_system_proxy(), proxy_gateway=True)
        self.config, self.runtime, self.store = config, runtime, store
        self.forwarder = None if check_only else forwarder
        self.check_only = check_only
        self.queue: asyncio.Queue[tuple[str, dict[str, Any], datetime, bool, int, str | None]] = asyncio.Queue(maxsize=500)
        self.serial = asyncio.Lock()
        self.bootstrap_serial = asyncio.Lock()
        self.tasks: list[asyncio.Task[Any]] = []
        self.channel: discord.TextChannel | None = None
        self.connected = self.ready_gate = self.account_verified = self.source_verified = False
        self.author_observed = self.content_observed = False
        self.connected_since: datetime | None = None
        self.last_receipt: datetime | None = None
        self.last_live_receipt: datetime | None = None
        self.last_created: datetime | None = None
        self.last_timing: dict[str, Any] | None = None
        self.last_classification: str | None = None
        self.error_category: str | None = None
        self.exit_code = self.epoch = self.rejected = self.bootstrap_count = 0
        self.shutting_down = False

    async def setup_hook(self) -> None:
        self.tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._status_loop()),
                      asyncio.create_task(self._forward_loop())]

    async def login(self, token: str) -> None:
        # Bound initial HTTP authentication in normal long-running mode too.
        await asyncio.wait_for(super().login(token), timeout=60)

    def _status(self) -> None:
        now, latency = utc_now(), self.latency
        atomic_json(self.runtime / "status.json", {
            "version": 1, "mode": "discord_personal_account_observer", "provenance_mode": _MODE,
            "observer_only": True, "eligible_for_orders": False, "broker_used": False,
            "orders_submitted": False, "personal_account_messages_sent": False, "cloud_uploaded": False,
            "connected": self.connected, "bootstrap_complete": self.ready_gate,
            "healthy": bool(self.connected and self.ready_gate and self.account_verified and self.source_verified and self.error_category is None),
            "update_verification_policy": _UPDATE_POLICY,
            "account_verified": self.account_verified, "account_binding_sha256": self.config["account_binding_sha256"],
            "source_channel_readable": self.source_verified, "source_bot_observed": self.author_observed,
            "source_author_authenticated": self.author_observed, "source_provenance_verified": self.author_observed,
            "message_content_available": self.content_observed, "message_content_observed": self.content_observed,
            "source_guild_id": SOURCE_GUILD_ID, "source_channel_id": SOURCE_CHANNEL_ID, "source_author_id": SOURCE_AUTHOR_ID,
            "updated_utc": _iso(now), "connected_since_utc": _iso(self.connected_since) if self.connected_since else None,
            "gateway_heartbeat_latency_seconds": round(latency, 6) if self.connected and math.isfinite(latency) else None,
            "last_verified_receipt_utc": _iso(self.last_receipt) if self.last_receipt else None,
            "last_live_receipt_utc": _iso(self.last_live_receipt) if self.last_live_receipt else None,
            "last_source_created_utc": _iso(self.last_created) if self.last_created else None,
            "last_live_timing": self.last_timing, "last_classification": self.last_classification,
            "bootstrap_messages_seen": self.bootstrap_count, "rejected_messages": self.rejected,
            "queue_size": self.queue.qsize(), "error_category": self.error_category,
            **self.store.counts(),
        })

    async def _fatal(self, category: str) -> None:
        self.error_category, self.exit_code, self.ready_gate = category, 1, False
        LOG.error("personal_observer_error category=%s", category)
        await self.close()

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        await self._fatal("observer_event_failed")

    async def _status_loop(self) -> None:
        try:
            while not self.is_closed():
                self._status()
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fatal("status_storage_failed")

    async def on_disconnect(self) -> None:
        if self.shutting_down:
            return
        self.epoch += 1
        self.connected = self.ready_gate = False
        self.connected_since = None
        self._status()

    async def on_ready(self) -> None:
        await self._bootstrap()

    async def on_resumed(self) -> None:
        await self._bootstrap()

    async def _bootstrap(self) -> None:
        async with self.bootstrap_serial:
            if self.shutting_down:
                return
            self.ready_gate = False
            self.connected = True
            self.connected_since = utc_now()
            try:
                async with self.serial:
                    if self.user is None or self.user.bot or hashlib.sha256(str(self.user.id).encode("utf-8")).hexdigest() != self.config["account_binding_sha256"]:
                        await self._fatal("personal_account_binding_failed")
                        return
                    self.account_verified = True
                    channel = await asyncio.wait_for(self.fetch_channel(int(SOURCE_CHANNEL_ID)), timeout=30)
                    if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != SOURCE_GUILD_ID:
                        await self._fatal("source_binding_failed")
                        return
                    self.channel = channel
                    # The library documents explicit subscribe for clients with
                    # guild_subscriptions=False. This subscribes only this guild.
                    await asyncio.wait_for(channel.guild.subscribe(typing=True, activities=False, threads=False, member_updates=False), timeout=30)

                    async def archive() -> None:
                        messages = await self.http.logs_from(int(SOURCE_CHANNEL_ID), 100)
                        if not isinstance(messages, list) or len(messages) > 100:
                            raise ValueError("source_history_response_invalid")
                        self.source_verified = True
                        for message in reversed(messages):
                            self.bootstrap_count += 1
                            message_id = _id(message.get("id")) if isinstance(message, dict) else None
                            if message_id is None:
                                raise ValueError("source_history_message_invalid")
                            await self._handle("BOOTSTRAP_HISTORY", _rest_payload(message, message_id), utc_now(), False)

                    await asyncio.wait_for(archive(), timeout=60)
                    self.ready_gate = True
                    self.error_category = None
                    self._status()
                if self.check_only:
                    await self.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._fatal(_error(exc))

    async def on_socket_raw_receive(self, raw: Any) -> None:
        # Installed discord.py-self 2.1.0 dispatches the decoded dictionary.
        # Accept text for compatible builds; never treat binary data as JSON.
        if self.shutting_down:
            return
        if isinstance(raw, dict):
            payload = raw
        elif isinstance(raw, str) and len(raw) <= _MAX_FRAME:
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                return
        else:
            return
        if not isinstance(payload, dict):
            return
        if payload.get("op") in (7, 9) or payload.get("t") in ("READY", "RESUMED"):
            self.ready_gate = False
            self.epoch += 1
        event = payload.get("t")
        if payload.get("op") != 0 or event not in _EVENTS:
            return
        data = payload.get("d")
        if not isinstance(data, dict) or data.get("channel_id") != SOURCE_CHANNEL_ID:
            return
        if data.get("guild_id") != SOURCE_GUILD_ID:
            if event == "MESSAGE_CREATE" or data.get("guild_id") is not None or not self.source_verified:
                return
            data = dict(data, guild_id=SOURCE_GUILD_ID)
        if len(_json(data).encode("utf-8")) > _MAX_MESSAGE:
            # An oversized change is still a change. Do not leave an earlier
            # CREATE executable just because we refuse to retain this payload.
            received = utc_now()
            if event == "MESSAGE_UPDATE" and _id(data.get("id")) is not None:
                self.store.invalidate(data["id"], received, "MESSAGE_UPDATE", forwarding=self.forwarder is not None)
            elif event in ("MESSAGE_DELETE", "MESSAGE_DELETE_BULK"):
                ids = data.get("ids") if event == "MESSAGE_DELETE_BULK" else [data.get("id")]
                if isinstance(ids, list):
                    for message_id in ids[:100]:
                        if _id(message_id):
                            self.store.invalidate(message_id, received, "MESSAGE_DELETE", forwarding=self.forwarder is not None)
            self.rejected += 1
            self._status()
            return
        received = utc_now()
        ticket = None
        try:
            if event == "MESSAGE_UPDATE" and _id(data.get("id")) is not None:
                ticket = self.store.begin_update(data["id"], received, data, forwarding=self.forwarder is not None)
                self._status()
            if event in ("MESSAGE_DELETE", "MESSAGE_DELETE_BULK"):
                ids = data.get("ids") if event == "MESSAGE_DELETE_BULK" else [data.get("id")]
                if isinstance(ids, list):
                    for message_id in ids[:100]:
                        if _id(message_id):
                            self.store.invalidate(message_id, received, "MESSAGE_DELETE", forwarding=self.forwarder is not None)
            self.queue.put_nowait((event, data, received, self.ready_gate and self.connected, self.epoch, ticket))
        except asyncio.QueueFull:
            await self._fatal("ingestion_queue_full")

    async def _worker(self) -> None:
        try:
            while not self.is_closed():
                event, data, received, live, epoch, ticket = await self.queue.get()
                try:
                    async with self.serial:
                        await self._handle(event, data, received, live and epoch == self.epoch and self.connected and self.ready_gate, update_ticket=ticket)
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fatal(_error(exc))

    async def _handle(self, event: str, data: dict[str, Any], received: datetime, live: bool, *, update_ticket: str | None = None) -> None:
        message_id = _id(data.get("id"))
        if event == "MESSAGE_UPDATE" and message_id is not None and update_ticket is None:
            update_ticket = self.store.begin_update(message_id, received, data, forwarding=self.forwarder is not None)
        try:
            await self._handle_event(event, data, received, live, update_ticket=update_ticket)
        finally:
            if update_ticket is not None and self.store.pending_ticket(update_ticket):
                self.store.reject_update(update_ticket, message_id, received, "verification_did_not_complete", forwarding=self.forwarder is not None)
                self._status()

    async def _handle_event(self, event: str, data: dict[str, Any], received: datetime, live: bool, *, update_ticket: str | None = None) -> None:
        if event in ("MESSAGE_DELETE", "MESSAGE_DELETE_BULK"):
            ids = data.get("ids") if event == "MESSAGE_DELETE_BULK" else [data.get("id")]
            if isinstance(ids, list):
                for message_id in ids[:100]:
                    if _id(message_id):
                        self.store.invalidate(message_id, received, "MESSAGE_DELETE", forwarding=self.forwarder is not None)
            return
        message_id = _id(data.get("id"))
        if message_id is None:
            self.rejected += 1
            return
        if event == "MESSAGE_UPDATE":
            if update_ticket is None:
                update_ticket = self.store.begin_update(message_id, received, data, forwarding=self.forwarder is not None)
            if self.channel is None:
                self.store.reject_update(update_ticket, message_id, received, "verified_channel_unavailable", forwarding=self.forwarder is not None)
                return
            try:
                data = _rest_payload(await asyncio.wait_for(self.http.get_message(int(SOURCE_CHANNEL_ID), int(message_id)), timeout=30), message_id)
            except asyncio.CancelledError:
                self.store.reject_update(update_ticket, message_id, received, "verification_cancelled", forwarding=self.forwarder is not None)
                raise
            except discord.NotFound:
                self.store.reject_update(update_ticket, message_id, received, "verification_source_deleted", forwarding=self.forwarder is not None, deleted=True)
                return
            except Exception as exc:
                self.store.reject_update(update_ticket, message_id, received, "verification_get_failed", forwarding=self.forwarder is not None)
                self.error_category = _error(exc)
                if self.error_category in ("authentication_failed", "source_permission_denied"):
                    await self._fatal(self.error_category)
                else:
                    self._status()
                return
        def reject_update(reason: str) -> None:
            if event == "MESSAGE_UPDATE":
                self.store.reject_update(update_ticket, message_id, received, reason, forwarding=self.forwarder is not None)
        author = data.get("author")
        flags = data.get("flags", 0)
        if (data.get("guild_id") != SOURCE_GUILD_ID or data.get("channel_id") != SOURCE_CHANNEL_ID
                or not isinstance(author, Mapping) or author.get("id") != SOURCE_AUTHOR_ID or author.get("bot") is not True
                or data.get("webhook_id") is not None or data.get("message_reference") is not None
                or data.get("message_snapshots") or type(flags) is not int or flags < 0 or flags & 10):
            reject_update("source_authentication_rejected")
            self.rejected += 1
            return
        embeds, content = data.get("embeds"), data.get("content", "")
        if not isinstance(embeds, list) or len(embeds) > 10 or not isinstance(content, str) or len(content) > 2000:
            reject_update("source_content_structure_rejected")
            self.rejected += 1
            return
        if len(_json(embeds).encode("utf-8")) > _MAX_SNAPSHOT:
            reject_update("source_content_size_rejected")
            self.rejected += 1
            return
        parsed = parse_message(dict(data, content=content))
        if not parsed.get("metadata_valid"):
            reject_update("source_metadata_rejected")
            self.rejected += 1
            return
        created = _created(message_id)
        api_stamp = datetime.fromisoformat(parsed["source"]["message_timestamp_utc"])
        if abs((api_stamp - created).total_seconds()) > 1:
            reject_update("source_creation_time_rejected")
            self.rejected += 1
            return
        source = parsed["source"]
        source.update(source_authenticated=True, source_author_authenticated=True,
                      provenance_mode=_MODE, original_author_id=SOURCE_AUTHOR_ID,
                      original_timestamp_utc=_iso(created), message_key_scope="direct_original_message")
        parsed["eligible_for_orders"] = False
        self.author_observed = True
        self.content_observed = self.content_observed or bool(content or embeds)
        if event == "MESSAGE_UPDATE" and self.store.metadata_update(update_ticket, parsed, data, received):
            self.last_classification = "metadata_only_observation"
            timing = None
        else:
            if event == "MESSAGE_UPDATE":
                self.store.reject_update(update_ticket, message_id, received, "raw_revision_or_source_changed", forwarding=self.forwarder is not None,
                                         verified_revision=parsed.get("revision_sha256"), verified_payload=data)
            self.last_classification, timing = self.store.observe(parsed, event, received, live, self.config["max_signal_age_seconds"], forwarding=self.forwarder is not None,
                                                                 raw_payload=data if event == "MESSAGE_CREATE" else None)
        self.last_receipt, self.last_created = received, created
        if live:
            self.last_live_receipt = received
            if event == "MESSAGE_CREATE" and self.last_classification == "fresh_observation":
                self.last_timing = timing
        # A failed update verification GET pauses the source. A subsequent
        # authenticated, durably archived message confirms read/ingestion has
        # resumed; permanent authentication/permission failures are never cleared.
        if self.error_category in ("request_timeout", "discord_http_failed", "rate_limited"):
            self.error_category = None
        self._status()

    async def _forward_loop(self) -> None:
        try:
            while not self.is_closed():
                if self.forwarder is not None:
                    row = self.store.db.execute("SELECT * FROM personal_forward_outbox WHERE status='pending' ORDER BY received_utc,event_id LIMIT 1").fetchone()
                    if row:
                        event = json.loads(row["payload_json"])
                        if event["event_kind"] == "MESSAGE_CREATE":
                            stored = self.store.message(event["source"]["message_id"])
                            age = (utc_now() - _created(event["source"]["message_id"])).total_seconds()
                            if (stored is None or stored["deleted"] or stored["ever_edited"] or stored["latest_revision"] != event["revision_sha256"] or not -5 <= age <= self.config["max_signal_age_seconds"]):
                                with self.store.db:
                                    self.store.db.execute("UPDATE personal_forward_outbox SET status='expired_or_invalidated' WHERE event_id=?", (row["event_id"],))
                                continue
                            if self.store.pending_update(event["source"]["message_id"]):
                                await asyncio.sleep(0.25)
                                continue
                            if not (self.connected and self.ready_gate and self.error_category is None):
                                await asyncio.sleep(0.25)
                                continue
                        # enqueue is a local durable write, not a network send.
                        await self.forwarder.enqueue(event)
                        with self.store.db:
                            self.store.db.execute("UPDATE personal_forward_outbox SET status='enqueued' WHERE event_id=?", (row["event_id"],))
                        continue
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fatal("forward_enqueue_failed")

    async def close(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        self.connected = self.ready_gate = False
        self.connected_since = None
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(task for task in self.tasks if task is not current), return_exceptions=True)
        with suppress(Exception):
            self._status()
        await super().close()


async def run_observer(config: dict[str, Any], token: str, runtime: Path, forwarder: Any = None,
                       stop_event: asyncio.Event | None = None, check_only: bool = False) -> int:
    """Run one source-bound observer; caller owns credential loading/forwarder.

    ``forwarder.enqueue(event)`` must durably deduplicate normalized events.
    It must never interpret this observer-only stream as permission to trade.
    A 0 return means orderly stop; a fixed status category explains failures.
    """
    _silence_sdk_logs()
    lock: _ProcessLock | None = None
    store: _Store | None = None
    client: PersonalObserver | None = None
    stopper: asyncio.Task[Any] | None = None
    try:
        if version("discord.py-self") != "2.1.0":
            raise ValueError("personal_sdk_version_mismatch")
        for name, expected in (("source_guild_id", SOURCE_GUILD_ID), ("source_channel_id", SOURCE_CHANNEL_ID), ("source_author_id", SOURCE_AUTHOR_ID)):
            if config.get(name) != expected:
                raise ValueError("fixed_source_binding_required")
        if config.get("observer_only") is not True or not isinstance(config.get("account_binding_sha256"), str) or not _SHA.fullmatch(config["account_binding_sha256"]):
            raise ValueError("personal_observer_binding_required")
        max_age = config.get("max_signal_age_seconds", 120)
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or not 1 <= max_age <= 120:
            raise ValueError("bounded_freshness_required")
        config = dict(config, max_signal_age_seconds=float(max_age))
        if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise ValueError("personal_credential_unavailable")
        expected_runtime = Path(__file__).resolve().parents[1] / "runtime" / "alpha_personal_local"
        runtime = Path(runtime)
        if runtime.resolve() != expected_runtime.resolve() or runtime.is_symlink() or runtime.parent.is_symlink():
            raise ValueError("isolated_personal_runtime_required")
        runtime.mkdir(parents=True, exist_ok=True)
        lock = _ProcessLock(runtime / "observer.lock")
        binding = {name: config[name] for name in ("source_guild_id", "source_channel_id", "source_author_id", "account_binding_sha256")}
        binding["provenance_mode"] = _MODE
        store = _Store(runtime / "personal_observer.sqlite3", binding, uuid.uuid4().hex)
        client = PersonalObserver(config, runtime, store, forwarder, check_only)
        client._status()
        if stop_event is not None:
            async def stop_on_request() -> None:
                await stop_event.wait()
                await client.close()
            stopper = asyncio.create_task(stop_on_request())
        async def session() -> None:
            # SDK __aenter__ initializes its HTTP transport and performs
            # startup network reads before login; bound that stage as well.
            await asyncio.wait_for(client.__aenter__(), timeout=60)
            try:
                await client.start(token, reconnect=not check_only)
            finally:
                await client.__aexit__(None, None, None)

        if check_only:
            await asyncio.wait_for(session(), timeout=120)
        else:
            await session()
        return client.exit_code
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        category = _error(exc)
        if client is not None:
            client.error_category, client.exit_code = category, 1
            with suppress(Exception):
                await client.close()
        LOG.error("personal_observer_error category=%s", category)
        return 1
    finally:
        if stopper is not None:
            stopper.cancel()
        if store is not None:
            store.db.close()
        if lock is not None:
            lock.close()
