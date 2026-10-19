"""Bounded, read-only local evidence report; never authorizes or sends orders.

Run with ``python -m alpha_discord.diagnostics --observer-runtime PATH
--copier-runtime PATH``. These are separate SQLite snapshots, not an atomic
cross-service snapshot and not a current broker account or position check.
Only fixed diagnostic categories and validated trade fields leave this module.
Nonempty WAL files are refused. For a running service, an operator must supply
an independently obtained consistent static snapshot; this tool never creates
backups, checkpoints a database, or touches the production shared-memory index.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any


_SOURCE_PREFIX = "discord:1434960637561409689:1492437553171660921:"
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_OCC = re.compile(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}\Z")
_TIER = re.compile(r"T[1-9][0-9]?\Z")
_DECIMAL = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,8})?\Z")
_MAX_DB = 128 * 1024 * 1024
_MAX_WAL = 64 * 1024 * 1024
_MAX_SHM = 8 * 1024 * 1024
_MAX_STATE = 8 * 1024 * 1024
_MAX_SNAPSHOT = 256 * 1024
_MAX_AUDIT = 1024 * 1024
_MAX_RECORDS = 10000
_MAX_REVISIONS = 2000
_CLASSIFICATIONS = frozenset({"fresh_observation", "duplicate_observation", "revision_observation",
    "archived_observation", "clock_skew_observation", "stale_observation", "unparsed_observation",
    "edited_observation", "withdrawn_observation"})
_RESOLUTIONS = frozenset({"no_pristine_live_create", "gateway_edit_timestamp",
    "verification_interrupted_on_restart", "verification_did_not_complete", "verification_cancelled",
    "verification_source_deleted", "verification_get_failed", "verified_channel_unavailable",
    "source_authentication_rejected", "source_content_size_rejected", "source_content_structure_rejected",
    "source_metadata_rejected", "source_creation_time_rejected", "raw_revision_or_source_changed",
    "verified_full_content_and_embed_transport", "verified_full_revision_unchanged"})
_DECISIONS = frozenset({"intent_buy_to_open_budget_range", "intent_buy_to_open_one", "intent_sell_to_close",
    "preview_only", "entry_signal_too_old", "source_expired_while_waiting_for_safety_gate",
    "source_expired", "source_delivery_expired", "source_before_live_activation", "source_deleted_or_revised",
    "source_snapshot_invalid", "source_receipt_binding_changed", "non_live_or_replayed_source",
    "entry_price_above_reference_cap", "entry_reference_price_missing_or_invalid", "broker_quote_expired",
    "broker_quote_unconfirmed", "cash_or_option_permission_insufficient", "copy_trade_or_shared_budget_exceeded",
    "no_whole_contract_quantity_in_authorized_budget_range", "same_occ_and_tier_already_owned_or_pending",
    "close_has_no_unique_owned_occ_and_tier", "close_opening_reference_mismatch", "owned_exit_review_required",
    "owned_exit_already_pending", "unsupported_source_action", "unsupported_open_direction",
    "unsupported_close_direction_or_contract", "operator_review_required", "market_closed",
    "source_update_verification_pending", "source_withdrawn", "source_ever_revised", "stopped"})
_PHASES = frozenset({"INTENT", "UNKNOWN", "PENDING", "TERMINAL", "NOT_SENT"})
_BROKER_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
    "held", "pending_cancel", "pending_replace", "stopped", "suspended", "calculated", "done_for_day",
    "filled", "canceled", "cancelled", "expired", "rejected", "failed", "voided"})
_OBSERVER_SCHEMA = {
    "personal_messages": {"source_key", "message_id", "first_received_utc", "last_received_utc", "created_utc",
                          "latest_revision", "deleted", "ever_edited", "latest_snapshot_json"},
    "personal_revisions": {"source_key", "revision", "event_kind", "received_utc", "classification", "snapshot_json"},
    "personal_tombstones": {"message_id", "received_utc", "reason"},
}
_UPDATE_SCHEMA = {"ticket", "message_id", "received_utc", "resolved_utc", "status",
                  "baseline_revision", "verified_revision", "audit_json"}


class _Unavailable(ValueError):
    """Only constants supplied by this module may become output categories."""


def _ordinary(path: Path, *, directory: bool = False, maximum: int | None = None):
    for item in reversed((path,) + tuple(path.parents)):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise _Unavailable("linked_path_refused")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise _Unavailable("runtime_not_directory")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise _Unavailable("database_or_sidecar_not_ordinary")
    if maximum is not None and (info.st_size < 0 or info.st_size > maximum):
        raise _Unavailable("database_or_sidecar_size_limit")
    return info


def _identity(info) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_size, info.st_mtime_ns)


def _sidecar(path: Path, maximum: int, owner: int):
    try:
        info = _ordinary(path, maximum=maximum)
    except FileNotFoundError:
        return None
    if info.st_uid != owner:
        raise _Unavailable("sidecar_owner_mismatch")
    return info


@contextmanager
def _read_database(runtime: Path, filename: str):
    # Reject traversal before normalization; check every ancestor for links.
    if ".." in runtime.parts:
        raise _Unavailable("runtime_traversal_refused")
    if str(runtime).startswith(("\\\\", "//")):
        raise _Unavailable("network_path_refused")
    runtime = Path(os.path.abspath(runtime))
    _ordinary(runtime, directory=True)
    path = runtime / filename
    before = _ordinary(path, maximum=_MAX_DB)
    wal_path, shm_path = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    wal = _sidecar(wal_path, _MAX_WAL, before.st_uid)
    shm = _sidecar(shm_path, _MAX_SHM, before.st_uid)
    journal_path = Path(str(path) + "-journal")
    journal = _sidecar(journal_path, _MAX_WAL, before.st_uid)
    if journal is not None and journal.st_size:
        raise _Unavailable("rollback_journal_requires_consistent_copy")
    if wal is not None and wal.st_size > 0:
        raise _Unavailable("active_wal_requires_consistent_snapshot")
    # Even mode=ro can write an existing shared-memory index or create it after
    # a sidecar race. Never pass an active WAL to SQLite in the source directory.
    # Immutable access does not open, create, or modify SQLite sidecars.
    uri = path.as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_STATE + 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
        deadline = time.monotonic() + 8
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        yield connection
        after = _ordinary(path, maximum=_MAX_DB)
        current_wal = _sidecar(wal_path, _MAX_WAL, before.st_uid)
        current_shm = _sidecar(shm_path, _MAX_SHM, before.st_uid)
        current_journal = _sidecar(journal_path, _MAX_WAL, before.st_uid)
        if current_journal is not None and current_journal.st_size:
            raise _Unavailable("snapshot_changed_during_read")
        sidecars_changed = any(
            (_identity(old) if old is not None else None) != (_identity(new) if new is not None else None)
            for old, new in ((wal, current_wal), (shm, current_shm), (journal, current_journal)))
        if _identity(before) != _identity(after) or sidecars_changed or (current_wal is not None and current_wal.st_size):
            raise _Unavailable("snapshot_changed_during_read")
    finally:
        connection.close()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _Unavailable("duplicate_json_key")
        result[key] = value
    return result


def _object(raw: Any, maximum: int) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum:
        raise _Unavailable("json_size_or_type_limit")
    value = json.loads(raw, object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(_Unavailable("nonfinite_json_refused")))
    if not isinstance(value, dict):
        raise _Unavailable("json_object_required")
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 100000 or depth > 32:
            raise _Unavailable("json_structure_limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def _time(value: Any) -> str | None:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or not 2000 <= stamp.year <= 2100:
            return None
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None and int(value) < 2**64


def _key(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_SOURCE_PREFIX) and _id(value[len(_SOURCE_PREFIX):])


def _category(value: Any, choices: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in choices else fallback


def _quantity(value: Any, *, nullable: bool = False) -> int | None:
    if type(value) is int and 0 <= value <= 5000:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 4 and 0 <= int(value) <= 5000:
        return int(value)
    if nullable:
        return None
    raise _Unavailable("order_quantity_unconfirmed")


def _price(value: Any) -> str | None:
    if type(value) not in (str, int, float) or isinstance(value, bool):
        return None
    raw = str(value)
    if _DECIMAL.fullmatch(raw) is None:
        return None
    try:
        number = Decimal(raw)
        return format(number, "f") if number.is_finite() and 0 <= number <= 1000000 else None
    except InvalidOperation:
        return None


def _schema(db: sqlite3.Connection, required: dict[str, set[str]]) -> set[str]:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 129")}
    if len(tables) > 128:
        raise _Unavailable("database_table_limit")
    for name, columns in required.items():
        if name not in tables or not columns.issubset({row[1] for row in db.execute("PRAGMA table_info(" + name + ")")}):
            raise _Unavailable("database_schema_unavailable")
    return tables


def _source_records(runtime: Path, limit: int) -> dict[str, Any]:
    records = []
    with _read_database(runtime, "personal_observer.sqlite3") as db:
        tables = _schema(db, _OBSERVER_SCHEMA)
        updates_available = "personal_update_checks" in tables
        if updates_available:
            _schema(db, {"personal_update_checks": _UPDATE_SCHEMA})
        rows = db.execute("""SELECT source_key,message_id,first_received_utc,last_received_utc,created_utc,
            deleted,ever_edited,CASE WHEN length(CAST(latest_snapshot_json AS BLOB))<=?
            THEN latest_snapshot_json ELSE NULL END AS latest_snapshot_json FROM personal_messages
            ORDER BY first_received_utc DESC,source_key DESC LIMIT ?""", (_MAX_SNAPSHOT, limit)).fetchall()
        for row in rows:
            key, message_id = row["source_key"], row["message_id"]
            if not _key(key) or not _id(message_id) or key != _SOURCE_PREFIX + message_id:
                raise _Unavailable("source_identity_unconfirmed")
            if row["deleted"] not in (0, 1) or row["ever_edited"] not in (0, 1):
                raise _Unavailable("source_marker_unconfirmed")
            snapshot = _object(row["latest_snapshot_json"], _MAX_SNAPSHOT)
            parsed = snapshot.get("parsed")
            if not isinstance(parsed, dict):
                raise _Unavailable("source_snapshot_unconfirmed")
            revisions = db.execute("SELECT event_kind,classification FROM personal_revisions WHERE source_key=? LIMIT ?", (key, _MAX_REVISIONS + 1)).fetchall()
            checks = (db.execute("SELECT status,resolved_utc,received_utc FROM personal_update_checks WHERE message_id=? ORDER BY received_utc DESC,ticket DESC LIMIT ?", (message_id, _MAX_REVISIONS + 1)).fetchall()
                      if updates_available else [])
            tombstone = db.execute("SELECT reason FROM personal_tombstones WHERE message_id=?", (message_id,)).fetchone()
            if len(revisions) > _MAX_REVISIONS or len(checks) > _MAX_REVISIONS:
                raise _Unavailable("source_history_row_limit")
            pending = sum(item["status"] == "pending" for item in checks)
            state = ("source_invalidated" if row["deleted"] or row["ever_edited"] or tombstone else
                     "update_verification_pending" if pending else "recorded_observation")
            contract = parsed.get("contract") if isinstance(parsed.get("contract"), dict) else {}
            occ = contract.get("occ_symbol")
            tier = parsed.get("signal_tier")
            latest_check = None
            if checks:
                audit_row = db.execute("""SELECT CASE WHEN length(CAST(audit_json AS BLOB))<=?
                    THEN audit_json ELSE NULL END FROM personal_update_checks WHERE message_id=?
                    ORDER BY received_utc DESC,ticket DESC LIMIT 1""", (_MAX_AUDIT, message_id)).fetchone()
                audit = _object(audit_row[0], _MAX_AUDIT)
                latest_check = {"status": _category(checks[0]["status"], frozenset({"pending", "metadata_only", "invalidated"}), "unrecognized_update_status"),
                    "resolution": _category(audit.get("resolution"), _RESOLUTIONS, "unrecognized_or_absent_resolution"),
                    "received_utc": _time(checks[0]["received_utc"]), "resolved_utc": _time(checks[0]["resolved_utc"])}
            records.append({"source_key": key, "source_message_id": message_id,
                "source_created_utc": _time(row["created_utc"]), "first_received_utc": _time(row["first_received_utc"]),
                "last_received_utc": _time(row["last_received_utc"]),
                "trade": {"action": _category(parsed.get("action_kind"), frozenset({"open", "close"}), "unrecognized_action"),
                    "occ": occ if isinstance(occ, str) and _OCC.fullmatch(occ) else None,
                    "tier": tier if isinstance(tier, str) and _TIER.fullmatch(tier) else None,
                    "reference_price_decimal": _price(parsed.get("entry_reference_price_decimal") if parsed.get("action_kind") == "open" else parsed.get("exit_reference_price_decimal"))},
                "source_state": state, "deleted_marker": bool(row["deleted"]), "edited_marker": bool(row["ever_edited"]),
                "tombstone_reason": _category(tombstone["reason"], frozenset({"source_deleted", "source_edited"}), "unrecognized_tombstone") if tombstone else None,
                "latest_classification": _category(snapshot.get("classification"), _CLASSIFICATIONS, "unrecognized_classification"),
                "recorded_live_create_count": sum(item["event_kind"] == "MESSAGE_CREATE" and item["classification"] == "fresh_observation" for item in revisions),
                "revision_record_count": len(revisions), "update_checks_available": updates_available,
                "pending_update_count": pending if updates_available else None,
                "latest_update_check": latest_check})
    return {"available": True, "category": "local_observer_snapshot", "records": records}


def _copier_snapshot(runtime: Path) -> dict[str, Any]:
    with _read_database(runtime, "copier.sqlite3") as db:
        _schema(db, {"engine_state": {"id", "payload"}})
        rows = db.execute("SELECT id,payload FROM engine_state LIMIT 2").fetchall()
        if len(rows) != 1 or rows[0]["id"] != 1:
            raise _Unavailable("copier_state_row_unconfirmed")
        state = _object(rows[0]["payload"], _MAX_STATE)
        for field in ("orders", "decisions", "lots", "pending_sources"):
            value = state.get(field, {} if field == "pending_sources" else None)
            if (not isinstance(value, dict) or len(value) > _MAX_RECORDS
                    or (field != "pending_sources" and any(not isinstance(item, dict) for item in value.values()))):
                raise _Unavailable("copier_state_structure_unconfirmed")
        if state.get("schema_version") != 1:
            raise _Unavailable("copier_state_version_unconfirmed")
    return state


def _execution(record: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {"available": False, "category": "copier_snapshot_unavailable", "orders": None}
    key = record["source_key"]
    matched = [item for item in state["orders"].values() if item.get("source_key") == key]
    if len(matched) > 100:
        raise _Unavailable("per_signal_order_limit")
    orders = []
    for item in matched:
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise _Unavailable("order_payload_unconfirmed")
        requested, filled = _quantity(payload.get("qty")), _quantity(item.get("filled_qty"))
        if requested == 0 or filled > requested:
            raise _Unavailable("order_quantity_unconfirmed")
        phase = _category(item.get("phase"), _PHASES, "unrecognized_phase")
        side = _category(payload.get("side"), frozenset({"buy", "sell"}), "unrecognized_side")
        if phase == "unrecognized_phase" or side == "unrecognized_side" or type(item.get("write_attempted")) is not bool:
            raise _Unavailable("order_evidence_unconfirmed")
        occ = payload.get("symbol")
        if not isinstance(occ, str) or _OCC.fullmatch(occ) is None:
            raise _Unavailable("order_contract_unconfirmed")
        orders.append({"side": side, "occ": occ, "requested_qty": requested, "recorded_filled_qty": filled,
            "phase": phase, "write_attempted": item["write_attempted"],
            "broker_status": _category(item.get("broker_status"), _BROKER_STATUSES, "unrecognized_or_absent_broker_status"),
            "recorded_average_fill_price_decimal": _price(item.get("avg_price")) if filled else None,
            "created_utc": _time(item.get("created_at")), "last_observed_utc": _time(item.get("observed_at"))})
    decision = state["decisions"].get(key)
    category = ("recorded_fill" if any(item["recorded_filled_qty"] for item in orders) else
                "submission_outcome_unconfirmed" if any(item["phase"] == "UNKNOWN" for item in orders) else
                "broker_response_recorded" if any(item["phase"] in ("PENDING", "TERMINAL") for item in orders) else
                "submission_attempt_recorded" if any(item["write_attempted"] for item in orders) else
                "intent_recorded_without_submission_evidence" if orders else "no_order_record_in_copier_snapshot")
    return {"available": True, "category": category, "order_record_count": len(orders), "orders": orders,
        "recorded_buy_filled_qty": sum(item["recorded_filled_qty"] for item in orders if item["side"] == "buy"),
        "recorded_sell_filled_qty": sum(item["recorded_filled_qty"] for item in orders if item["side"] == "sell"),
        "unknown_order_count": sum(item["phase"] == "UNKNOWN" for item in orders),
        "pending_source_recorded": key in state.get("pending_sources", {}),
        "decision_recorded": decision is not None,
        "decision_category": _category(decision.get("decision"), _DECISIONS, "other_recorded_decision") if decision else None,
        "decision_utc": _time(decision.get("observed_at")) if decision else None}


def _failure(exc: Exception) -> dict[str, Any]:
    category = (str(exc) if isinstance(exc, _Unavailable) else
                "database_or_runtime_missing" if isinstance(exc, FileNotFoundError) else
                "database_read_failed" if isinstance(exc, sqlite3.Error) else
                "local_file_access_failed" if isinstance(exc, OSError) else "snapshot_content_unconfirmed")
    return {"available": False, "category": category}


def diagnose(observer_runtime: Path, copier_runtime: Path, limit: int = 20) -> dict[str, Any]:
    """Return sanitized evidence; this API grants no source or order permission."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise _Unavailable("invalid_record_limit")
    try:
        observer = _source_records(observer_runtime, limit)
    except Exception as exc:
        observer = _failure(exc)
    state = None
    try:
        state = _copier_snapshot(copier_runtime)
        copier = {"available": True, "category": "local_copier_snapshot",
                  "order_record_count": len(state["orders"]), "decision_record_count": len(state["decisions"])}
    except Exception as exc:
        copier = _failure(exc)
    records = observer.pop("records", [])
    for record in records:
        try:
            record["execution_evidence"] = _execution(record, state)
        except Exception as exc:
            record["execution_evidence"] = _failure(exc)
    complete = bool(observer.get("available") and copier.get("available")
                    and all(record["execution_evidence"].get("available") for record in records))
    return {"version": 1, "mode": "local_read_only_signal_diagnostics", "read_at_utc": datetime.now(timezone.utc).isoformat(),
        "diagnostic_complete": complete,
        "read_only": True, "network_used": False, "broker_used": False, "orders_submitted": False,
        "evidence_scope": "separate_local_ledger_snapshots_not_current_broker_state",
        "no_order_record_does_not_prove_no_broker_order": True,
        "source_state_does_not_override_recorded_order_or_fill": True,
        "source_records_do_not_authorize_execution": True,
        "observer": observer, "copier": copier, "record_count": len(records), "signals": records}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"available": False, "category": "invalid_arguments"}))
        raise SystemExit(2)


def main() -> int:
    parser = _Parser(prog="python -m alpha_discord.diagnostics", description=__doc__)
    parser.add_argument("--observer-runtime", required=True, type=Path)
    parser.add_argument("--copier-runtime", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1..100")
    args = parser.parse_args()
    try:
        report = diagnose(args.observer_runtime, args.copier_runtime, args.limit)
    except Exception as exc:
        report = _failure(exc)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0 if report.get("diagnostic_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
