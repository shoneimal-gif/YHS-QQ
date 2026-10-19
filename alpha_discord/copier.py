"""Explicitly authorized AlphaAgent channel copy execution, Alpaca PAPER only.

Public entry point: run_copier(config, broker, observer_runtime=..., runtime=...,
coordinator_root=..., notifier=...). The deployment runner supplies an already
configured AlpacaPaperClient and a generic durable notifier with
enqueue(event_id, title, fields). This module never reads credentials.

The legacy policy accepts OPEN 1x as one BUY_TO_OPEN. The separately explicit
fixed_one_contract policy copies any supported OPEN as exactly one contract;
the original source's size marker is preserved and never becomes order qty.
The separately authorized budget_range_contracts policy chooses whole contracts
nearest its target debit within its explicit range and currently available caps.
Its flat fee reserve is charged once per entry intent, never per contract.
Source CLOSE authorizes SELL_TO_CLOSE of our uniquely matched OCC+tier holding.
Neither the source's prices/PnL nor screenshots are execution/fill evidence.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from alpaca_client import AlpacaError, SubmissionNotSent, SubmissionUnknown, normalize_quote
from shared_account import SharedAccountCoordinator, SharedAccountError, SharedAccountBlocked
from .common import atomic_json
from .personal_source import PersonalSource, personal_binding


TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "failed", "voided"}
PENDING = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
           "held", "pending_cancel", "pending_replace", "stopped", "suspended", "calculated", "done_for_day"}
_OCC = re.compile(r"([A-Z]{1,6})([0-9]{6})([CP])([0-9]{8})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_FIELDS = ("source_guild_id", "source_channel_id", "target_guild_id", "target_channel_id")
_FOLLOW_MODE = "native_announcement_follow"
_PERSONAL_MODE = "direct_personal_account"
_PERMANENT = {"ledger_integrity", "authentication", "permission", "tls_verification", "client_closed"}
_DEFER = {"broker_warmup", "shared_account_waiting", "market_closed", "close_market_closed",
          "owned_fill_not_yet_confirmed", "owned_order_not_yet_confirmed", "option_quantity_not_reconciled",
          "broker_clock_unconfirmed", "broker_state_unconfirmed", "observer_not_live_healthy",
          "observer_status_unavailable", "follower_provenance_unconfirmed", "broker_quote_unconfirmed",
          "owned_exit_on_same_contract_pending", "source_update_verification_pending"}


class CopyError(ValueError):
    def __init__(self, category: str, *, permanent: bool = False):
        super().__init__(category)
        self.category, self.permanent = category, permanent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise CopyError("timestamp_unconfirmed") from None
    if stamp.tzinfo is None:
        raise CopyError("timestamp_unconfirmed")
    return stamp.astimezone(timezone.utc)


def _number(value: Any, *, positive: bool = False, whole: bool = False) -> float:
    if isinstance(value, bool):
        raise CopyError("number_unconfirmed")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CopyError("number_unconfirmed") from None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0) or (whole and number != int(number)):
        raise CopyError("number_unconfirmed")
    return number


def _requested_qty(payload: Mapping[str, Any]) -> int:
    value = payload.get("qty")
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,3}", value) is None or int(value) > 5000:
        raise CopyError("immutable_order_quantity_invalid", permanent=True)
    return int(value)


def _age(stamp: Any, maximum: float) -> bool:
    return -5 <= (_now() - _time(stamp)).total_seconds() <= maximum


def _snowflake(value: str) -> datetime:
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value) >= 2**64:
        raise CopyError("source_identifier_unconfirmed")
    return datetime.fromtimestamp(((int(value) >> 22) + 1420070400000) / 1000, tz=timezone.utc)


def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate explicit deployment authorization; defaults never enable writes."""
    cfg = dict(config)
    for key in _FIELDS:
        if not isinstance(cfg.get(key), str) or not _ID.fullmatch(cfg[key]) or int(cfg[key]) >= 2**64:
            raise CopyError("source_binding_required", permanent=True)
    cfg.setdefault("source_mode", _FOLLOW_MODE)
    cfg.setdefault("quantity_policy", "legacy_open_1x")
    if cfg["source_mode"] not in (_FOLLOW_MODE, _PERSONAL_MODE):
        raise CopyError("unsupported_source_mode", permanent=True)
    if cfg["quantity_policy"] not in ("legacy_open_1x", "fixed_one_contract", "budget_range_contracts"):
        raise CopyError("unsupported_quantity_policy", permanent=True)
    if cfg["source_mode"] == _PERSONAL_MODE:
        personal_binding(cfg, error_class=CopyError)
        if cfg["quantity_policy"] not in ("fixed_one_contract", "budget_range_contracts"):
            raise CopyError("explicit_direct_quantity_policy_required", permanent=True)
        for key, lower, upper in (("max_entry_price_premium_pct", 0, 3), ("max_entry_signal_age_seconds", 1, 30)):
            value = cfg.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise CopyError("explicit_entry_price_and_delay_policy_required", permanent=True)
    allowed = cfg.get("allowed_account_sha256")
    if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) or not _HASH.fullmatch(item) for item in allowed):
        raise CopyError("account_allowlist_required", permanent=True)
    if (cfg.get("paper_only") is not True
            or (cfg["quantity_policy"] == "legacy_open_1x" and cfg.get("open_1x_is_one_buy_to_open") is not True)
            or cfg.get("close_only_owned_exact_occ_and_tier") is not True
            or cfg.get("legacy_executor_disabled") is not True):
        raise CopyError("explicit_copy_authorization_required", permanent=True)
    if cfg["quantity_policy"] == "budget_range_contracts":
        if cfg["source_mode"] != _PERSONAL_MODE:
            raise CopyError("budget_quantity_requires_direct_source", permanent=True)
        for key, expected in (("min_trade_budget_usd", 3000), ("target_trade_budget_usd", 4000), ("single_trade_cap_usd", 5000), ("fee_reserve_usd", 2)):
            value = cfg.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value != expected:
                raise CopyError("explicit_budget_range_authorization_required", permanent=True)
    elif cfg.get("single_trade_cap_usd", 4000) != 4000:
        raise CopyError("copy_caps_not_authorized", permanent=True)
    if cfg.get("options_cap_usd", 10000) != 10000:
        raise CopyError("copy_caps_not_authorized", permanent=True)
    cfg.setdefault("execute_paper", False)
    if type(cfg["execute_paper"]) is not bool:
        raise CopyError("invalid_execution_mode", permanent=True)
    for key, default, upper in (("max_signal_age_seconds", 120, 120), ("max_quote_age_seconds", 30, 30),
                               ("observer_status_max_age_seconds", 20, 30), ("entry_timeout_seconds", 120, 120),
                               ("poll_seconds", 5, 30)):
        cfg.setdefault(key, default)
        if type(cfg[key]) not in (int, float) or not 1 <= cfg[key] <= upper:
            raise CopyError("invalid_timing_policy", permanent=True)
    cfg.setdefault("quote_feed", "indicative")
    if cfg["quote_feed"] not in ("indicative", "opra"):
        raise CopyError("invalid_quote_feed", permanent=True)
    cfg.setdefault("fee_reserve_usd", 2.0)
    if not 0 <= _number(cfg["fee_reserve_usd"]) <= 20:
        raise CopyError("invalid_fee_reserve", permanent=True)
    approved = cfg.get("approved_follower_webhook_id")
    if approved is not None and (not isinstance(approved, str) or not _ID.fullmatch(approved)):
        raise CopyError("invalid_follower_binding", permanent=True)
    if "armed_at_utc" not in cfg or (_time(cfg["armed_at_utc"]) - _now()).total_seconds() > 5:
        raise CopyError("explicit_arming_timestamp_required", permanent=True)
    return cfg


class _Lease:
    def __init__(self, path: Path):
        if path.is_symlink():
            raise CopyError("linked_lock_refused", permanent=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise CopyError("options_executor_already_running", permanent=True) from None

    def close(self) -> None:
        self.handle.close()


class _Ledger:
    def __init__(self, path: Path, account_id: str, binding: str, activated_at: str, quantity_policy: str = "legacy_open_1x"):
        if path.is_symlink():
            raise CopyError("linked_ledger_refused", permanent=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS engine_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        row = self.db.execute("SELECT payload FROM engine_state WHERE id=1").fetchone()
        if row:
            try:
                self.state = json.loads(row[0])
                if not isinstance(self.state, dict):
                    raise ValueError
                if self.state.get("config_binding") != binding:
                    # A source or quantity-policy change is never an implicit
                    # ledger reset, re-arm, or migration, even if presently flat.
                    raise CopyError("source_or_policy_binding_changed_requires_review", permanent=True)
                if (self.state["schema_version"] != 1 or self.state["account_id"] != account_id
                        or any(not isinstance(self.state[key], dict) for key in ("decisions", "lots", "orders", "notices"))
                        or not isinstance(self.state["halts"], list)):
                    raise ValueError
                _time(self.state["activated_at"])
                for cid, order in self.state["orders"].items():
                    if order["payload"]["client_order_id"] != cid or order["lot_id"] not in self.state["lots"]:
                        raise ValueError
                    requested = _requested_qty(order["payload"])
                    if ((quantity_policy != "budget_range_contracts" and requested != 1)
                            or order["payload"]["side"] not in ("buy", "sell")):
                        raise ValueError
                    filled = _number(order["filled_qty"], whole=True)
                    if filled > requested or order["phase"] not in {"INTENT", "UNKNOWN", "PENDING", "TERMINAL", "NOT_SENT"}:
                        raise ValueError
                    if quantity_policy == "budget_range_contracts":
                        if (type(order.get("requested_qty")) is not int or order["requested_qty"] != requested
                                or type(order["filled_qty"]) is not int
                                or (order["phase"] == "NOT_SENT" and filled)
                                or (order["phase"] == "TERMINAL" and order.get("broker_status") not in TERMINAL)
                                or order["payload"].get("position_intent") != ("buy_to_open" if order["payload"]["side"] == "buy" else "sell_to_close")
                                or order["payload"].get("type") != ("limit" if order["payload"]["side"] == "buy" else "market")
                                or order["payload"].get("time_in_force") != "day"
                                or order["payload"].get("extended_hours") is not False):
                            raise ValueError
                        _number(order["avg_price"], positive=bool(filled))
                    if type(order.get("write_attempted")) is not bool or type(order.get("cancel_attempted")) is not bool:
                        raise ValueError
                self.state.setdefault("pending_sources", {})
                if not isinstance(self.state["pending_sources"], dict):
                    raise ValueError
                self.state.setdefault("source_telemetry", {})
                if not isinstance(self.state["source_telemetry"], dict) or any(not isinstance(item, dict) for item in self.state["source_telemetry"].values()):
                    raise ValueError
                for lot_id, lot in self.state["lots"].items():
                    entry = self.state["orders"][lot["entry_client_id"]]
                    if (lot.get("lot_id") != lot_id or entry["lot_id"] != lot_id or entry["payload"]["side"] != "buy"
                            or entry["payload"]["symbol"] != lot["occ"]
                            or not isinstance(lot["exit_client_ids"], list) or len(set(lot["exit_client_ids"])) != len(lot["exit_client_ids"])):
                        raise ValueError
                    exits = [self.state["orders"][cid] for cid in lot["exit_client_ids"]]
                    if any(item["lot_id"] != lot_id or item["payload"]["side"] != "sell" or item["payload"]["symbol"] != lot["occ"]
                           or _requested_qty(item["payload"]) > _requested_qty(entry["payload"]) for item in exits):
                        raise ValueError
                    if sum(item["filled_qty"] for item in exits) > entry["filled_qty"] or sum(item["phase"] not in {"NOT_SENT", "TERMINAL"} for item in exits) > 1:
                        raise ValueError
                    if quantity_policy == "budget_range_contracts":
                        if (type(lot.get("planned_qty")) is not int or lot["planned_qty"] != _requested_qty(entry["payload"])
                                or lot.get("quantity_policy") != quantity_policy or type(lot.get("exit_review_required", False)) is not bool):
                            raise ValueError
                        limit = Decimal(entry["payload"]["limit_price"])
                        debit = limit * 100 * lot["planned_qty"] + 2
                        if (not limit.is_finite() or limit < Decimal("0.01") or limit != limit.quantize(Decimal("0.01"))
                                or not 3000 <= debit <= 5000 or Decimal(lot["planned_debit_usd"]) != debit):
                            raise ValueError
                        if any(type(item.get("owned_qty_at_intent")) is not int
                               or item["owned_qty_at_intent"] != _requested_qty(item["payload"])
                               or item["owned_qty_at_intent"] > entry["filled_qty"] for item in exits):
                            raise ValueError
                if quantity_policy == "budget_range_contracts":
                    referenced = [lot["entry_client_id"] for lot in self.state["lots"].values()]
                    referenced += [cid for lot in self.state["lots"].values() for cid in lot["exit_client_ids"]]
                    if len(referenced) != len(set(referenced)) or set(referenced) != set(self.state["orders"]):
                        raise ValueError
            except CopyError:
                self.db.close()
                raise
            except (KeyError, ValueError, TypeError, InvalidOperation):
                self.db.close()
                raise CopyError("ledger_integrity", permanent=True) from None
        else:
            self.state = {"schema_version": 1, "account_id": account_id, "config_binding": binding,
                          "activated_at": _time(activated_at).isoformat(), "follower_webhook_id": None,
                          "decisions": {}, "lots": {}, "orders": {}, "notices": {}, "halts": []}
            self.state["pending_sources"] = {}
            self.state["source_telemetry"] = {}
            self.save()
        if os.name != "nt":
            os.chmod(path, 0o600)

    def save(self) -> None:
        try:
            encoded = json.dumps(self.state, ensure_ascii=False, allow_nan=False, sort_keys=True)
            with self.db:
                self.db.execute("INSERT INTO engine_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (encoded,))
        except (sqlite3.Error, ValueError, TypeError):
            raise CopyError("ledger_integrity", permanent=True) from None

    def notice(self, identity: str, title: str, fields: dict[str, Any]) -> None:
        event_id = "alpha:" + hashlib.sha256(identity.encode()).hexdigest()
        self.state["notices"].setdefault(event_id, {"title": title, "fields": fields, "queued": False})

    def flush(self, notifier: Any) -> None:
        if notifier is None:
            return
        for event_id, notice in self.state["notices"].items():
            if notice["queued"]:
                continue
            try:
                accepted = notifier.enqueue(event_id, notice["title"], notice["fields"])
            except Exception:
                accepted = False
            if accepted is True:
                notice["queued"] = True
                self.save()


class CopyEngine:
    def __init__(self, config: Mapping[str, Any], broker: Any, *, observer_runtime: Path,
                 runtime: Path, coordinator_root: Path, notifier: Any = None,
                 stop_event: threading.Event | None = None):
        self.config = validate_config(config)
        self.broker, self.notifier = broker, notifier
        if str(getattr(broker, "base_url", "")) != "https://paper-api.alpaca.markets" or getattr(broker, "paper_only", False) is not True:
            raise CopyError("paper_endpoint_required", permanent=True)
        self.runtime, self.observer_runtime = Path(runtime).resolve(), Path(observer_runtime).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.stop = stop_event or threading.Event()
        self.leases, self.ledger, self.observer_db = [], None, None
        self.personal_source = None
        self.coordinator_root, self.coordinator = Path(coordinator_root).resolve(), None
        self.account_hash, self.healthy_polls = None, 0
        self.bound, self.legacy_leased = False, False
        self.last_error, self.last_snapshot = None, None
        self.forced_order_refresh: set[str] = set()
        self.leases.append(_Lease(self.runtime / "copier.lock"))

    def _bind(self) -> None:
        account = self.broker.get_account()
        account_id = str(account.get("id", ""))
        digest = hashlib.sha256(account_id.encode()).hexdigest()
        if not account_id or digest not in self.config["allowed_account_sha256"]:
            raise CopyError("account_binding_mismatch", permanent=True)
        legacy = Path(self.config.get("legacy_options_state_path") or (
            Path(__file__).resolve().parents[1] / "runtime" / "alpaca_accounts" / digest / "alpaca_paper_state.json")).resolve()
        if legacy.parent.name != digest or not legacy.is_file() or legacy.is_symlink():
            raise CopyError("legacy_state_unconfirmed", permanent=True)
        if not self.legacy_leased:
            self.leases.append(_Lease(legacy.parent / "runner.lock"))
            self.leases.append(_Lease(legacy.with_suffix(legacy.suffix + ".lock")))
            self.legacy_leased = True
        try:
            previous = json.loads(legacy.read_text(encoding="utf-8"))
            if previous.get("account_id") not in (None, account_id) or not isinstance(previous.get("positions"), list):
                raise ValueError
            if any(item.get("status") not in ("CLOSED", "CANCELLED") for item in previous["positions"]):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CopyError("legacy_option_exposure_not_zero", permanent=True) from None
        binding_data = {key: self.config[key] for key in _FIELDS}
        binding_data.update({"account_sha256": digest, "policy": "OPEN1x_BTO1_CLOSE_exact_owned_v1",
                             "single_trade_cap_usd": 4000, "options_cap_usd": 10000})
        if (self.config["source_mode"] != _FOLLOW_MODE or self.config["quantity_policy"] != "legacy_open_1x"):
            # The original Follow/default-policy digest remains byte-for-byte
            # compatible. A new source or policy gets an explicit new binding.
            binding_data.update(source_mode=self.config["source_mode"], quantity_policy=self.config["quantity_policy"],
                                policy="FIXED_ONE_BTO_CLOSE_exact_owned_v1")
            if self.config["source_mode"] == _PERSONAL_MODE:
                binding_data["source_binding"] = personal_binding(self.config, error_class=CopyError)
                binding_data.update(policy="FIXED_ONE_BTO_ENTRY_PRICE_DELAY_GUARDS_v2",
                    max_entry_price_premium_pct=self.config["max_entry_price_premium_pct"],
                    max_entry_signal_age_seconds=self.config["max_entry_signal_age_seconds"])
                if self.config["quantity_policy"] == "budget_range_contracts":
                    binding_data.update(policy="BUDGET_RANGE_BTO_PARTIAL_EXACT_OWNED_CLOSE_v3",
                        min_trade_budget_usd=self.config["min_trade_budget_usd"],
                        target_trade_budget_usd=self.config["target_trade_budget_usd"],
                        single_trade_cap_usd=self.config["single_trade_cap_usd"],
                        fee_reserve_usd=2, fee_reserve_basis="once_per_entry_intent_not_per_contract")
        binding = hashlib.sha256(json.dumps(binding_data, sort_keys=True).encode()).hexdigest()
        if self.ledger is None:
            self.ledger = _Ledger(self.runtime / "copier.sqlite3", account_id, binding, self.config["armed_at_utc"], self.config["quantity_policy"])
        self.account_hash = digest
        if self.coordinator is None:
            self.coordinator = SharedAccountCoordinator(account_id, self.coordinator_root, "options")
        limits = self.coordinator.snapshot()
        if limits["strategy_caps_usd"] != {"stocks": 40000, "options": 10000} or limits["total_cap_usd"] != 50000:
            raise CopyError("shared_copy_limits_not_bound", permanent=True)
        self.broker.attach_shared_coordinator(self.coordinator, fee_reserve_usd=self.config["fee_reserve_usd"])
        if self.config["source_mode"] == _PERSONAL_MODE:
            if self.personal_source is None:
                self.personal_source = PersonalSource(self.config, self.observer_runtime, error_class=CopyError)
            self.personal_source.validate_binding()
            self.bound = True
            return
        observer_path = self.observer_runtime / "observer.sqlite3"
        if observer_path.is_symlink() or not observer_path.is_file():
            raise CopyError("observer_database_unavailable")
        if self.observer_db is None:
            self.observer_db = sqlite3.connect(observer_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        self.observer_db.execute("PRAGMA query_only=ON")
        expected = hashlib.sha256(json.dumps({key: self.config[key] for key in _FIELDS}, sort_keys=True).encode()).hexdigest()
        row = self.observer_db.execute("SELECT value FROM metadata WHERE key='config_binding'").fetchone()
        if row is None or row[0] != expected:
            raise CopyError("observer_binding_mismatch", permanent=True)
        self.bound = True

    def _feed(self) -> dict[str, Any]:
        if self.config["source_mode"] == _PERSONAL_MODE:
            if self.personal_source is None:
                raise CopyError("observer_database_unavailable")
            return self.personal_source.feed()
        try:
            status = json.loads((self.observer_runtime / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CopyError("observer_status_unavailable") from None
        if (status.get("mode") != "discord_announcement_observer"
                or any(status.get(key) != self.config[key] for key in _FIELDS)
                or any(status.get(key) is not True for key in ("connected", "bootstrap_complete", "healthy", "target_verified", "source_provenance_verified"))
                or status.get("error_category") is not None
                or not _age(status.get("updated_utc"), self.config["observer_status_max_age_seconds"])):
            raise CopyError("observer_not_live_healthy")
        follower = status.get("follower_webhook_id")
        row = self.observer_db.execute("SELECT value FROM metadata WHERE key='follower_webhook_id'").fetchone()
        if not isinstance(follower, str) or not _ID.fullmatch(follower) or row is None or row[0] != follower:
            raise CopyError("follower_provenance_unconfirmed")
        approved = self.config.get("approved_follower_webhook_id")
        latched = self.ledger.state["follower_webhook_id"]
        if (approved is not None and follower != approved) or (latched is not None and follower != latched):
            raise CopyError("follower_binding_changed", permanent=True)
        if latched is None:
            self.ledger.state["follower_webhook_id"] = follower
            self.ledger.save()
        return status

    def _source(self, key: str, revision: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.config["source_mode"] == _PERSONAL_MODE:
            if self.personal_source is None or self.ledger is None:
                raise CopyError("observer_database_unavailable")
            return self.personal_source.source(key, revision, self.ledger.state["activated_at"])
        feed = self._feed()
        row = self.observer_db.execute("""SELECT m.original_message_id,m.first_received_utc,m.latest_revision,m.deleted,
            r.first_received_utc,r.event_kind,r.classification,r.snapshot_json FROM messages m
            JOIN revisions r ON r.source_key=m.source_key AND r.revision=m.latest_revision WHERE m.source_key=?""", (key,)).fetchone()
        if row is None or row[3] or (revision is not None and row[2] != revision):
            raise CopyError("source_deleted_or_revised")
        revisions = self.observer_db.execute("SELECT COUNT(*) FROM revisions WHERE source_key=?", (key,)).fetchone()[0]
        if revisions != 1:
            raise CopyError("source_ever_revised")
        if row[5] != "MESSAGE_CREATE" or row[6] != "fresh_observation":
            raise CopyError("non_live_or_replayed_source")
        if len(row[7].encode()) > 131072:
            raise CopyError("source_snapshot_invalid")
        snapshot = json.loads(row[7])
        parsed, timing = snapshot.get("parser"), snapshot.get("timing")
        if (not isinstance(parsed, dict) or not isinstance(timing, dict) or not parsed.get("parsed")
                or parsed.get("metadata_valid") is not True or parsed.get("parser_version") != "alpha_embed_v2" or parsed.get("errors")):
            raise CopyError("unsupported_source_syntax")
        source = parsed.get("source", {})
        reference = source.get("message_reference", {})
        if (source.get("message_key") != key or source.get("message_key_scope") != "crosspost_reference"
                or source.get("source_authenticated") is not True
                or source.get("guild_id") != self.config["target_guild_id"]
                or source.get("channel_id") != self.config["target_channel_id"]
                or source.get("webhook_id") != feed["follower_webhook_id"]
                or reference.get("guild_id") != self.config["source_guild_id"]
                or reference.get("channel_id") != self.config["source_channel_id"]
                or reference.get("message_id") != row[0] or reference.get("type") != 0
                or type(source.get("flags")) is not int or not source["flags"] & 2 or source["flags"] & 8):
            raise CopyError("source_provenance_mismatch", permanent=True)
        deleted = self.observer_db.execute("SELECT 1 FROM tombstones WHERE source_key=? OR delivery_id=? LIMIT 1", (key, source.get("message_id"))).fetchone()
        delivery = self.observer_db.execute("SELECT deleted,source_key FROM deliveries WHERE delivery_id=?", (source.get("message_id"),)).fetchone()
        if deleted or delivery is None or delivery[0] or delivery[1] != key:
            raise CopyError("source_withdrawn")
        received = _time(timing.get("received_utc"))
        if (received < _time(self.ledger.state["activated_at"]) or received < _time(feed.get("connected_since_utc"))
                or row[1] != row[4] or _time(row[4]) != received):
            raise CopyError("source_before_live_activation")
        if not -5 <= (_now() - _snowflake(row[0])).total_seconds() <= self.config["max_signal_age_seconds"]:
            raise CopyError("source_expired")
        if not _age(received, self.config["max_signal_age_seconds"]):
            raise CopyError("source_delivery_expired")
        return parsed, {"revision": row[2], "original_message_id": row[0], "received_utc": row[4]}

    def _remaining(self, lot: dict[str, Any]) -> int:
        entry = self.ledger.state["orders"][lot["entry_client_id"]]
        bought = int(_number(entry["filled_qty"], whole=True))
        sold = sum(int(_number(self.ledger.state["orders"][cid]["filled_qty"], whole=True)) for cid in lot["exit_client_ids"])
        if bought > _requested_qty(entry["payload"]) or sold > bought:
            raise CopyError("managed_quantity_mismatch", permanent=True)
        return bought - sold

    def _exit_review_pending(self) -> bool:
        return any(lot.get("exit_review_required") and self._remaining(lot) > 0 for lot in self.ledger.state["lots"].values())

    def _local_used(self) -> float:
        """Keep exact copied-lot cost caps even if the shared registry uses FIFO.

        Different tiers can own the same fungible OCC contract. Their local
        cost anchors remain attached to the actually confirmed entry CID, so
        closing a cheaper tier cannot release an expensive tier's risk budget.
        """
        total = 0.0
        for lot in self.ledger.state["lots"].values():
            entry = self.ledger.state["orders"][lot["entry_client_id"]]
            remaining = self._remaining(lot)
            total += entry["avg_price"] * remaining * 100
            pending_buy = entry["phase"] not in {"NOT_SENT", "TERMINAL"}
            if pending_buy:
                total += (_requested_qty(entry["payload"]) - entry["filled_qty"]) * float(entry["payload"]["limit_price"]) * 100
            if remaining or pending_buy:
                total += self.config["fee_reserve_usd"]
        return total

    def _budget_quantity(self, limit: Decimal, snapshot: dict[str, Any]) -> tuple[int, Decimal]:
        shared = self.coordinator.snapshot()
        fee = Decimal(str(self.config["fee_reserve_usd"]))
        unit = limit * 100
        if not limit.is_finite() or limit < Decimal("0.01"):
            raise CopyError("budget_unit_price_invalid")
        upper = min(Decimal(str(self.config["single_trade_cap_usd"])),
                    Decimal("10000") - Decimal(str(self._local_used())),
                    Decimal("10000") - Decimal(str(_number(shared["risk_used_usd"]["options"]))),
                    Decimal("50000") - Decimal(str(_number(shared["total_risk_used_usd"]))),
                    Decimal(str(_number(snapshot["account"].get("cash")))),
                    Decimal(str(_number(snapshot["account"].get("options_buying_power")))))
        lower = Decimal(str(self.config["min_trade_budget_usd"]))
        target = Decimal(str(self.config["target_trade_budget_usd"]))
        minimum = max(1, int(((lower - fee) / unit).to_integral_value(rounding=ROUND_CEILING)))
        maximum = int(((upper - fee) / unit).to_integral_value(rounding=ROUND_FLOOR))
        if maximum < minimum:
            raise CopyError("no_whole_contract_quantity_in_authorized_budget_range")
        ideal = (target - fee) / unit
        candidates = {minimum, maximum, int(ideal.to_integral_value(rounding=ROUND_FLOOR)), int(ideal.to_integral_value(rounding=ROUND_CEILING))}
        choices = [(quantity, unit * quantity + fee) for quantity in candidates if minimum <= quantity <= maximum]
        quantity, debit = min(choices, key=lambda item: (abs(item[1] - target), item[0]))
        if not 1 <= quantity <= 5000 or not lower <= debit <= upper or not self.coordinator.entry_allowed(float(debit)):
            raise CopyError("copy_trade_or_shared_budget_exceeded")
        return quantity, debit

    def _telemetry(self, key: str) -> dict[str, Any]:
        record = self.ledger.state["source_telemetry"].setdefault(key, {
            "source_message_id": key.rsplit(":", 1)[-1],
            "source_created_utc": _snowflake(key.rsplit(":", 1)[-1]).isoformat(),
            "source_received_utc": None, "decision_utc": None, "submit_attempt_utc": None,
            "post_ack_received_utc": None, "broker_first_observed_utc": None,
            "broker_fill_utc": None, "fill_observed_utc": None,
            "timing_basis": "discord_snowflake_host_utc_and_confirmed_broker_timestamps",
            "independent_clock_offsets_verified": False,
        })
        return record

    def _measure_delays(self, record: dict[str, Any]) -> None:
        pairs = {"source_to_receive_seconds": ("source_created_utc", "source_received_utc"),
                 "source_to_decision_seconds": ("source_created_utc", "decision_utc"),
                 "source_to_submit_seconds": ("source_created_utc", "submit_attempt_utc"),
                 "receive_to_submit_seconds": ("source_received_utc", "submit_attempt_utc"),
                 "source_to_post_ack_seconds": ("source_created_utc", "post_ack_received_utc"),
                 "source_to_fill_seconds": ("source_created_utc", "broker_fill_utc"),
                 "source_to_first_fill_observed_seconds": ("source_created_utc", "fill_observed_utc"),
                 "receive_to_fill_seconds": ("source_received_utc", "broker_fill_utc")}
        for name, (start, end) in pairs.items():
            record[name] = round((_time(record[end]) - _time(record[start])).total_seconds(), 6) if record.get(start) and record.get(end) else None

    def _record_source(self, key: str, parsed: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        record = self._telemetry(key)
        received = _time(meta["received_utc"]).isoformat()
        if record.get("source_received_utc") not in (None, received):
            raise CopyError("source_receipt_binding_changed", permanent=True)
        record.update(source_received_utc=received, action=parsed.get("action_kind"),
                      occ=(parsed.get("contract") or {}).get("occ_symbol"),
                      source_reference_price_decimal=parsed.get("entry_reference_price_decimal") if parsed.get("action_kind") == "open" else parsed.get("exit_reference_price_decimal"))
        self._measure_delays(record)
        return record

    def _entry_age(self, key: str, *, now: datetime | None = None) -> None:
        if self.config["source_mode"] != _PERSONAL_MODE:
            return
        age = ((now or _now()) - _snowflake(key.rsplit(":", 1)[-1])).total_seconds()
        if not -5 <= age <= self.config["max_entry_signal_age_seconds"]:
            raise CopyError("entry_signal_too_old")

    def _entry_cap(self, parsed: dict[str, Any]) -> tuple[Decimal, Decimal]:
        value = parsed.get("entry_reference_price_decimal")
        if not isinstance(value, str) or len(value) > 32:
            raise CopyError("entry_reference_price_missing_or_invalid")
        try:
            reference = Decimal(value)
        except InvalidOperation:
            raise CopyError("entry_reference_price_missing_or_invalid") from None
        if not reference.is_finite() or reference <= 0:
            raise CopyError("entry_reference_price_missing_or_invalid")
        cap = (reference * (Decimal(1) + Decimal(str(self.config["max_entry_price_premium_pct"])) / 100)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        return reference, cap

    def _telemetry_fields(self, key: str) -> dict[str, str]:
        record = self._telemetry(key)
        self._measure_delays(record)
        def show(value: Any) -> str:
            return "未观测" if value is None else str(value)
        price = "源价 " + show(record.get("source_reference_price_decimal"))
        if record.get("action") == "open":
            price += " | ask " + show(record.get("observed_ask_decimal")) + " | 限价 " + show(record.get("limit_price_decimal"))
            price += " | 价差% " + show(record.get("limit_vs_reference_pct")) + " | 上限 " + show(record.get("entry_price_cap_decimal"))
        delays = "源→接收 " + show(record.get("source_to_receive_seconds"))
        delays += " | 源→决定 " + show(record.get("source_to_decision_seconds"))
        delays += " | 接收→提交 " + show(record.get("receive_to_submit_seconds"))
        delays += " | 源→提交 " + show(record.get("source_to_submit_seconds"))
        delays += " | 源→首笔观测 " + show(record.get("source_to_first_fill_observed_seconds"))
        delays += " | 源→全成 " + show(record.get("source_to_fill_seconds"))
        return {"价格核对": price, "延迟秒（跨系统UTC观测）": delays}

    def _apply_order(self, intent: dict[str, Any], raw: dict[str, Any], *, post_ack_received_utc: str | None = None) -> None:
        payload = intent["payload"]
        requested = _requested_qty(payload)
        if (not isinstance(raw, dict) or not raw.get("id") or raw.get("client_order_id") != payload["client_order_id"]
                or raw.get("symbol") != payload["symbol"] or raw.get("asset_class") != "us_option"
                or raw.get("side") != payload["side"] or raw.get("type") != payload["type"]
                or raw.get("time_in_force") != "day" or raw.get("order_class") not in (None, "", "simple")
                or _number(raw.get("qty"), positive=True, whole=True) != requested
                or raw.get("replaces") or raw.get("replaced_by")
                or (intent["order_id"] is not None and raw["id"] != intent["order_id"])):
            raise CopyError("order_ownership_mismatch", permanent=True)
        status = str(raw.get("status", ""))
        qty = int(_number(raw.get("filled_qty"), whole=True))
        if qty > requested or qty < intent["filled_qty"] or status not in TERMINAL | PENDING:
            raise CopyError("order_state_unconfirmed")
        if ((status == "filled") != (qty == requested)) or (status in {"rejected", "failed", "voided"} and qty):
            raise CopyError("order_state_unconfirmed")
        if ((status == "partially_filled" and not 0 < qty < requested)
                or (intent["phase"] == "TERMINAL" and status not in TERMINAL)):
            raise CopyError("order_state_unconfirmed")
        price = _number(raw.get("filled_avg_price"), positive=True) if qty else 0.0
        if qty and (not _age(raw.get("filled_at") or raw.get("updated_at"), 3650 * 86400)):
            raise CopyError("fill_timestamp_unconfirmed")
        if payload["type"] == "limit" and Decimal(str(raw.get("limit_price"))) != Decimal(payload["limit_price"]):
            raise CopyError("order_limit_changed", permanent=True)
        self.coordinator.register_order(raw)
        old_id, old_status, old_qty, old_price = intent["order_id"], intent["broker_status"], intent["filled_qty"], intent["avg_price"]
        intent.update(order_id=str(raw["id"]), broker_status=status, filled_qty=qty, avg_price=price,
                      phase="TERMINAL" if status in TERMINAL else "PENDING", observed_at=_now().isoformat())
        if status in TERMINAL and not intent.get("terminal_first_observed_utc"):
            intent["terminal_first_observed_utc"] = intent["observed_at"]
        record = self._telemetry(intent["source_key"])
        if record.get("broker_first_observed_utc") is None:
            record["broker_first_observed_utc"] = intent["observed_at"]
        if post_ack_received_utc is not None:
            record["post_ack_received_utc"] = post_ack_received_utc
            intent["post_ack_received_utc"] = post_ack_received_utc
        if qty:
            if record.get("fill_observed_utc") is None:
                record["fill_observed_utc"] = intent["observed_at"]
            if qty != old_qty or price != old_price:
                record["latest_fill_observed_utc"] = intent["observed_at"]
            record["confirmed_fill_price_decimal"] = str(raw["filled_avg_price"])
            if status == "filled" and qty == requested and raw.get("filled_at") is not None:
                record["broker_fill_utc"] = _time(raw["filled_at"]).isoformat()
                record["fill_timestamp_source"] = "broker_filled_at_complete_order"
            else:
                record["broker_fill_utc"] = None
                record["fill_timestamp_source"] = "partial_fill_or_complete_fill_time_unobserved"
        self._measure_delays(record)
        if qty:
            intent["fill_telemetry"] = dict(record)
        intent["telemetry"] = dict(record)
        lot = self.ledger.state["lots"][intent["lot_id"]]
        remaining = self._remaining(lot)
        if (self.config["quantity_policy"] == "budget_range_contracts" and payload["side"] == "sell"
                and status in TERMINAL and remaining > 0):
            lot["exit_review_required"] = True
            self.ledger.notice("exit-incomplete-review:" + payload["client_order_id"], "AlphaAgent 平仓委托已终止但仍有余仓｜需人工核对", {
                "合约": lot["occ"], "层级": lot["tier"] or "无层级", "原委托张数": requested,
                "本委托累计成交": qty, "本批剩余张数": remaining, "券商终态": status,
                "说明": "不自动提交另一张平仓单；其他已知仓位继续对账，余仓不能视为已平"})
        elif remaining == 0:
            lot["exit_review_required"] = False
        if (self.config["quantity_policy"] == "budget_range_contracts" and payload["side"] == "buy"
                and qty > old_qty and remaining > 0 and (lot.get("close_source") or {}).get("attempted")
                and not any(self.ledger.state["orders"][cid]["phase"] not in {"NOT_SENT", "TERMINAL"} for cid in lot["exit_client_ids"])):
            lot["exit_review_required"] = True
            self.ledger.notice("late-entry-fill-review:" + payload["client_order_id"] + f":{qty}", "AlphaAgent 取消入场后新增成交已确认｜余仓需人工核对", {
                "合约": lot["occ"], "新增确认成交": qty-old_qty, "本批剩余张数": remaining,
                "说明": "原CLOSE已处理；不会用过期源消息自动补发平仓单"})
        fields = {"来源": "AlphaAgent 原频道已验证机器人消息" if self.config["source_mode"] == _PERSONAL_MODE else "AlphaAgent 已验证公告关注消息", "合约": payload["symbol"], "层级": lot["tier"] or "无层级",
                  "方向": "买入开仓" if payload["side"] == "buy" else "卖出平仓",
                  "委托张数": requested, "累计确认成交张数": qty, "本次新增成交张数": qty - old_qty,
                  "本系统剩余张数": remaining, "券商状态": status, "订单尾号": str(raw["id"])[-8:],
                  "响应途径": "POST回执" if post_ack_received_utc is not None else "GET对账",
                  **self._telemetry_fields(intent["source_key"])}
        if old_id is None:
            self.ledger.notice("accepted:" + payload["client_order_id"], "AlphaAgent 模拟盘订单已获券商响应", fields)
        pnl_fields = {}
        pnl_changed = False
        if payload["side"] == "sell" and qty:
            entry_price = self.ledger.state["orders"][lot["entry_client_id"]]["avg_price"]
            cumulative_pnl = (price - entry_price) * qty * 100
            previous_pnl = intent.get("realized_pnl_usd", 0.0)
            intent["realized_pnl_usd"] = cumulative_pnl
            pnl_changed = cumulative_pnl != previous_pnl
            pnl_fields = {"本订单累计PnL_USD未含费": cumulative_pnl,
                          "本次PnL增量USD未含费": cumulative_pnl - previous_pnl}
        if qty != old_qty or price != old_price:
            fields = {**fields, "确认累计成交均价": price, **pnl_fields}
            title = "AlphaAgent 模拟盘已确认全部成交" if status == "filled" and qty == requested else "AlphaAgent 模拟盘已确认部分成交"
            self.ledger.notice("fill:" + payload["client_order_id"] + f":{qty}:{price}", title, fields)
        elif pnl_changed:
            self.ledger.notice("pnl-correction:" + payload["client_order_id"] + f":{cumulative_pnl}", "AlphaAgent 成本更新｜已实现PnL修正（无新增卖出成交）", {
                "合约": lot["occ"], "层级": lot["tier"] or "无层级", "累计卖出张数": qty,
                "本次新增卖出成交张数": 0, "确认入场累计均价": entry_price,
                "确认卖出累计均价": price, "本系统剩余张数": remaining, **pnl_fields,
                "说明": "按同一入场订单最新确认均价修正成本，不代表新增卖出成交"})
        if old_status != status and status in {"rejected", "failed", "voided", "canceled", "cancelled", "expired"}:
            self.ledger.notice("terminal:" + payload["client_order_id"] + ":" + status, "AlphaAgent 模拟盘订单终止", fields)
        self.ledger.save()

    def _snapshot(self) -> dict[str, Any]:
        with self.coordinator.lock():
            account = self.broker.get_account()
            if hashlib.sha256(str(account.get("id", "")).encode()).hexdigest() != self.account_hash:
                raise CopyError("account_binding_mismatch", permanent=True)
            if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
                raise CopyError("account_not_tradeable", permanent=True)
            clock = self.broker.get_clock()
            if type(clock.get("is_open")) is not bool or not _age(clock.get("timestamp"), 30):
                raise CopyError("broker_clock_unconfirmed")
            if clock["is_open"] and _time(clock.get("next_close")) <= _now():
                raise CopyError("broker_clock_unconfirmed")
            for intent in self.ledger.state["orders"].values():
                if intent["phase"] == "NOT_SENT":
                    continue
                if intent["phase"] == "TERMINAL":
                    lot = self.ledger.state["lots"][intent["lot_id"]]
                    recent_partial_entry = (intent["payload"]["side"] == "buy"
                        and intent["filled_qty"] < _requested_qty(intent["payload"])
                        and intent.get("terminal_first_observed_utc")
                        and (_now()-_time(intent["terminal_first_observed_utc"])).total_seconds() <= 120)
                    if not (self.config["quantity_policy"] == "budget_range_contracts"
                            and (self._remaining(lot) > 0 or recent_partial_entry
                                 or intent["payload"]["client_order_id"] in self.forced_order_refresh)):
                        continue
                raw = self.broker.get_order_by_client_id(intent["payload"]["client_order_id"])
                if raw is None:
                    self.coordinator.register_intent(intent["payload"]["client_order_id"], {
                        **intent["payload"], "asset_class": "us_option",
                        "fee_reserve_usd": self.config["fee_reserve_usd"] if intent["payload"]["side"] == "buy" else 0,
                    }, imported=True)
                    self.coordinator.mark_unknown(intent["payload"]["client_order_id"])
                    intent["phase"] = "UNKNOWN"
                else:
                    self._apply_order(intent, raw)
            positions = self.broker.get_positions()
            orders = self.broker.get_orders("open")
            if not isinstance(positions, list) or not isinstance(orders, list):
                raise CopyError("broker_state_unconfirmed")
            for raw in orders:
                known = self.ledger.state["orders"].get(raw.get("client_order_id"))
                if known is not None and known["phase"] not in {"NOT_SENT", "TERMINAL"}:
                    self._apply_order(known, raw)
            expected: dict[str, int] = {}
            for lot in self.ledger.state["lots"].values():
                remaining = self._remaining(lot)
                if remaining:
                    expected[lot["occ"]] = expected.get(lot["occ"], 0) + remaining
            actual: dict[str, int] = {}
            pending_own_buys = {item["payload"]["symbol"] for item in self.ledger.state["orders"].values()
                                if item["payload"]["side"] == "buy" and item["phase"] in {"INTENT", "UNKNOWN"}}
            if self.config["quantity_policy"] == "budget_range_contracts":
                pending_own_buys.update(item["payload"]["symbol"] for item in self.ledger.state["orders"].values()
                    if item["payload"]["side"] == "buy" and item["phase"] != "NOT_SENT"
                    and item["filled_qty"] < _requested_qty(item["payload"]))
            for position in positions:
                symbol = position.get("symbol")
                if position.get("asset_class") == "us_option" or (isinstance(symbol, str) and _OCC.fullmatch(symbol)):
                    qty = int(_number(position.get("qty"), whole=True))
                    if position.get("asset_class") != "us_option" or position.get("side") != "long":
                        raise CopyError("foreign_option_position", permanent=True)
                    if qty and symbol not in expected:
                        if symbol in pending_own_buys:
                            self.forced_order_refresh.update(item["payload"]["client_order_id"] for item in self.ledger.state["orders"].values() if item["payload"]["symbol"] == symbol)
                            raise CopyError("owned_fill_not_yet_confirmed")
                        raise CopyError("foreign_option_position", permanent=True)
                    if qty:
                        actual[symbol] = qty
            if actual != expected:
                if self.config["quantity_policy"] == "budget_range_contracts":
                    symbols = set(actual) | set(expected)
                    self.forced_order_refresh.update(item["payload"]["client_order_id"] for item in self.ledger.state["orders"].values() if item["payload"]["symbol"] in symbols)
                raise CopyError("option_quantity_not_reconciled")
            own_ids = {item["order_id"] for item in self.ledger.state["orders"].values() if item["order_id"]}
            for order in orders:
                if order.get("id") in own_ids:
                    if not self.coordinator.owns_order(order):
                        raise CopyError("order_ownership_mismatch", permanent=True)
                elif not self.coordinator.owns_order(order, other_strategy="stocks"):
                    known = self.ledger.state["orders"].get(order.get("client_order_id"))
                    if known is not None and known["phase"] in {"INTENT", "UNKNOWN"}:
                        raise CopyError("owned_order_not_yet_confirmed")
                    raise CopyError("foreign_open_order", permanent=True)
            own_positions, pending = [], []
            for lot in self.ledger.state["lots"].values():
                remaining = self._remaining(lot)
                entry = self.ledger.state["orders"][lot["entry_client_id"]]
                if remaining:
                    own_positions.append({"symbol": lot["occ"], "qty": remaining, "cost_usd": entry["avg_price"] * remaining * 100,
                                          "entry_client_order_id": lot["entry_client_id"], "entry_order_id": entry["order_id"]})
            for intent in self.ledger.state["orders"].values():
                if intent["phase"] not in ("NOT_SENT", "TERMINAL"):
                    pending.append({"client_order_id": intent["payload"]["client_order_id"],
                                    "status": "unknown" if intent["phase"] in ("INTENT", "UNKNOWN") else intent["broker_status"],
                                    "reserved_usd": float(intent["payload"].get("limit_price", 0)) * 100 * (_requested_qty(intent["payload"])-intent["filled_qty"]) if intent["payload"]["side"] == "buy" else 0})
            self.coordinator.update_state(own_positions, pending)
            self.ledger.save()
            if not _age(clock.get("timestamp"), 30):
                raise CopyError("broker_clock_unconfirmed")
            self.forced_order_refresh.clear()
            return {"account": account, "clock": clock, "positions": actual,
                    "shared_entries_allowed": self.coordinator.entry_allowed()}

    def _quote(self, occ: str) -> dict[str, Any]:
        raw = self.broker.get_option_quote(occ, self.config["quote_feed"])
        quote = normalize_quote(raw, occ, self.config["quote_feed"])
        if not _age(quote["quoted_at"], self.config["max_quote_age_seconds"]) or quote["bid"] <= 0:
            raise CopyError("broker_quote_unconfirmed")
        return quote

    def _contract(self, parsed: dict[str, Any]) -> str:
        target = parsed.get("contract", {})
        occ = target.get("occ_symbol")
        match = _OCC.fullmatch(occ) if isinstance(occ, str) else None
        if match is None:
            raise CopyError("exact_contract_unconfirmed")
        actual = self.broker.get_contract(occ)
        if (not isinstance(actual, dict) or actual.get("tradable") is not True or str(actual.get("status", "")).lower() != "active"
                or actual.get("symbol") != occ or actual.get("underlying_symbol") != target.get("underlying")
                or actual.get("type") != ("call" if target.get("right") == "C" else "put")
                or actual.get("expiration_date") != target.get("expiration")
                or Decimal(str(actual.get("strike_price"))) != Decimal(str(target.get("strike_decimal")))
                or _number(actual.get("size"), positive=True) != 100):
            raise CopyError("contract_metadata_mismatch")
        if date.fromisoformat(target["expiration"]) < _now().astimezone(ZoneInfo("America/New_York")).date():
            raise CopyError("contract_expired")
        return occ

    def _claim(self, key: str, reason: str, *, lot_id: str | None = None) -> None:
        self.ledger.state["pending_sources"].pop(key, None)
        record = self._telemetry(key)
        record.update(decision=reason, decision_utc=_now().isoformat())
        self._measure_delays(record)
        self.ledger.state["decisions"][key] = {"decision": reason, "lot_id": lot_id, "observed_at": record["decision_utc"]}
        if not reason.startswith("intent_"):
            fields = {"原因分类": reason, "源消息编号": key.rsplit(":", 1)[-1],
                      "说明": "源收益不是本系统收益；截图、历史、修改和重复消息不用于新下单",
                      **self._telemetry_fields(key)}
            if lot_id is not None:
                lot = self.ledger.state["lots"][lot_id]
                fields.update({"本系统对应合约": lot["occ"], "本系统仍持张数": self._remaining(lot),
                               "持仓说明": "未确认实际卖出；仍持有时需要继续有效源CLOSE或人工核对"})
            self.ledger.notice("decision:" + key, "AlphaAgent 消息未提交新订单", fields)
        self.ledger.save()

    def _intent(self, lot: dict[str, Any], side: str, source_key: str, revision: str,
                limit_price: str | None = None, *, quantity: int = 1) -> dict[str, Any]:
        if type(quantity) is not int or not 1 <= quantity <= 5000:
            raise CopyError("immutable_order_quantity_invalid", permanent=True)
        cid = "alpha-opt-" + uuid4().hex
        payload = {"symbol": lot["occ"], "qty": str(quantity), "side": side, "type": "limit" if side == "buy" else "market",
                   "time_in_force": "day", "extended_hours": False, "client_order_id": cid,
                   "position_intent": "buy_to_open" if side == "buy" else "sell_to_close"}
        if side == "buy":
            payload["limit_price"] = limit_price
        intent = {"lot_id": lot["lot_id"], "payload": payload, "source_key": source_key, "revision": revision,
                  "phase": "INTENT", "broker_status": None, "order_id": None, "filled_qty": 0,
                  "avg_price": 0.0, "created_at": _now().isoformat(), "cancel_attempted": False}
        intent["requested_qty"] = quantity
        if side == "sell":
            intent["owned_qty_at_intent"] = self._remaining(lot)
            if intent["owned_qty_at_intent"] != quantity:
                raise CopyError("close_quantity_unconfirmed")
        intent["write_attempted"] = False
        self.ledger.state["orders"][cid] = intent
        return intent

    def _send(self, intent: dict[str, Any], snapshot: dict[str, Any], quote: dict[str, Any]) -> None:
        side = intent["payload"]["side"]
        def verify_local() -> dict[str, Any]:
            current_source, current_meta = self._source(intent["source_key"], intent["revision"])
            record = self._record_source(intent["source_key"], current_source, current_meta)
            if self.stop.is_set() or not snapshot["clock"]["is_open"] or not _age(snapshot["clock"]["timestamp"], 30):
                raise CopyError("stopped_or_market_clock_expired")
            if _now() >= _time(snapshot["clock"].get("next_close")):
                raise CopyError("stopped_or_market_clock_expired")
            if not _age(quote["quoted_at"], self.config["max_quote_age_seconds"]):
                raise CopyError("broker_quote_expired")
            if self.ledger.state["halts"]:
                raise CopyError("operator_review_required")
            if side == "buy" and (self.ledger.state["halts"] or not self.coordinator.entry_allowed()):
                raise CopyError("entry_gate_closed")
            if side == "buy" and self._local_used() > 10000 + 1e-8:
                raise CopyError("exact_copied_lot_budget_exceeded")
            lot = self.ledger.state["lots"][intent["lot_id"]]
            if side == "buy" and self._exit_review_pending():
                raise CopyError("owned_exit_review_required")
            if side == "sell":
                if self._remaining(lot) != _requested_qty(intent["payload"]):
                    raise CopyError("close_quantity_unconfirmed")
                if (self.ledger.state["orders"][lot["entry_client_id"]]["phase"] not in {"NOT_SENT", "TERMINAL"}
                        or lot.get("exit_review_required")):
                    raise CopyError("owned_exit_review_required")
                for other in self.ledger.state["lots"].values():
                    if other["occ"] == lot["occ"] and any(cid != intent["payload"]["client_order_id"]
                            and self.ledger.state["orders"][cid]["phase"] not in {"NOT_SENT", "TERMINAL"} for cid in other["exit_client_ids"]):
                        raise CopyError("owned_exit_on_same_contract_pending")
            if side == "buy" and self.config["source_mode"] == _PERSONAL_MODE:
                self._entry_age(intent["source_key"])
                reference, cap = self._entry_cap(current_source)
                expected = intent.get("entry_price_policy")
                if (not isinstance(expected, dict) or expected.get("source_reference_price_decimal") != format(reference, "f")
                        or expected.get("entry_price_cap_decimal") != format(cap, "f")
                        or expected.get("max_entry_price_premium_pct") != self.config["max_entry_price_premium_pct"]
                        or expected.get("max_entry_signal_age_seconds") != self.config["max_entry_signal_age_seconds"]):
                    raise CopyError("entry_price_policy_binding_changed", permanent=True)
                limit = Decimal(intent["payload"]["limit_price"])
                ask = Decimal(str(quote["ask"])).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
                if limit <= 0 or limit != ask or limit > cap:
                    raise CopyError("entry_price_above_reference_cap")
                if self.config["quantity_policy"] == "budget_range_contracts":
                    debit = limit * 100 * _requested_qty(intent["payload"]) + Decimal("2")
                    if (intent.get("requested_qty") != _requested_qty(intent["payload"])
                            or lot.get("planned_qty") != _requested_qty(intent["payload"])
                            or Decimal(lot["planned_debit_usd"]) != debit
                            or not Decimal("3000") <= debit <= Decimal("5000")):
                        raise CopyError("immutable_budget_quantity_changed", permanent=True)
            return record

        def before_send() -> None:
            try:
                record = verify_local()
                record["intent_precommit_utc"] = _now().isoformat()
                intent["intent_precommit_utc"] = record["intent_precommit_utc"]
                intent["telemetry"] = dict(record)
                self.ledger.save()
                # Persistence may be slow. Recheck every original local gate,
                # including CLOSE/source withdrawal, before returning to HTTP.
                # There is no persistent write after this successful recheck.
                record = verify_local()
                attempted = _now().isoformat()
                record["submit_attempt_utc"] = attempted
                record["submit_attempt_is_order_acceptance"] = False
                self._measure_delays(record)
                intent["submit_attempt_utc"] = attempted
                intent["telemetry"] = dict(record)
                self.ledger.notice("submit-attempt:" + intent["payload"]["client_order_id"], "AlphaAgent 模拟盘提交尝试｜尚未确认成交", {
                    "合约": intent["payload"]["symbol"], "方向": "买入开仓" if side == "buy" else "卖出平仓",
                    "张数": _requested_qty(intent["payload"]), "委托类型": intent["payload"]["type"], "源消息编号": record["source_message_id"],
                    "提交门检查UTC时间": attempted, **self._telemetry_fields(intent["source_key"]),
                    "说明": "这是发送前检查通过的提交尝试；券商回执和成交另行确认"})
            except CopyError as exc:
                intent["final_gate_rejection_category"] = exc.category
                if exc.permanent and exc.category not in self.ledger.state["halts"]:
                    self.ledger.state["halts"].append(exc.category)
                record = self._telemetry(intent["source_key"])
                record.update(final_gate_rejection_category=exc.category, final_gate_rejected_utc=_now().isoformat())
                self.ledger.save()
                raise
        intent["write_attempted"] = True
        self.ledger.save()  # durable attempt marker precedes the one client call
        try:
            raw = self.broker.submit_order(intent["payload"], before_send=before_send)
            self._apply_order(intent, raw, post_ack_received_utc=_now().isoformat())
        except SubmissionNotSent:
            intent["phase"] = "NOT_SENT"
            reason = intent.get("final_gate_rejection_category", "final_gate_rejected")
            self.ledger.notice("not-sent:" + intent["payload"]["client_order_id"], "AlphaAgent 模拟盘提交前已停止", {
                "合约": intent["payload"]["symbol"], "原因分类": reason,
                **self._telemetry_fields(intent["source_key"]), "说明": "确认没有发送；本源消息不重提"})
        except SubmissionUnknown as exc:
            intent["phase"] = "UNKNOWN"
            if not exc.recoverable or exc.category in _PERMANENT:
                if exc.category not in self.ledger.state["halts"]:
                    self.ledger.state["halts"].append(exc.category)
            self.ledger.notice("unknown:" + intent["payload"]["client_order_id"], "AlphaAgent 模拟盘订单待确认", {
                "合约": intent["payload"]["symbol"], "原因分类": exc.category,
                "需要人工核对": not exc.recoverable,
                **self._telemetry_fields(intent["source_key"]),
                "说明": "保留预算，仅按持久client_order_id查询，404也不会重提"})
        except AlpacaError as exc:
            intent["phase"] = "NOT_SENT"
            self.ledger.notice("rejected:" + intent["payload"]["client_order_id"], "AlphaAgent 模拟盘订单被拒绝", {
                "合约": intent["payload"]["symbol"], "HTTP状态": exc.status or 0, "原因分类": exc.category})
            if not exc.recoverable:
                self.ledger.state["halts"].append("broker_write_rejected_requires_review")
        except Exception:
            intent["phase"] = "UNKNOWN"
            self.coordinator.mark_unknown(intent["payload"]["client_order_id"])
            raise
        self.ledger.save()

    def _cancel_entry(self, intent: dict[str, Any]) -> None:
        if self.ledger.state["halts"]:
            return
        if intent["cancel_attempted"] or not intent["order_id"] or intent["phase"] != "PENDING":
            return
        intent["cancel_attempted"] = True
        self.ledger.save()
        try:
            self.broker.cancel_order(intent["order_id"], before_send=lambda: None if not self.stop.is_set() else (_ for _ in ()).throw(CopyError("stopped")))
        except SubmissionNotSent:
            pass
        except SubmissionUnknown:
            pass
        except AlpacaError:
            pass
        self.ledger.notice("cancel:" + intent["payload"]["client_order_id"], "AlphaAgent 入场委托取消状态待核对", {
            "合约": intent["payload"]["symbol"], "说明": "取消仅尝试一次，GET终态前不释放预算或假设未成交"})
        self.ledger.save()

    def _open(self, key: str, parsed: dict[str, Any], meta: dict[str, Any]) -> None:
        record = self._record_source(key, parsed, meta)
        self._entry_age(key)
        price_policy = None
        if self.config["source_mode"] == _PERSONAL_MODE:
            reference, cap = self._entry_cap(parsed)
            price_policy = {"source_reference_price_decimal": format(reference, "f"),
                            "entry_price_cap_decimal": format(cap, "f"),
                            "max_entry_price_premium_pct": self.config["max_entry_price_premium_pct"],
                            "max_entry_signal_age_seconds": self.config["max_entry_signal_age_seconds"]}
            record.update(price_policy)
        if self.config["quantity_policy"] == "legacy_open_1x":
            if not ((parsed.get("quantity_mode") == "unknown_size_multiplier" and parsed.get("size_multiplier") == 1)
                    or (parsed.get("quantity_mode") == "explicit_contracts" and parsed.get("requested_contracts") == 1)):
                raise CopyError("only_explicit_authorized_one_contract")
        elif self.config["quantity_policy"] not in ("fixed_one_contract", "budget_range_contracts"):
            raise CopyError("unsupported_quantity_policy", permanent=True)
        if parsed.get("explicit_side") not in ("UNKNOWN", "BUY_TO_OPEN"):
            raise CopyError("unsupported_open_direction")
        occ = self._contract(parsed)
        for lot in self.ledger.state["lots"].values():
            entry = self.ledger.state["orders"][lot["entry_client_id"]]
            if (lot["occ"] == occ and lot["tier"] == parsed.get("signal_tier")
                    and (self._remaining(lot) or entry["phase"] not in ("NOT_SENT", "TERMINAL"))):
                raise CopyError("same_occ_and_tier_already_owned_or_pending")
        with self.coordinator.lock():
            snapshot = self._snapshot()
            if self.healthy_polls < 2:
                raise CopyError("broker_warmup")
            if self.ledger.state["halts"]:
                raise CopyError("operator_review_required")
            if self._exit_review_pending():
                raise CopyError("owned_exit_review_required")
            if not snapshot["clock"]["is_open"]:
                raise CopyError("market_closed")
            if self.coordinator.snapshot()["pending_write_count"]:
                raise CopyError("shared_account_waiting")
            if not snapshot["shared_entries_allowed"]:
                raise CopyError("shared_account_waiting")
            quote = self._quote(occ)
            limit = Decimal(str(quote["ask"])).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            record.update(observed_ask_decimal=str(quote["ask"]), limit_price_decimal=format(limit, "f"),
                          quote_timestamp_utc=quote["quoted_at"])
            if price_policy is not None:
                record["limit_vs_reference_pct"] = float(((limit / reference - 1) * 100).quantize(Decimal("0.000001")))
                self._entry_age(key)
                if limit > cap:
                    raise CopyError("entry_price_above_reference_cap")
            quantity = 1
            if self.config["quantity_policy"] == "budget_range_contracts":
                quantity, planned_debit = self._budget_quantity(limit, snapshot)
            else:
                planned_debit = limit * 100 + Decimal(str(self.config["fee_reserve_usd"]))
            debit = float(planned_debit)
            if debit > self.config.get("single_trade_cap_usd", 4000) or self._local_used() + debit > 10000 + 1e-8 or not self.coordinator.entry_allowed(debit):
                raise CopyError("copy_trade_or_shared_budget_exceeded")
            if (_number(snapshot["account"].get("cash")) < debit
                    or _number(snapshot["account"].get("options_buying_power")) < debit
                    or int(snapshot["account"].get("options_trading_level", snapshot["account"].get("options_approved_level", 0))) < 2):
                raise CopyError("cash_or_option_permission_insufficient")
            if not self.config["execute_paper"]:
                self._claim(key, "preview_only")
                return
            if self.config["source_mode"] == _PERSONAL_MODE:
                # Broker/quote reads may have overlapped a Discord update.
                # Defer before claiming a source or creating an immutable CID.
                self._source(key, meta["revision"])
                self._entry_age(key)
            lot_id = uuid4().hex
            lot = {"lot_id": lot_id, "occ": occ, "tier": parsed.get("signal_tier"), "source_key": key,
                   "source_revision": meta["revision"], "opening_message_id": meta["original_message_id"],
                   "quantity_policy": self.config["quantity_policy"],
                   "source_quantity_mode": parsed.get("quantity_mode"),
                   "source_size_multiplier": parsed.get("size_multiplier"),
                   "source_requested_contracts": parsed.get("requested_contracts"),
                   "entry_client_id": None, "exit_client_ids": [], "close_source": None,
                   "planned_qty": quantity, "planned_debit_usd": format(planned_debit, "f"),
                   "exit_review_required": False}
            self.ledger.state["lots"][lot_id] = lot
            intent = self._intent(lot, "buy", key, meta["revision"], format(limit, "f"), quantity=quantity)
            if price_policy is not None:
                intent["entry_price_policy"] = dict(price_policy)
            intent["telemetry"] = dict(record)
            lot["entry_client_id"] = intent["payload"]["client_order_id"]
            record.update(planned_qty=quantity, planned_debit_usd=format(planned_debit, "f"), fee_reserve_basis="once_per_entry_intent_not_per_contract")
            self._claim(key, "intent_buy_to_open_budget_range" if self.config["quantity_policy"] == "budget_range_contracts" else "intent_buy_to_open_one", lot_id=lot_id)
            self._send(intent, snapshot, quote)

    def _close(self, key: str, parsed: dict[str, Any], meta: dict[str, Any]) -> None:
        self._record_source(key, parsed, meta)
        target = parsed.get("contract", {})
        occ, tier = target.get("occ_symbol"), parsed.get("signal_tier")
        if parsed.get("explicit_side") not in ("UNKNOWN", "SELL_TO_CLOSE") or not isinstance(occ, str) or not _OCC.fullmatch(occ):
            raise CopyError("unsupported_close_direction_or_contract")
        matches = [lot for lot in self.ledger.state["lots"].values() if lot["occ"] == occ and lot["tier"] == tier
                   and (self._remaining(lot) or self.ledger.state["orders"][lot["entry_client_id"]]["phase"] not in ("NOT_SENT", "TERMINAL"))]
        if len(matches) != 1:
            raise CopyError("close_has_no_unique_owned_occ_and_tier")
        lot = matches[0]
        if lot.get("exit_review_required"):
            raise CopyError("owned_exit_review_required")
        if parsed.get("opening_message_id") is not None and parsed["opening_message_id"] != lot["opening_message_id"]:
            raise CopyError("close_opening_reference_mismatch")
        if any(self.ledger.state["orders"][cid]["phase"] not in ("NOT_SENT", "TERMINAL") for cid in lot["exit_client_ids"]):
            raise CopyError("owned_exit_already_pending")
        if self.config["source_mode"] == _PERSONAL_MODE:
            self._source(key, meta["revision"])
        lot["close_source"] = {"key": key, "revision": meta["revision"]}
        self._claim(key, "intent_close_owned", lot_id=lot["lot_id"])
        self._advance_close(lot)

    def _advance_close(self, lot: dict[str, Any]) -> None:
        close = lot["close_source"]
        if close is None or close.get("attempted") or lot.get("exit_review_required"):
            return
        if self.ledger.state["halts"]:
            raise CopyError("operator_review_required")
        try:
            self._source(close["key"], close["revision"])
        except CopyError as exc:
            if exc.category in _DEFER or exc.permanent:
                raise
            close["attempted"] = True
            self._claim(close["key"], "close_" + exc.category, lot_id=lot["lot_id"])
            return
        with self.coordinator.lock():
            snapshot = self._snapshot()
            entry = self.ledger.state["orders"][lot["entry_client_id"]]
            if entry["phase"] not in ("NOT_SENT", "TERMINAL"):
                self._cancel_entry(entry)
                return
            remaining = self._remaining(lot)
            if remaining <= 0:
                close["attempted"] = True
                self._claim(close["key"], "close_no_owned_remaining_fill", lot_id=lot["lot_id"])
                return
            for other in self.ledger.state["lots"].values():
                if other["lot_id"] != lot["lot_id"] and other["occ"] == lot["occ"]:
                    if any(self.ledger.state["orders"][cid]["phase"] not in {"NOT_SENT", "TERMINAL"}
                           for cid in other["exit_client_ids"]):
                        raise CopyError("owned_exit_on_same_contract_pending")
            if not snapshot["clock"]["is_open"]:
                raise CopyError("close_market_closed")
            quote = self._quote(lot["occ"])
            if not self.config["execute_paper"]:
                close["attempted"] = True
                self._claim(close["key"], "preview_only", lot_id=lot["lot_id"])
                return
            if self.config["source_mode"] == _PERSONAL_MODE:
                self._source(close["key"], close["revision"])
            intent = self._intent(lot, "sell", close["key"], close["revision"], quantity=remaining)
            lot["exit_client_ids"].append(intent["payload"]["client_order_id"])
            close["attempted"] = True
            self.ledger.save()
            self._send(intent, snapshot, quote)

    def _maintenance(self) -> None:
        if self.ledger.state["halts"]:
            return
        for lot in list(self.ledger.state["lots"].values()):
            entry = self.ledger.state["orders"][lot["entry_client_id"]]
            if self.personal_source is not None:
                try:
                    withdrawn = self.personal_source.invalidated(lot["source_key"], lot["source_revision"])
                except CopyError as exc:
                    if exc.category != "source_update_verification_pending":
                        raise
                    # Pending metadata verification is not a confirmed edit.
                    # Existing timeout/CLOSE cancellation rules still apply.
                    withdrawn = False
            else:
                row = self.observer_db.execute("SELECT deleted,latest_revision FROM messages WHERE source_key=?", (lot["source_key"],)).fetchone()
                count = self.observer_db.execute("SELECT COUNT(*) FROM revisions WHERE source_key=?", (lot["source_key"],)).fetchone()[0]
                withdrawn = row is None or bool(row[0]) or row[1] != lot["source_revision"] or count != 1
            if entry["phase"] == "PENDING":
                expired = (_now() - _time(entry["created_at"])).total_seconds() >= self.config["entry_timeout_seconds"]
                if withdrawn or expired or lot["close_source"]:
                    self._cancel_entry(entry)
            if withdrawn and self._remaining(lot) and not lot.get("withdrawal_notified"):
                lot["withdrawal_notified"] = True
                self.ledger.notice("withdrawn-after-fill:" + lot["lot_id"], "AlphaAgent 源消息撤回或修改，仍有本系统持仓", {
                    "合约": lot["occ"], "本系统剩余张数": self._remaining(lot),
                    "说明": "删除或修改不是已成交平仓；等待有效同合约同层级CLOSE，必要时人工核对"})
                self.ledger.save()
            if lot["close_source"]:
                try:
                    self._advance_close(lot)
                except CopyError as exc:
                    if exc.category != "source_update_verification_pending":
                        raise

    def _consume(self) -> None:
        self._feed()
        # A 15-minute *diagnostic* candidate window lets expired, genuinely
        # live CREATEs receive one skip notice. _source and before_send still
        # enforce the immutable 120-second execution freshness limit.
        cutoff = max(_time(self.ledger.state["activated_at"]), _now() - timedelta(minutes=15))
        if self.personal_source is not None:
            rows = self.personal_source.candidates(cutoff)
        else:
            rows = self.observer_db.execute("""SELECT m.source_key FROM messages m
                JOIN revisions r ON r.source_key=m.source_key AND r.revision=m.latest_revision
                WHERE m.deleted=0 AND m.first_received_utc>=? AND r.event_kind='MESSAGE_CREATE'
                  AND r.classification='fresh_observation'
                ORDER BY m.first_received_utc DESC,m.source_key DESC LIMIT 100""", (cutoff.isoformat(),)).fetchall()
            rows.reverse()
        added = False
        ready_sources: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for (key,) in rows:
            if key in self.ledger.state["decisions"]:
                continue
            if key not in self.ledger.state["pending_sources"]:
                self.ledger.state["pending_sources"][key] = _now().isoformat()
                added = True
            try:
                parsed, meta = self._source(key)
                self._record_source(key, parsed, meta)
                # This source-age check needs no additional broker request and
                # also expires an OPEN while broker/account safety gates wait.
                if parsed.get("action_kind") == "open":
                    self._entry_age(key)
                ready_sources[key] = parsed, meta
            except CopyError as exc:
                if exc.category in _DEFER:
                    continue
                self._claim(key, exc.category)
                if exc.permanent:
                    raise
        if added or ready_sources:
            self.ledger.save()
        if (self.healthy_polls < 2 or self.ledger.state["halts"]
                or not self.last_snapshot or not self.last_snapshot["clock"]["is_open"]
                or self.coordinator.snapshot()["pending_write_count"]):
            return
        for (key,) in rows:
            if self.stop.is_set() or key in self.ledger.state["decisions"]:
                continue
            try:
                if key not in ready_sources:
                    continue
                parsed, meta = ready_sources[key]
                if parsed.get("action_kind") == "open":
                    self._open(key, parsed, meta)
                elif parsed.get("action_kind") == "close":
                    self._close(key, parsed, meta)
                else:
                    raise CopyError("unsupported_source_action")
            except CopyError as exc:
                if exc.category in _DEFER:
                    continue
                self._claim(key, exc.category)
                if exc.permanent:
                    raise

    def status(self) -> dict[str, Any]:
        state = self.ledger.state if self.ledger else None
        live_feed = False
        if state and (self.observer_db is not None or self.personal_source is not None):
            try:
                self._feed()
                live_feed = True
            except (CopyError, OSError, sqlite3.Error, ValueError, TypeError):
                pass
        clock_live = False
        if self.last_snapshot:
            try:
                clock_live = self.last_snapshot["clock"]["is_open"] is True and _age(self.last_snapshot["clock"]["timestamp"], 30)
            except (CopyError, KeyError, TypeError):
                pass
        return {"version": 1, "mode": "alphaagent_alpaca_paper_copy" if self.config["execute_paper"] else "alphaagent_copy_preview",
                "paper_only": True, "updated_utc": _now().isoformat(), "account_sha256": self.account_hash,
                "healthy_broker_polls": self.healthy_polls, "last_error_category": self.last_error,
                "entries_enabled": bool(state and not state["halts"] and not self._exit_review_pending() and self.healthy_polls >= 2 and self.config["execute_paper"]
                                         and self.last_error is None and live_feed and clock_live
                                         and self.last_snapshot and self.last_snapshot["shared_entries_allowed"]),
                "observer_live_healthy": live_feed,
                "option_single_trade_cap_usd": self.config.get("single_trade_cap_usd", 4000), "options_cap_usd": 10000,
                "exact_copied_lot_cost_and_reserve_usd": self._local_used() if state else 0,
                "shared_account": self.coordinator.snapshot() if self.coordinator else None,
                "source_mode": self.config["source_mode"],
                "source_author_authenticated": bool(live_feed and self.config["source_mode"] == _PERSONAL_MODE),
                "provenance_basis": ("authenticated_original_bot_message_via_personal_account" if self.config["source_mode"] == _PERSONAL_MODE
                                     else "verified_native_announcement_follower_channel"),
                "source_binding_sha256": self.personal_source.binding_sha256 if self.personal_source is not None else None,
                "quantity_policy": self.config["quantity_policy"],
                "copy_policy": ("OPEN_budget_range_whole_contracts_CLOSE_only_owned_exact_OCC_and_tier" if self.config["quantity_policy"] == "budget_range_contracts"
                                else "EVERY_OPEN_BUY_TO_OPEN_one_CLOSE_only_owned_exact_OCC_and_tier" if self.config["quantity_policy"] == "fixed_one_contract"
                                else "OPEN1x_BUY_TO_OPEN_one_CLOSE_only_owned_exact_OCC_and_tier"),
                "entry_order_budget_policy": {"minimum_debit_usd": self.config["min_trade_budget_usd"],
                    "target_debit_usd": self.config["target_trade_budget_usd"], "maximum_debit_usd": self.config["single_trade_cap_usd"],
                    "debit_basis": "limit_price_times_100_times_whole_contracts_plus_flat_fee_reserve",
                    "flat_fee_reserve_per_entry_usd": 2, "source_size_controls_quantity": False,
                    "selection": "nearest_target_within_available_caps_ties_choose_fewer_contracts",
                    "no_feasible_whole_quantity": "skip", "partial_or_cheaper_fills_auto_top_up": False,
                    "terminal_partial_exit": "manual_review_no_automatic_residual_order"}
                    if self.config["quantity_policy"] == "budget_range_contracts" else None,
                "entry_limit_policy": {"source": "Dennis_original_AlphaAgent_only",
                    "max_entry_price_premium_pct": self.config["max_entry_price_premium_pct"],
                    "max_entry_signal_age_seconds": self.config["max_entry_signal_age_seconds"],
                    "reference_cap_rounding": "floor_to_cent", "ask_limit_rounding": "ceil_to_cent",
                    "over_cap_or_late_open": "skip_without_chasing",
                    "close_uses_entry_price_cap": False,
                    "close_max_source_age_seconds": self.config["max_signal_age_seconds"]}
                    if self.config["source_mode"] == _PERSONAL_MODE else None,
                "signal_latency_telemetry_count": len(state.get("source_telemetry", {})) if state else 0,
                "recent_signal_latency_telemetry": sorted(state.get("source_telemetry", {}).values(),
                    key=lambda item: item.get("decision_utc") or item.get("source_received_utc") or item.get("source_created_utc", ""))[-5:] if state else [],
                "screenshots_executable": False, "source_performance_is_ours": False,
                "quote_feed": self.config["quote_feed"], "quote_is_estimate": self.config["quote_feed"] == "indicative",
                "broker_protective_orders": False, "source_close_required": True,
                "unknown_orders": sum(item["phase"] in ("INTENT", "UNKNOWN") for item in state["orders"].values()) if state else 0,
                "open_copied_positions": [{"occ": lot["occ"], "tier": lot["tier"], "remaining_qty": self._remaining(lot),
                                           "planned_entry_qty": _requested_qty(state["orders"][lot["entry_client_id"]]["payload"]),
                                           "confirmed_entry_qty": state["orders"][lot["entry_client_id"]]["filled_qty"],
                                           "exit_review_required": lot.get("exit_review_required", False)}
                                          for lot in state["lots"].values() if self._remaining(lot)] if state else [],
                "exit_review_lots": [{"occ": lot["occ"], "tier": lot["tier"], "remaining_qty": self._remaining(lot)}
                                     for lot in state["lots"].values() if lot.get("exit_review_required") and self._remaining(lot)] if state else [],
                "decisions_processed": len(state["decisions"]) if state else 0,
                "halts": list(state["halts"]) if state else [],
                "pending_notifications": sum(not item["queued"] for item in state["notices"].values()) if state else 0,
                "notifications_are_outbox_acknowledgements_not_delivery_proof": True}

    def step(self) -> dict[str, Any]:
        try:
            if not self.bound:
                self._bind()
            for key in list(self.ledger.state["pending_sources"]):
                record = self.ledger.state["source_telemetry"].get(key, {})
                if (self.config["source_mode"] == _PERSONAL_MODE and record.get("action") == "open"
                        and (_now() - _snowflake(key.rsplit(":", 1)[-1])).total_seconds() > self.config["max_entry_signal_age_seconds"]):
                    self._claim(key, "entry_signal_too_old")
                elif (_now() - _snowflake(key.rsplit(":", 1)[-1])).total_seconds() > self.config["max_signal_age_seconds"]:
                    self._claim(key, "source_expired_while_waiting_for_safety_gate")
            self.last_snapshot = self._snapshot()
            self.healthy_polls = min(2, self.healthy_polls + 1)
            self.last_error = None
            self._maintenance()
            self._consume()
        except (CopyError, AlpacaError, SharedAccountError, sqlite3.Error, OSError, ValueError, TypeError, KeyError, InvalidOperation, AttributeError) as exc:
            category = exc.category if isinstance(exc, (CopyError, AlpacaError, SharedAccountError)) else "state_or_response_unconfirmed"
            permanent = (getattr(exc, "permanent", False) or category in _PERMANENT
                         or (isinstance(exc, SharedAccountError) and not exc.recoverable))
            self.healthy_polls, self.last_error = 0, category
            if self.coordinator is not None:
                self.coordinator.pause_state("ledger_integrity" if permanent else "state_unconfirmed")
            if self.ledger is not None:
                if permanent and category not in self.ledger.state["halts"]:
                    self.ledger.state["halts"].append(category)
                self.ledger.notice("failure:" + category, "AlphaAgent 模拟盘复制已暂停", {"原因分类": category,
                                   "需要人工核对": bool(permanent), "说明": "已知持仓未被假定平仓；未知写请求不重提"})
                self.ledger.save()
            if permanent and self.ledger is None:
                raise
        if self.ledger:
            self.ledger.flush(self.notifier)
        report = self.status()
        atomic_json(self.runtime / "copier_status.json", report)
        return report

    def close(self) -> None:
        if self.ledger:
            self.ledger.save()
            self.ledger.flush(self.notifier)
            self.ledger.db.close()
        if self.observer_db:
            self.observer_db.close()
        if self.personal_source is not None:
            self.personal_source.close()
        for lease in reversed(self.leases):
            lease.close()
        self.leases = []


def run_copier(config: Mapping[str, Any], broker: Any, *, observer_runtime: Path,
               runtime: Path, coordinator_root: Path, notifier: Any = None,
               stop_event: threading.Event | None = None, once: bool = False) -> dict[str, Any]:
    """Run the independent Paper consumer; runner owns credentials/notifier life."""
    stopping = stop_event or threading.Event()
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if number is not None:
                previous[number] = signal.signal(number, lambda *_: stopping.set())
    engine = None
    report: dict[str, Any] = {}
    try:
        engine = CopyEngine(config, broker, observer_runtime=observer_runtime, runtime=runtime,
                            coordinator_root=coordinator_root, notifier=notifier, stop_event=stopping)
        while not stopping.is_set():
            report = engine.step()
            if once:
                break
            remaining = float(engine.config["poll_seconds"])
            while remaining > 0 and not stopping.is_set():
                interval = min(remaining, 1.0)
                stopping.wait(interval)
                remaining -= interval
        return report
    finally:
        if engine is not None:
            engine.close()
        for number, handler in previous.items():
            signal.signal(number, handler)
