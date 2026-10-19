"""Read-only provenance adapter for the independent personal-account observer.

This module neither authenticates to Discord nor reads credentials. It consumes
the observer's original-source database directly; informational webhook posts
and native announcement Follow deliveries are deliberately different sources.
The observer remains read-only. Trading authorization belongs to the copier's
separate configuration and its account, fill, freshness and risk checks.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Mapping


SOURCE_MODE = "direct_personal_account"
SOURCE_GUILD = "1434960637561409689"
SOURCE_CHANNEL = "1492437553171660921"
SOURCE_AUTHOR = "1462345436852654295"
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_UPDATE_VERIFICATION_POLICY = "verified_embed_transport_v2"
_SCHEMA = {
    "personal_metadata": ("key", "value"),
    "personal_messages": ("source_key", "message_id", "first_received_utc", "last_received_utc",
                          "created_utc", "latest_revision", "deleted", "ever_edited", "latest_snapshot_json"),
    "personal_revisions": ("source_key", "revision", "event_kind", "received_utc", "classification", "snapshot_json"),
    "personal_tombstones": ("message_id", "received_utc", "reason"),
    "personal_update_checks": ("ticket", "message_id", "received_utc", "resolved_utc", "status",
                               "baseline_revision", "verified_revision", "audit_json"),
}


class PersonalSourceError(ValueError):
    def __init__(self, category: str, *, permanent: bool = False):
        super().__init__(category)
        self.category, self.permanent = category, permanent


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _time(value: Any, error_class=PersonalSourceError) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise error_class("timestamp_unconfirmed") from None


def _id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None and int(value) < 2**64


def _created(message_id: Any, error_class=PersonalSourceError) -> datetime:
    if not _id(message_id):
        raise error_class("source_identifier_unconfirmed")
    return datetime.fromtimestamp(((int(message_id) >> 22) + 1420070400000) / 1000, timezone.utc)


def personal_binding(config: Mapping[str, Any], *, error_class=PersonalSourceError) -> dict[str, Any]:
    """Return the exact nonsecret binding used by the personal observer."""
    expected = {"source_guild_id": SOURCE_GUILD, "source_channel_id": SOURCE_CHANNEL,
                "source_author_id": SOURCE_AUTHOR}
    account = config.get("source_account_binding_sha256")
    if (config.get("source_mode") != SOURCE_MODE or any(config.get(k) != v for k, v in expected.items())
            or not isinstance(account, str) or _SHA.fullmatch(account) is None
            or config.get("approved_follower_webhook_id") is not None):
        raise error_class("personal_source_binding_required", permanent=True)
    return dict(expected, account_binding_sha256=account, provenance_mode=SOURCE_MODE)


class PersonalSource:
    """Read personal observer evidence without modifying its database or state."""

    def __init__(self, config: Mapping[str, Any], runtime: Path, *, error_class=PersonalSourceError):
        self.error_class = error_class
        self.config = dict(config)
        self.binding = personal_binding(config, error_class=error_class)
        self.binding_sha256 = hashlib.sha256(_json(self.binding).encode("utf-8")).hexdigest()
        self.runtime = Path(runtime).absolute()
        self.db: sqlite3.Connection | None = None
        self.status_max_age = config.get("observer_status_max_age_seconds", 20)
        self.max_age = config.get("max_signal_age_seconds", 120)
        for value, upper in ((self.status_max_age, 30), (self.max_age, 120)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= upper:
                raise error_class("invalid_timing_policy", permanent=True)

    def _ordinary(self, path: Path) -> None:
        for item in reversed((path,) + tuple(path.parents)):
            try:
                info = item.lstat()
            except FileNotFoundError:
                raise self.error_class("observer_database_unavailable") from None
            if stat.S_ISLNK(info.st_mode):
                raise self.error_class("linked_personal_source_refused", permanent=True)

    def validate_binding(self) -> None:
        path = self.runtime / "personal_observer.sqlite3"
        self._ordinary(path)
        if not path.is_file():
            raise self.error_class("observer_database_unavailable")
        if self.db is None:
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON")
        for table, columns in _SCHEMA.items():
            # Table names are fixed source-code constants, never configuration.
            actual = tuple(row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")"))
            if actual != columns:
                raise self.error_class("personal_observer_schema_mismatch", permanent=True)
        metadata = dict(self.db.execute("SELECT key,value FROM personal_metadata WHERE key IN ('schema_version','binding_sha256','update_verification_policy')"))
        if metadata != {"schema_version": "1", "binding_sha256": self.binding_sha256,
                        "update_verification_policy": _UPDATE_VERIFICATION_POLICY}:
            raise self.error_class("personal_observer_binding_mismatch", permanent=True)

    def feed(self) -> dict[str, Any]:
        self.validate_binding()
        path = self.runtime / "status.json"
        self._ordinary(path)
        try:
            with path.open("rb") as stream:
                raw = stream.read(131073)
            if len(raw) > 131072:
                raise ValueError
            status = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            raise self.error_class("observer_status_unavailable") from None
        now = datetime.now(timezone.utc)
        required = ("connected", "bootstrap_complete", "healthy", "account_verified", "source_channel_readable",
                    "source_bot_observed", "source_author_authenticated", "source_provenance_verified", "message_content_observed")
        if (not isinstance(status, dict) or type(status.get("version")) is not int or status.get("version") != 1
                or status.get("mode") != "discord_personal_account_observer"
                or status.get("provenance_mode") != SOURCE_MODE or status.get("observer_only") is not True
                or status.get("eligible_for_orders") is not False or status.get("broker_used") is not False
                or status.get("orders_submitted") is not False
                or status.get("personal_account_messages_sent") is not False
                or any(status.get(k) is not True for k in required)
                or any(status.get(k) != self.binding[k] for k in ("source_guild_id", "source_channel_id", "source_author_id", "account_binding_sha256"))
                or status.get("error_category") is not None
                or not -5 <= (now - _time(status.get("updated_utc"), self.error_class)).total_seconds() <= self.status_max_age
                or (now - _time(status.get("connected_since_utc"), self.error_class)).total_seconds() < -5):
            raise self.error_class("observer_not_live_healthy")
        return status

    def _snapshot(self, key: str, row: sqlite3.Row, raw: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32768:
            raise self.error_class("source_snapshot_invalid")
        try:
            snapshot = json.loads(raw)
        except (ValueError, TypeError):
            raise self.error_class("source_snapshot_invalid") from None
        parsed = snapshot.get("parsed") if isinstance(snapshot, dict) else None
        timing = snapshot.get("timing") if isinstance(snapshot, dict) else None
        if (not isinstance(parsed, dict) or not isinstance(timing, dict)
                or snapshot.get("observer_only") is not True or snapshot.get("eligible_for_orders") is not False
                or parsed.get("eligible_for_orders") is not False or parsed.get("parsed") is not True
                or parsed.get("metadata_valid") is not True or parsed.get("parser_version") != "alpha_embed_v2"
                or parsed.get("errors") != [] or not isinstance(row["latest_revision"], str)
                or _SHA.fullmatch(row["latest_revision"]) is None
                or parsed.get("revision_sha256") != row["latest_revision"]):
            raise self.error_class("unsupported_source_syntax")
        source = parsed.get("source")
        if not isinstance(source, dict):
            raise self.error_class("source_provenance_mismatch", permanent=True)
        reference = source.get("message_reference")
        expected_key = "discord:" + ":".join((SOURCE_GUILD, SOURCE_CHANNEL, row["message_id"]))
        flags = source.get("flags")
        if (key != expected_key or source.get("message_key") != key
                or source.get("message_key_scope") != "direct_original_message"
                or source.get("provenance_mode") != SOURCE_MODE
                or source.get("source_authenticated") is not True or source.get("source_author_authenticated") is not True
                or source.get("guild_id") != SOURCE_GUILD or source.get("channel_id") != SOURCE_CHANNEL
                or source.get("message_id") != row["message_id"] or source.get("author_id") != SOURCE_AUTHOR
                or source.get("original_author_id") != SOURCE_AUTHOR or source.get("author_is_bot") is not True
                or source.get("webhook_id") is not None or source.get("edited_timestamp_utc") is not None
                or not isinstance(reference, dict) or any(v is not None for v in reference.values())
                or type(flags) is not int or flags < 0 or flags & 10):
            raise self.error_class("source_provenance_mismatch", permanent=True)
        created = _created(row["message_id"], self.error_class)
        if (abs((_time(source.get("message_timestamp_utc"), self.error_class) - created).total_seconds()) > 1
                or _time(source.get("original_timestamp_utc"), self.error_class) != created
                or _time(row["created_utc"], self.error_class) != created
                or _time(timing.get("source_created_utc"), self.error_class) != created):
            raise self.error_class("source_creation_time_mismatch", permanent=True)
        return parsed, timing

    def invalidated(self, key: str, revision: str) -> bool:
        self.validate_binding()
        row = self.db.execute("SELECT * FROM personal_messages WHERE source_key=?", (key,)).fetchone()
        if row is None or row["deleted"] or row["ever_edited"] or row["latest_revision"] != revision:
            return True
        count = self.db.execute("SELECT COUNT(DISTINCT revision) FROM personal_revisions WHERE source_key=?", (key,)).fetchone()[0]
        tombstone = self.db.execute("SELECT 1 FROM personal_tombstones WHERE message_id=?", (row["message_id"],)).fetchone()
        if count != 1 or tombstone is not None:
            return True
        # A verification ticket temporarily blocks source-authorized writes;
        # it does not establish an edit or withdrawal of an accepted entry.
        # Any genuine prior edit/deletion above remains irreversible.
        pending = self.db.execute("SELECT 1 FROM personal_update_checks WHERE message_id=? AND status='pending' LIMIT 1",
                                  (row["message_id"],)).fetchone()
        if pending is not None:
            raise self.error_class("source_update_verification_pending")
        return False

    def source(self, key: str, expected_revision: str | None, activated_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if expected_revision is not None and (not isinstance(expected_revision, str) or _SHA.fullmatch(expected_revision) is None):
            raise self.error_class("source_revision_unconfirmed", permanent=True)
        feed = self.feed()
        row = self.db.execute("SELECT * FROM personal_messages WHERE source_key=?", (key,)).fetchone()
        if row is None or self.invalidated(key, expected_revision or row["latest_revision"]):
            raise self.error_class("source_deleted_or_revised")
        revision = self.db.execute("SELECT * FROM personal_revisions WHERE source_key=? AND revision=? AND event_kind='MESSAGE_CREATE'",
                                   (key, row["latest_revision"])).fetchone()
        if revision is None or revision["classification"] != "fresh_observation":
            raise self.error_class("non_live_or_replayed_source")
        parsed, timing = self._snapshot(key, row, revision["snapshot_json"])
        if json.loads(revision["snapshot_json"]).get("classification") != "fresh_observation":
            raise self.error_class("source_snapshot_invalid")
        received = _time(timing.get("received_at_utc"), self.error_class)
        if (received < _time(activated_at, self.error_class)
                or received < _time(feed.get("connected_since_utc"), self.error_class)
                or _time(row["first_received_utc"], self.error_class) != received
                or _time(revision["received_utc"], self.error_class) != received):
            raise self.error_class("source_before_live_activation")
        now = datetime.now(timezone.utc)
        if not -5 <= (now - _created(row["message_id"], self.error_class)).total_seconds() <= self.max_age:
            raise self.error_class("source_expired")
        if not -5 <= (now - received).total_seconds() <= self.max_age:
            raise self.error_class("source_delivery_expired")
        # The observer may commit an UPDATE ticket while this snapshot is
        # being decoded. Recheck immediately before granting source evidence.
        if self.invalidated(key, row["latest_revision"]):
            raise self.error_class("source_deleted_or_revised")
        return parsed, {"revision": row["latest_revision"], "original_message_id": row["message_id"], "received_utc": revision["received_utc"]}

    def candidates(self, cutoff: datetime, maximum: int = 100) -> list[tuple[str]]:
        self.validate_binding()
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise self.error_class("invalid_candidate_limit", permanent=True)
        rows = self.db.execute("""SELECT m.source_key FROM personal_messages m
            JOIN personal_revisions r ON r.source_key=m.source_key AND r.revision=m.latest_revision
            WHERE m.deleted=0 AND m.ever_edited=0 AND m.first_received_utc>=?
              AND r.event_kind='MESSAGE_CREATE' AND r.classification='fresh_observation'
            ORDER BY m.first_received_utc DESC,m.source_key DESC LIMIT ?""", (cutoff.isoformat(), maximum)).fetchall()
        return [(row[0],) for row in reversed(rows)]

    def verified_gate(self) -> dict[str, str]:
        """Prove an actual supported source format; history never grants orders."""
        self.feed()
        rows = self.db.execute("""SELECT * FROM personal_messages WHERE deleted=0 AND ever_edited=0
            ORDER BY last_received_utc DESC LIMIT 1000""").fetchall()
        for row in rows:
            try:
                if self.invalidated(row["source_key"], row["latest_revision"]):
                    continue
                parsed, _ = self._snapshot(row["source_key"], row, row["latest_snapshot_json"])
                if parsed.get("action_kind") not in ("open", "close") or not isinstance(parsed.get("contract"), dict):
                    continue
                if self.invalidated(row["source_key"], row["latest_revision"]):
                    continue
            except self.error_class as exc:
                if exc.permanent:
                    raise
                continue
            return {"source_mode": SOURCE_MODE, "source_key": row["source_key"],
                    "source_account_binding_sha256": self.binding["account_binding_sha256"],
                    "source_author_id": SOURCE_AUTHOR, "source_binding_sha256": self.binding_sha256}
        raise self.error_class("actual_signal_format_not_observed")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None


def read_verified_gate(config: Mapping[str, Any], runtime: Path) -> dict[str, str]:
    """Read-only entry point for a separately authorized cutover controller."""
    reader = PersonalSource(config, runtime)
    try:
        return reader.verified_gate()
    finally:
        reader.close()
