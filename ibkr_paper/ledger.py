"""Independent durable intent/fill ledger for the IBKR Paper executor.

An order identity and its full debit reservation are committed before a write
can be attempted. A missing broker order is never proof of a failed write.
"""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .common import PaperError, now, timestamp


SHA = re.compile(r"[a-f0-9]{64}\Z")
REF = re.compile(r"AAIBP:[a-f0-9]{32}\Z")
OCC = re.compile(r"[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}\Z")
PHASES = {"INTENT", "UNKNOWN", "PENDING", "SETTLING", "TERMINAL", "NOT_SENT"}
TERMINAL = {"Filled", "Cancelled", "ApiCancelled"}


class LedgerError(PaperError):
    permanent = True


def decimal(value, *, zero=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise LedgerError("paper_ledger_number_unconfirmed")
    try:
        result = Decimal(str(value))
        if (not result.is_finite() or result < 0 or (not zero and result == 0)
                or result > Decimal("1000000000") or result.as_tuple().exponent < -12):
            raise InvalidOperation
        return result
    except (ValueError, InvalidOperation, OverflowError):
        raise LedgerError("paper_ledger_number_unconfirmed") from None


def whole(value, *, zero=False) -> int:
    result = decimal(value, zero=zero)
    if result != int(result) or result > 1000000:
        raise LedgerError("paper_ledger_quantity_unconfirmed")
    return int(result)


def source_identity(source_id: str) -> str:
    if not isinstance(source_id, str) or not re.fullmatch(r"discord:[0-9]+:[0-9]+:[0-9]+", source_id):
        raise LedgerError("paper_source_identifier_unconfirmed")
    return hashlib.sha256(source_id.encode()).hexdigest()


class PaperLedger:
    """A single fully synced SQLite state row plus immutable execution evidence."""

    def __init__(self, path: Path, *, account_sha256: str, client_id: int, config_binding: str):
        self.path = Path(path)
        if (self.path.is_symlink() or self.path.parent.is_symlink()
                or not isinstance(account_sha256, str) or not SHA.fullmatch(account_sha256)
                or not isinstance(config_binding, str) or not SHA.fullmatch(config_binding)
                or type(client_id) is not int or client_id <= 0):
            raise LedgerError("paper_ledger_binding_required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA trusted_schema=OFF")
        self.db.execute("CREATE TABLE IF NOT EXISTS paper_state (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
        row = self.db.execute("SELECT payload FROM paper_state WHERE id=1").fetchone()
        if row is None:
            self.state = {"schema_version": 1, "account_sha256": account_sha256,
                          "client_id": client_id, "config_binding": config_binding,
                          "activated_at": now().isoformat(), "orders": {}, "lots": {},
                          "sources": {}, "fills": {}, "notices": {}, "halts": []}
            self.save()
        else:
            try:
                if not isinstance(row[0], str) or len(row[0].encode()) > 16 * 1024 * 1024:
                    raise ValueError
                self.state = json.loads(row[0])
                if (self.state.get("account_sha256") != account_sha256
                        or self.state.get("client_id") != client_id
                        or self.state.get("config_binding") != config_binding):
                    raise LedgerError("paper_ledger_account_or_policy_changed")
                self._validate()
            except LedgerError:
                self.db.close()
                raise
            except Exception:
                self.db.close()
                raise LedgerError("paper_ledger_integrity") from None

    def _validate(self):
        s = self.state
        if (not isinstance(s, dict) or set(s) != {"schema_version", "account_sha256", "client_id", "config_binding",
                                                 "activated_at", "orders", "lots", "sources", "fills", "notices", "halts"}
                or type(s["schema_version"]) is not int or s["schema_version"] != 1
                or any(not isinstance(s[k], dict) for k in ("orders", "lots", "sources", "fills", "notices"))
                or not isinstance(s["halts"], list) or len(s["orders"]) > 20000 or len(s["sources"]) > 50000
                or len(s["fills"]) > 100000 or len(s["notices"]) > 100000
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", v) for v in s["halts"])):
            raise LedgerError("paper_ledger_integrity")
        timestamp(s["activated_at"])
        identities = set()
        fill_totals = {}
        for exec_id, execution in s["fills"].items():
            if (not isinstance(exec_id, str) or not 1 <= len(exec_id) <= 128 or not isinstance(execution, dict)
                    or execution.get("exec_id") != exec_id or execution.get("order_ref") not in s["orders"]):
                raise LedgerError("paper_ledger_integrity")
            timestamp(execution["executed_utc"])
            ref = execution["order_ref"]
            qty, price = whole(execution["quantity"]), decimal(execution["price"])
            old_qty, old_notional = fill_totals.get(ref, (0, Decimal(0)))
            fill_totals[ref] = (old_qty + qty, old_notional + price * qty * 100)
        for ref, order in s["orders"].items():
            if (not isinstance(ref, str) or not REF.fullmatch(ref) or not isinstance(order, dict)
                    or order.get("order_ref") != ref or order.get("phase") not in PHASES
                    or order.get("action") not in {"BUY", "SELL"}
                    or order.get("client_id") != s["client_id"]
                    or type(order.get("order_id")) is not int or order["order_id"] < 0
                    or order.get("lot_id") not in s["lots"]
                    or not isinstance(order.get("occ_symbol"), str) or not OCC.fullmatch(order["occ_symbol"])
                    or not isinstance(order.get("source_revision"), str) or not SHA.fullmatch(order["source_revision"])
                    or any(type(order.get(k)) is not bool for k in ("write_attempted", "cancel_attempted"))):
                raise LedgerError("paper_ledger_integrity")
            identity = (order["client_id"], order["order_id"])
            if identity in identities:
                raise LedgerError("paper_ledger_order_id_reused")
            identities.add(identity)
            qty, filled = whole(order.get("quantity")), whole(order.get("filled_qty"), zero=True)
            if not qty <= 50 or filled > qty or (order["phase"] == "NOT_SENT" and (filled or order["write_attempted"])):
                raise LedgerError("paper_ledger_integrity")
            decimal(order.get("limit_price"))
            decimal(order.get("filled_notional_usd"), zero=True)
            decimal(order.get("fee_reserve_usd"), zero=True)
            if type(order.get("con_id")) is not int or order["con_id"] <= 0:
                raise LedgerError("paper_ledger_integrity")
            source_identity(order["source_id"])
            for field in ("created_utc", "signal_created_utc", "signal_received_utc"):
                timestamp(order[field])
            if order.get("submitted_utc") is not None:
                timestamp(order["submitted_utc"])
            actual_qty, actual_notional = fill_totals.get(ref, (0, Decimal(0)))
            if (actual_qty != filled or actual_notional != decimal(order["filled_notional_usd"], zero=True)):
                raise LedgerError("paper_ledger_fill_totals_disagree")
        for lot_id, lot in s["lots"].items():
            if (not isinstance(lot_id, str) or not SHA.fullmatch(lot_id) or lot.get("lot_id") != lot_id
                    or lot.get("entry_ref") not in s["orders"] or not isinstance(lot.get("exit_refs"), list)
                    or len(lot["exit_refs"]) != len(set(lot["exit_refs"]))):
                raise LedgerError("paper_ledger_integrity")
            entry = s["orders"][lot["entry_ref"]]
            if entry["action"] != "BUY" or entry["lot_id"] != lot_id:
                raise LedgerError("paper_ledger_integrity")
            exits = [s["orders"][ref] for ref in lot["exit_refs"]]
            if (any(o["action"] != "SELL" or o["lot_id"] != lot_id or o["occ_symbol"] != entry["occ_symbol"] for o in exits)
                    or sum(o["filled_qty"] for o in exits) > entry["filled_qty"]
                    or sum(o["phase"] not in {"TERMINAL", "NOT_SENT"} for o in exits) > 1):
                raise LedgerError("paper_ledger_ownership_unconfirmed")
        for key, source in s["sources"].items():
            if (not isinstance(key, str) or not SHA.fullmatch(key) or not isinstance(source, dict)
                    or source_identity(source.get("source_id")) != key
                    or type(source.get("invalidated")) is not bool):
                raise LedgerError("paper_ledger_integrity")
        for key, notice in s["notices"].items():
            if (not isinstance(key, str) or not re.fullmatch(r"ibkr-paper:[a-f0-9]{64}", key)
                    or not isinstance(notice, dict) or type(notice.get("queued")) is not bool
                    or not isinstance(notice.get("title"), str) or not isinstance(notice.get("fields"), dict)):
                raise LedgerError("paper_ledger_integrity")

    def save(self):
        try:
            self._validate()
            raw = json.dumps(self.state, sort_keys=True, allow_nan=False, separators=(",", ":"))
            if len(raw.encode()) > 16 * 1024 * 1024:
                raise ValueError
            with self.db:
                self.db.execute("INSERT INTO paper_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (raw,))
        except LedgerError:
            raise
        except Exception:
            raise LedgerError("paper_ledger_commit_failed") from None

    def halt(self, reason):
        if reason not in self.state["halts"]:
            self.state["halts"].append(reason)
            self.save()

    def notice(self, identity: str, title: str, fields: dict):
        event_id = "ibkr-paper:" + hashlib.sha256(identity.encode()).hexdigest()
        self.state["notices"].setdefault(event_id, {"title": title, "fields": dict(fields), "queued": False})

    def flush(self, notifier):
        if notifier is None:
            return
        for key, item in self.state["notices"].items():
            if item["queued"]:
                continue
            try:
                accepted = notifier.enqueue(key, item["title"], item["fields"])
            except Exception:
                accepted = False
            if accepted is True:
                item["queued"] = True
                self.save()

    def remaining(self, lot: dict) -> int:
        entry = self.state["orders"][lot["entry_ref"]]
        result = entry["filled_qty"] - sum(self.state["orders"][ref]["filled_qty"] for ref in lot["exit_refs"])
        if result < 0:
            raise LedgerError("paper_ledger_ownership_unconfirmed")
        return result

    def used_usd(self) -> Decimal:
        total = Decimal(0)
        for lot in self.state["lots"].values():
            entry = self.state["orders"][lot["entry_ref"]]
            remaining = self.remaining(lot)
            if remaining:
                total += decimal(entry["filled_notional_usd"], zero=True) * remaining / entry["filled_qty"]
            pending = entry["phase"] not in {"TERMINAL", "NOT_SENT"}
            if pending:
                total += decimal(entry["limit_price"]) * 100 * (entry["quantity"] - entry["filled_qty"])
            if remaining or pending:
                total += decimal(entry["fee_reserve_usd"], zero=True)
        return total

    def max_order_id(self) -> int:
        return max((o["order_id"] for o in self.state["orders"].values()), default=-1)

    def close(self):
        self.db.close()
