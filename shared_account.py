"""Account-bound coordination for the stock and option Alpaca Paper strategies.

The registry identifies orders by persisted intent and exact broker fields, not
by client-ID prefixes. All writers use the same OS lock and keep it held across
one broker write. Unknown writes retain their risk reservation and close the
whole account's new-entry gate. This coordinates one host/shared filesystem;
executors on another host must not trade this account concurrently.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping


TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "failed", "voided", "replaced"}
PENDING = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding", "held",
           "pending_cancel", "pending_replace", "stopped", "suspended", "calculated", "done_for_day"}
ASSETS = {"options": "us_option", "stocks": "us_equity"}
_ID = re.compile(r"[A-Za-z0-9._:\-]{1,128}")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,30}")
_GLOBAL_LOCKS: dict[str, tuple[threading.RLock, threading.local]] = {}
_LOCKS_GUARD = threading.Lock()


class SharedAccountError(ValueError):
    category = "ledger_integrity"
    recoverable = False


class SharedAccountBlocked(SharedAccountError):
    category = "shared_account_pending"
    recoverable = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SharedAccountError("Shared registry requires an exact valid broker/client order ID")
    return value


def _decimal(value: Any, *, positive: bool = True, integer: bool = False) -> str:
    if isinstance(value, bool):
        raise SharedAccountError("Shared order quantities/prices cannot be boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SharedAccountError("Shared order quantities/prices must be numeric") from None
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        raise SharedAccountError("Shared order quantities/prices are invalid")
    if integer and parsed != parsed.to_integral_value():
        raise SharedAccountError("Shared strategies only support whole shares/contracts")
    return format(parsed.normalize(), "f")


def _inventory_time(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise SharedAccountError("Inventory adoption requires its real observed timestamp") from None
    if stamp.tzinfo is None or (stamp - datetime.now(timezone.utc)).total_seconds() > 5:
        raise SharedAccountError("Inventory adoption timestamp is unconfirmed")
    return stamp.astimezone(timezone.utc).isoformat()


def _identity(raw: Mapping[str, Any], *, strategy: str | None = None) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise SharedAccountError("Shared order data must be an object")
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise SharedAccountError("Shared order symbol is invalid")
    asset = raw.get("asset_class")
    if asset not in ASSETS.values() or (strategy is not None and asset != ASSETS[strategy]):
        raise SharedAccountError("Shared order asset class does not match its strategy")
    if asset == "us_option" and not re.fullmatch(r"[A-Z]{1,6}\d{6}[CP]\d{8}", symbol):
        raise SharedAccountError("Option ownership requires a standard OCC contract symbol")
    if asset == "us_equity" and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        raise SharedAccountError("Stock ownership requires an equity/ETF ticker")
    if raw.get("side") not in ("buy", "sell") or raw.get("type") not in ("market", "limit", "stop", "stop_limit"):
        raise SharedAccountError("Shared order side/type is unsupported")
    if raw.get("time_in_force") not in ("day", "gtc") or raw.get("extended_hours", False) is not False:
        raise SharedAccountError("Shared orders require regular-session day/gtc execution")
    order_class = raw.get("order_class") or "simple"
    if order_class not in ("simple", "bracket", "oco"):
        raise SharedAccountError("Shared order class is unsupported")
    identity = {
        "symbol": symbol, "asset_class": asset, "side": str(raw["side"]),
        "qty": _decimal(raw.get("qty"), integer=True), "type": str(raw["type"]),
        "time_in_force": str(raw["time_in_force"]), "order_class": str(order_class),
        "client_order_id": _identifier(raw.get("client_order_id")),
    }
    for key in ("limit_price", "stop_price"):
        value = raw.get(key)
        required = (key == "limit_price" and identity["type"] in ("limit", "stop_limit")) or (
            key == "stop_price" and identity["type"] in ("stop", "stop_limit"))
        if required:
            identity[key] = _decimal(value)
        elif value is not None:
            raise SharedAccountError("Shared order contains an unexpected limit/stop price")
    return identity


class SharedAccountCoordinator:
    def __init__(self, account_id: str, root: Path, strategy: str, *,
                 strategy_cap_usd: float = 10000, total_cap_usd: float = 20000,
                 stock_budget_usd: float | None = None):
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            raise SharedAccountError("A verified account ID is required")
        if strategy not in ASSETS:
            raise SharedAccountError("Shared strategy must be stocks or options")
        if strategy == "options" and (strategy_cap_usd != 10000 or stock_budget_usd is not None):
            raise SharedAccountError("Options cannot change the shared stock budget or its own 10000 cap")
        if total_cap_usd not in (20000, 50000):
            raise SharedAccountError("Shared total cap must correspond to the explicitly configured stock budget")
        requested_stock_cap = stock_budget_usd
        if strategy == "stocks" and requested_stock_cap is None and strategy_cap_usd != 10000:
            requested_stock_cap = strategy_cap_usd
        if requested_stock_cap is not None and (strategy != "stocks" or requested_stock_cap not in (10000, 40000)):
            raise SharedAccountError("Explicit stock Paper budget must be 10000 or 40000")
        if strategy == "stocks" and strategy_cap_usd not in (10000, 40000):
            raise SharedAccountError("Stock Paper cap is not an authorized supported configuration")
        self.account_id, self.strategy = account_id, strategy
        self.account_sha256 = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        self.root = Path(root).resolve() / self.account_sha256
        if self.root.is_symlink():
            raise SharedAccountError("Shared account directory cannot be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)
        self.path = self.root / "registry.json"
        self.lock_path = self.root / "account.lock"
        with _LOCKS_GUARD:
            self._thread_lock, self._local = _GLOBAL_LOCKS.setdefault(str(self.lock_path), (threading.RLock(), threading.local()))
        with self.lock():
            if not self.path.exists():
                self._save({"schema_version": 1, "account_id": account_id,
                            "account_sha256": self.account_sha256,
                            "strategy_caps_usd": {"stocks": 10000, "options": 10000},
                            "total_cap_usd": 20000, "intents": {}, "orders": {},
                            "opening_inventory": {}, "states": {}, "updated_at": _now()})
            value = self._load()
            if requested_stock_cap is not None and value["strategy_caps_usd"]["stocks"] != requested_stock_cap:
                value["strategy_caps_usd"]["stocks"] = requested_stock_cap
                value["total_cap_usd"] = requested_stock_cap + 10000
                self._save(value)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        with self._thread_lock:
            depth = getattr(self._local, "depth", 0)
            handle = None
            try:
                if not depth:
                    if self.lock_path.is_symlink():
                        raise SharedAccountError("Shared lock file cannot be a symbolic link")
                    handle = self.lock_path.open("a+b")
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0, os.SEEK_END)
                        if handle.tell() == 0:
                            handle.write(b"0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        os.chmod(self.lock_path, 0o600)
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._local.depth = depth + 1
                yield
            except OSError as exc:
                raise SharedAccountError("Shared account lock or persistence is unavailable") from exc
            finally:
                self._local.depth = depth
                if handle is not None:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()

    def _load(self) -> dict[str, Any]:
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 64 * 1024 * 1024:
            raise SharedAccountError("Shared registry is missing or invalid; it must not be reset automatically")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SharedAccountError("Shared registry cannot be read; automatic trading is blocked") from None
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("account_id") != self.account_id or value.get("account_sha256") != self.account_sha256
                or not isinstance(value.get("strategy_caps_usd"), dict)
                or value["strategy_caps_usd"].get("options") != 10000
                or value["strategy_caps_usd"].get("stocks") not in (10000, 40000)
                or value.get("total_cap_usd") != value["strategy_caps_usd"]["stocks"] + 10000
                or any(not isinstance(value.get(key), dict) for key in ("intents", "orders", "states"))):
            raise SharedAccountError("Shared registry account binding/schema does not match")
        value.setdefault("opening_inventory", {})
        if not isinstance(value["opening_inventory"], dict):
            raise SharedAccountError("Shared opening inventory registry is malformed")
        try:
            for inventory_id, inventory in value["opening_inventory"].items():
                if (not isinstance(inventory, dict) or inventory_id != inventory.get("inventory_id")
                        or inventory.get("strategy") != "stocks" or inventory.get("asset_class") != "us_equity"
                        or inventory.get("account_sha256") != self.account_sha256
                        or inventory.get("source_account_sha256") != self.account_sha256
                        or inventory.get("source") != "verified_broker_opening_inventory"
                        or not isinstance(inventory.get("symbol"), str)
                        or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", inventory["symbol"])):
                    raise SharedAccountError("Opening inventory account/source binding is invalid")
                _identifier(inventory_id)
                _decimal(inventory["qty"], integer=True)
                _decimal(inventory["cost_usd"])
                _decimal(inventory["adoption_mark"])
                _inventory_time(inventory["adopted_at"])
            for cid, item in value["intents"].items():
                if (not isinstance(item, dict) or not isinstance(item.get("payload"), dict)
                        or cid != item["payload"].get("client_order_id")
                        or item.get("strategy") not in ASSETS or item.get("account_sha256") != self.account_sha256
                        or type(item.get("write_attempted")) is not bool
                        or item.get("write_state") not in {"prepared", "submitting", "unknown", "submitted", "not_sent", "rejected", "imported"}):
                    raise SharedAccountError("Shared registry intent binding is invalid")
                if _identity(item["payload"], strategy=item["strategy"]) != item["identity"]:
                    raise SharedAccountError("Shared registry intent identity changed")
                inventory_id = item["payload"].get("opening_inventory_id")
                if inventory_id is not None:
                    inventory = value["opening_inventory"].get(inventory_id)
                    if (not inventory or item["strategy"] != "stocks" or item["identity"]["side"] != "sell"
                            or inventory["symbol"] != item["identity"]["symbol"]):
                        raise SharedAccountError("Shared exit is detached from its opening inventory anchor")
                fee = float(item["fee_reserve_usd"])
                if not math.isfinite(fee) or fee < 0:
                    raise SharedAccountError("Shared registry fee reserve is invalid")
            for oid, item in value["orders"].items():
                if (not isinstance(item, dict) or oid != item.get("id") or item.get("strategy") not in ASSETS
                        or item.get("account_sha256") != self.account_sha256
                        or item.get("root_client_order_id") not in value["intents"]):
                    raise SharedAccountError("Shared registry order binding is invalid")
                _identifier(oid)
                identity = _identity(item["identity"], strategy=item["strategy"])
                intent = value["intents"][item["root_client_order_id"]]
                if identity != item["identity"] or intent["strategy"] != item["strategy"]:
                    raise SharedAccountError("Shared registry order identity is not canonical")
                if item.get("parent_id") is None:
                    expected = dict(intent["identity"])
                    if expected["order_class"] == "oco" and float(identity["qty"]) <= float(expected["qty"]):
                        expected["qty"] = identity["qty"]
                    if identity != expected or intent.get("order_id") not in (None, oid):
                        raise SharedAccountError("Shared registry parent is detached from its exact intent")
                else:
                    parent = value["orders"].get(item["parent_id"])
                    base = intent["identity"]
                    if (not parent or parent["root_client_order_id"] != item["root_client_order_id"]
                            or base["order_class"] not in ("bracket", "oco") or identity["symbol"] != base["symbol"]
                            or identity["side"] != "sell" or identity["asset_class"] != base["asset_class"]
                            or float(identity["qty"]) > float(base["qty"])):
                        raise SharedAccountError("Shared registry protection is detached from its exact parent")
                filled = float(_decimal(item["filled_qty"], positive=False, integer=True))
                if filled > float(identity["qty"]) or item["status"] not in TERMINAL | PENDING:
                    raise SharedAccountError("Shared registry fill/status is invalid")
                if filled:
                    _decimal(item["filled_avg_price"])
                if type(item.get("observation_unknown")) is not bool or item.get("cancel_state") not in (None, "submitting", "unknown", "confirmed", "not_sent"):
                    raise SharedAccountError("Shared registry operation state is invalid")
            for owner, state in value["states"].items():
                if (owner not in ASSETS or not isinstance(state, dict)
                        or not isinstance(state.get("positions"), list) or not isinstance(state.get("pending"), list)
                        or type(state.get("unconfirmed", False)) is not bool):
                    raise SharedAccountError("Shared registry strategy state is invalid")
                for pending in state["pending"]:
                    cid = _identifier(pending["client_order_id"])
                    if value["intents"][cid]["strategy"] != owner or not isinstance(pending["status"], str):
                        raise SharedAccountError("Shared pending state belongs to another strategy")
                    _decimal(pending["reserved_usd"], positive=False)
            self._risk(value)  # Reject malformed quantities and negative inferred holdings.
        except SharedAccountError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise SharedAccountError("Shared registry contents are malformed; automatic trading is blocked") from None
        return value

    def _save(self, value: dict[str, Any]) -> None:
        value["updated_at"] = _now()
        descriptor, temporary = tempfile.mkstemp(prefix="registry-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, ValueError, TypeError):
            raise SharedAccountError("Shared intent cannot be persisted; no broker write is permitted") from None
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _risk(self, value: dict[str, Any]) -> dict[str, float]:
        totals = {strategy: 0.0 for strategy in ASSETS}
        buys: dict[tuple[str, str], list[list[float]]] = {}
        sells: dict[tuple[str, str], float] = {}
        inventory_sells: dict[str, float] = {}
        for item in value["orders"].values():
            identity = item["identity"]
            qty = float(_decimal(item.get("filled_qty", "0"), positive=False, integer=True))
            if not qty:
                continue
            average = float(_decimal(item.get("filled_avg_price")))
            key = item["strategy"], identity["symbol"]
            root_intent = value["intents"][item["root_client_order_id"]]
            inventory_id = root_intent["payload"].get("opening_inventory_id")
            multiplier = 100 if identity["asset_class"] == "us_option" else 1
            if identity["side"] == "buy":
                buys.setdefault(key, []).append([qty, average * multiplier])
            else:
                if inventory_id is not None:
                    inventory_sells[inventory_id] = inventory_sells.get(inventory_id, 0.0) + qty
                else:
                    sells[key] = sells.get(key, 0.0) + qty
        for key, lots in buys.items():
            remaining_sold = sells.pop(key, 0.0)
            for qty, unit_cost in lots:
                reduced = min(qty, remaining_sold)
                remaining_sold -= reduced
                totals[key[0]] += (qty - reduced) * unit_cost
            if remaining_sold > 1e-8:
                raise SharedAccountError("Shared confirmed sales exceed managed purchases")
        if any(qty > 0 for qty in sells.values()):
            raise SharedAccountError("Shared confirmed sale has no managed purchase")
        for inventory_id, inventory in value.get("opening_inventory", {}).items():
            original_qty = float(inventory["qty"])
            remaining = original_qty - inventory_sells.pop(inventory_id, 0.0)
            if remaining < 0:
                raise SharedAccountError("Confirmed inventory sales exceed the adopted real quantity")
            # Grandfather an already owned position without pretending that the
            # strategy bought it. New entries still obey the persisted caps.
            capital = max(float(inventory["cost_usd"]), float(inventory["adoption_mark"]) * original_qty)
            totals["stocks"] += capital * remaining / original_qty
        if inventory_sells:
            raise SharedAccountError("Confirmed sale references unregistered opening inventory")
        for cid, item in value["intents"].items():
            identity = item["identity"]
            if identity["side"] != "buy" or item["write_state"] in {"not_sent", "rejected"}:
                continue
            parent = value["orders"].get(item.get("order_id", ""))
            if parent and parent["status"] in TERMINAL:
                unfilled = 0.0
            else:
                unfilled = float(identity["qty"]) - (float(parent["filled_qty"]) if parent else 0.0)
            multiplier = 100 if identity["asset_class"] == "us_option" else 1
            totals[item["strategy"]] += unfilled * float(identity["limit_price"]) * multiplier
            if unfilled > 0 or (parent and float(parent["filled_qty"]) > 0 and parent["status"] not in TERMINAL):
                totals[item["strategy"]] += float(item.get("fee_reserve_usd", 0.0))
        if any(not math.isfinite(amount) or amount < 0 for amount in totals.values()):
            raise SharedAccountError("Shared risk totals are invalid")
        return totals

    @staticmethod
    def _unresolved(value: dict[str, Any], *, exclude: str | None = None) -> list[str]:
        unresolved = [cid for cid, item in value["intents"].items()
                      if cid != exclude and item["write_state"] in {"prepared", "submitting", "unknown"}]
        unresolved.extend(oid for oid, item in value["orders"].items()
                          if item.get("cancel_state") in {"submitting", "unknown"} or item.get("observation_unknown"))
        for strategy, state in value["states"].items():
            if state.get("unconfirmed"):
                unresolved.append("state:" + strategy)
            unresolved.extend(item["client_order_id"] for item in state.get("pending", [])
                              if item["client_order_id"] != exclude
                              and item.get("status", "").lower() in {"unknown", "entry_unknown", "exit_unknown", "hold"})
        return unresolved

    def _allowed(self, value: dict[str, Any], reserve: float = 0.0, *, exclude: str | None = None) -> bool:
        if not math.isfinite(reserve) or reserve < 0:
            raise SharedAccountError("Shared entry reserve is invalid")
        usage = self._risk(value)
        return (not self._unresolved(value, exclude=exclude)
                and all(amount <= value["strategy_caps_usd"][owner] + 1e-8 for owner, amount in usage.items())
                and usage[self.strategy] + reserve <= value["strategy_caps_usd"][self.strategy] + 1e-8
                and sum(usage.values()) + reserve <= value["total_cap_usd"] + 1e-8)

    def _check_owned_exit(self, value: dict[str, Any], identity: dict[str, str], *, exclude: str | None = None,
                          opening_inventory_id: str | None = None) -> None:
        symbol = identity["symbol"]
        orders = [item for item in value["orders"].values()
                  if item["strategy"] == self.strategy and item["identity"]["symbol"] == symbol
                  and value["intents"][item["root_client_order_id"]]["payload"].get("opening_inventory_id") == opening_inventory_id]
        if opening_inventory_id is not None:
            inventory = value["opening_inventory"].get(opening_inventory_id)
            if (not inventory or self.strategy != "stocks" or inventory["symbol"] != symbol
                    or identity["asset_class"] != "us_equity"):
                raise SharedAccountError("Exit does not match its account-bound adopted stock inventory")
            owned_qty = self._inventory_remaining(value, opening_inventory_id)
        else:
            bought = sum(float(item["filled_qty"]) for item in orders if item["identity"]["side"] == "buy")
            sold = sum(float(item["filled_qty"]) for item in orders if item["identity"]["side"] == "sell")
            owned_qty = bought - sold
        if float(identity["qty"]) > owned_qty:
            raise SharedAccountBlocked("Exit quantity is not covered by confirmed managed inventory")
        for cid, intent in value["intents"].items():
            if (cid == exclude or intent["strategy"] != self.strategy
                    or intent["identity"]["symbol"] != symbol or intent["identity"]["side"] != "sell"
                    or intent["payload"].get("opening_inventory_id") != opening_inventory_id
                    or intent["write_state"] in {"not_sent", "rejected"}):
                continue
            order = value["orders"].get(intent.get("order_id", ""))
            if order is None or order["status"] not in TERMINAL:
                raise SharedAccountBlocked("An existing managed exit must reach a confirmed terminal state")
        if any(item["identity"]["side"] == "sell" and item.get("parent_id")
               and item["status"] not in TERMINAL for item in orders):
            raise SharedAccountBlocked("Broker protective exits must be confirmed canceled before a standalone close")

    def snapshot(self) -> dict[str, Any]:
        with self.lock():
            value = self._load()
            usage = self._risk(value)
            unresolved = self._unresolved(value)
            return {"schema_version": 1, "account_sha256": self.account_sha256,
                    "strategy": self.strategy, "risk_used_usd": usage,
                    "total_risk_used_usd": sum(usage.values()), "strategy_caps_usd": value["strategy_caps_usd"],
                    "total_cap_usd": value["total_cap_usd"], "pending_write_count": len(unresolved),
                    "entries_allowed": self._allowed(value), "registered_orders": len(value["orders"]),
                    "updated_at": value["updated_at"]}

    def entry_allowed(self, reserve_usd: float = 0.0) -> bool:
        with self.lock():
            scope = getattr(self._local, "active_write", None)
            exclude = scope[0] if scope is not None and scope[1] == self.strategy else None
            return self._allowed(self._load(), float(reserve_usd), exclude=exclude)

    @contextlib.contextmanager
    def write_scope(self, client_id: str) -> Iterator[None]:
        """Exclude only this in-flight buy from its own final entry callback.

        The caller must already hold the OS account lock through the broker
        write. The scope is per thread, per account, exact CID, and strategy;
        it never hides another unresolved write or survives an exception.
        """
        if not getattr(self._local, "depth", 0) or getattr(self._local, "active_write", None) is not None:
            raise SharedAccountError("Shared write scope requires the held account lock and cannot nest")
        cid = _identifier(client_id)
        item = self._load()["intents"].get(cid)
        if not item or item["strategy"] != self.strategy or item["write_state"] != "submitting" or not item["write_attempted"]:
            raise SharedAccountError("Shared write scope must match the one persisted in-flight intent")
        self._local.active_write = cid, self.strategy
        try:
            yield
        finally:
            self._local.active_write = None

    def intent_state(self, client_id: str) -> dict[str, Any] | None:
        with self.lock():
            item = self._load()["intents"].get(_identifier(client_id))
            if item is None:
                return None
            if item["strategy"] != self.strategy:
                raise SharedAccountError("Client order ID belongs to another registered strategy")
            return {"write_state": item["write_state"], "write_attempted": item["write_attempted"],
                    "order_id": item.get("order_id"), "identity": copy.deepcopy(item["identity"])}

    def register_inventory(self, inventory_id: str, record: Mapping[str, Any]) -> None:
        """Adopt an explicitly authorized, broker-verified opening stock holding.

        This is a separate inventory anchor, never a fabricated purchase, fill,
        client ID or broker order ID. The caller must verify the live account,
        positions and orders before calling it and keep its ledger anchor.
        """
        if self.strategy != "stocks" or not isinstance(record, Mapping):
            raise SharedAccountError("Only the stock strategy can adopt verified opening inventory")
        inventory_id = _identifier(inventory_id)
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise SharedAccountError("Opening inventory must identify a stock/ETF ticker")
        source_account = record.get("source_account_sha256") or record.get("source_account_sha")
        if source_account != self.account_sha256:
            raise SharedAccountError("Opening inventory source account does not match the verified account")
        item = {"inventory_id": inventory_id, "strategy": "stocks", "asset_class": "us_equity",
                "account_sha256": self.account_sha256, "source_account_sha256": self.account_sha256,
                "symbol": symbol, "qty": _decimal(record.get("qty"), integer=True),
                "cost_usd": _decimal(record.get("cost_usd")),
                "adoption_mark": _decimal(record.get("adoption_mark")),
                "adopted_at": _inventory_time(record.get("adopted_at")),
                "source": "verified_broker_opening_inventory"}
        if any(not math.isfinite(float(item[key])) for key in ("qty", "cost_usd", "adoption_mark")):
            raise SharedAccountError("Opening inventory exposure must be finite")
        with self.lock():
            value = self._load()
            existing = value["opening_inventory"].get(inventory_id)
            if existing is not None:
                if existing != item:
                    raise SharedAccountError("Opening inventory ID cannot be rebound to a new holding or mark")
                return
            for known_id, known in value["opening_inventory"].items():
                if known["symbol"] == symbol and self._inventory_remaining(value, known_id) > 0:
                    raise SharedAccountError("An opening inventory holding for this ticker is already registered")
            managed_buys = sum(float(order["filled_qty"]) for order in value["orders"].values()
                               if order["strategy"] == "stocks" and order["identity"]["symbol"] == symbol
                               and order["identity"]["side"] == "buy")
            managed_sells = sum(float(order["filled_qty"]) for order in value["orders"].values()
                                if order["strategy"] == "stocks" and order["identity"]["symbol"] == symbol
                                and order["identity"]["side"] == "sell"
                                and not value["intents"][order["root_client_order_id"]]["payload"].get("opening_inventory_id"))
            if managed_buys > managed_sells:
                raise SharedAccountError("Opening inventory adoption would overlap an existing strategy purchase")
            value["opening_inventory"][inventory_id] = item
            self._risk(value)
            self._save(value)

    @staticmethod
    def _inventory_remaining(value: dict[str, Any], inventory_id: str) -> float:
        inventory = value["opening_inventory"].get(inventory_id)
        if inventory is None:
            raise SharedAccountError("Opening inventory identity is not registered")
        sold = sum(float(order["filled_qty"]) for order in value["orders"].values()
                   if value["intents"][order["root_client_order_id"]]["payload"].get("opening_inventory_id") == inventory_id
                   and order["identity"]["side"] == "sell")
        remaining = float(inventory["qty"]) - sold
        if remaining < 0:
            raise SharedAccountError("Actual inventory exits exceeded the adopted holding")
        return remaining

    def inventory_state(self, inventory_id: str) -> dict[str, Any] | None:
        with self.lock():
            value = self._load()
            item = value["opening_inventory"].get(_identifier(inventory_id))
            if item is None:
                return None
            remaining = self._inventory_remaining(value, inventory_id)
            original = float(item["qty"])
            return {**copy.deepcopy(item), "original_qty": original, "remaining_qty": remaining,
                    "cost_usd": float(item["cost_usd"]),
                    "remaining_cost_usd": float(item["cost_usd"]) * remaining / original,
                    "adoption_mark": float(item["adoption_mark"]),
                    "risk_used_usd": max(float(item["cost_usd"]), float(item["adoption_mark"]) * original) * remaining / original}

    def pause_state(self, reason: str = "state_unconfirmed") -> None:
        if reason not in {"state_unconfirmed", "quantity_mismatch", "response_schema", "ledger_integrity", "network_unconfirmed"}:
            raise SharedAccountError("Shared state pause reason is invalid")
        with self.lock():
            value = self._load()
            state = value["states"].setdefault(self.strategy, {"positions": [], "pending": []})
            state["unconfirmed"], state["reason"], state["observed_at"] = True, reason, _now()
            self._save(value)

    def register_intent(self, client_id: str, payload: Mapping[str, Any], *, imported: bool = False) -> None:
        cid = _identifier(client_id)
        order = dict(payload)
        if order.get("client_order_id") not in (None, cid):
            raise SharedAccountError("Payload client ID does not match the intent binding")
        order["client_order_id"] = cid
        identity = _identity(order, strategy=self.strategy)
        if identity["side"] == "buy" and identity["type"] != "limit":
            raise SharedAccountError("Shared new positions require a capped limit buy")
        if identity["order_class"] == "bracket":
            if self.strategy != "stocks" or identity["side"] != "buy":
                raise SharedAccountError("Only stock long entries may create bracket orders")
            tp, sl = order.get("take_profit"), order.get("stop_loss")
            if not isinstance(tp, dict) or not isinstance(sl, dict):
                raise SharedAccountError("Stock bracket intent requires exact protective prices")
            order["take_profit"] = {"limit_price": _decimal(tp.get("limit_price"))}
            order["stop_loss"] = {"stop_price": _decimal(sl.get("stop_price"))}
            if sl.get("limit_price") is not None:
                order["stop_loss"]["limit_price"] = _decimal(sl["limit_price"])
            if not float(order["stop_loss"]["stop_price"]) < float(identity["limit_price"]) < float(order["take_profit"]["limit_price"]):
                raise SharedAccountError("Stock bracket prices do not protect the intended long")
        elif identity["order_class"] == "oco":
            if self.strategy != "stocks" or identity["side"] != "sell" or identity["type"] != "limit":
                raise SharedAccountError("Only stock long exits may create protective OCO orders")
            tp, sl = order.get("take_profit"), order.get("stop_loss")
            if not isinstance(tp, dict) or not isinstance(sl, dict):
                raise SharedAccountError("OCO intent requires both exact protective prices")
            order["take_profit"] = {"limit_price": _decimal(tp.get("limit_price"))}
            order["stop_loss"] = {"stop_price": _decimal(sl.get("stop_price"))}
            if sl.get("limit_price") is not None:
                order["stop_loss"]["limit_price"] = _decimal(sl["limit_price"])
            if (order["take_profit"]["limit_price"] != identity["limit_price"]
                    or float(order["stop_loss"]["stop_price"]) >= float(identity["limit_price"])):
                raise SharedAccountError("OCO root/stop prices do not match the intended long protection")
        elif identity["order_class"] != "simple":
            raise SharedAccountError("Shared order class is not supported")
        inventory_id = order.get("opening_inventory_id", order.get("inventory_id"))
        if (order.get("opening_inventory_id") is not None and order.get("inventory_id") is not None
                and order["opening_inventory_id"] != order["inventory_id"]):
            raise SharedAccountError("Opening inventory aliases do not match")
        if inventory_id is not None:
            inventory_id = _identifier(inventory_id)
            if self.strategy != "stocks" or identity["side"] != "sell":
                raise SharedAccountError("Opening inventory can only anchor an owned stock exit")
        fee = float(order.get("fee_reserve_usd", 0.0))
        if not math.isfinite(fee) or fee < 0:
            raise SharedAccountError("Shared fee reserve is invalid")
        safe_payload = dict(identity)
        for key in ("take_profit", "stop_loss", "position_intent"):
            if key in order:
                safe_payload[key] = copy.deepcopy(order[key])
        if inventory_id is not None:
            safe_payload["opening_inventory_id"] = inventory_id
        with self.lock():
            value = self._load()
            existing = value["intents"].get(cid)
            if existing:
                if existing["strategy"] != self.strategy or existing["payload"] != safe_payload or existing["fee_reserve_usd"] != fee:
                    raise SharedAccountError("Client order ID is already bound to a different exact intent")
                return
            reservation = (float(identity["qty"]) * float(identity["limit_price"])
                           * (100 if self.strategy == "options" else 1) + fee) if identity["side"] == "buy" else 0.0
            if not imported and identity["side"] == "buy" and not self._allowed(value, reservation):
                raise SharedAccountBlocked("Shared account is awaiting reconciliation or has reached its risk cap")
            if not imported and identity["side"] == "sell":
                self._check_owned_exit(value, identity, opening_inventory_id=inventory_id)
            if inventory_id is not None:
                inventory = value["opening_inventory"].get(inventory_id)
                if not inventory or inventory["symbol"] != identity["symbol"]:
                    raise SharedAccountError("Stock exit inventory binding is missing or mismatched")
            value["intents"][cid] = {"account_sha256": self.account_sha256, "strategy": self.strategy,
                                    "identity": identity, "payload": safe_payload, "fee_reserve_usd": fee,
                                    "write_state": "unknown" if imported else "prepared",
                                    "write_attempted": bool(imported), "created_at": _now()}
            self._save(value)

    def begin_write(self, client_id: str) -> None:
        with self.lock():
            value = self._load()
            item = value["intents"].get(_identifier(client_id))
            if not item or item["strategy"] != self.strategy:
                raise SharedAccountError("Broker write has no account-bound owned intent")
            if item["write_attempted"] or item["write_state"] != "prepared":
                raise SharedAccountBlocked("Broker write was already attempted; query its client order ID")
            if item["identity"]["side"] == "buy" and not self._allowed(value, exclude=client_id):
                raise SharedAccountBlocked("Shared entry gate closed before the broker write")
            if item["identity"]["side"] == "sell":
                self._check_owned_exit(value, item["identity"], exclude=client_id,
                                       opening_inventory_id=item["payload"].get("opening_inventory_id"))
            item["write_attempted"], item["write_state"], item["attempted_at"] = True, "submitting", _now()
            self._save(value)

    def mark_not_sent(self, client_id: str) -> None:
        with self.lock():
            value = self._load()
            item = value["intents"].get(_identifier(client_id))
            if not item or item["strategy"] != self.strategy or item.get("order_id"):
                raise SharedAccountError("Cannot release an unknown or accepted order reservation")
            if item["write_state"] == "unknown":
                raise SharedAccountError("Unknown broker write cannot be declared not sent")
            item["write_state"] = "not_sent"
            self._save(value)

    def mark_unknown(self, identifier: str) -> None:
        with self.lock():
            value = self._load()
            identifier = _identifier(identifier)
            if identifier in value["intents"]:
                item = value["intents"][identifier]
                if item["strategy"] != self.strategy:
                    raise SharedAccountError("Cannot change another strategy's intent")
                item["write_state"] = "unknown"
            elif identifier in value["orders"]:
                item = value["orders"][identifier]
                item["observation_unknown"] = True
                if item.get("cancel_state") == "submitting":
                    item["cancel_state"] = "unknown"
            else:
                raise SharedAccountError("Unknown write must retain a registered exact identifier")
            self._save(value)

    def begin_cancel(self, order_id: str) -> None:
        with self.lock():
            value = self._load()
            item = value["orders"].get(_identifier(order_id))
            if not item or item["strategy"] != self.strategy:
                raise SharedAccountError("Cancel may only target an exactly owned order")
            if item.get("cancel_state") or item["status"] in TERMINAL:
                raise SharedAccountBlocked("Cancellation already attempted or order is terminal; query the order")
            item["cancel_state"] = "submitting"
            self._save(value)

    def cancel_not_sent(self, order_id: str) -> None:
        with self.lock():
            value = self._load()
            item = value["orders"].get(_identifier(order_id))
            if not item or item["strategy"] != self.strategy or item.get("cancel_state") != "submitting":
                raise SharedAccountError("Cancellation result does not match an owned active intent")
            item["cancel_state"], item["observation_unknown"] = "not_sent", False
            self._save(value)

    def _store_order(self, value: dict[str, Any], raw: Mapping[str, Any], *, owner: str,
                     root_cid: str, expected: dict[str, str], parent_id: str | None = None,
                     allow_quantity_change: bool = False) -> None:
        oid = _identifier(raw.get("id"))
        identity = _identity(raw, strategy=owner)
        if identity != expected:
            raise SharedAccountError("Broker order fields do not match the persisted exact intent")
        if raw.get("account_id") is not None and raw["account_id"] != self.account_id:
            raise SharedAccountError("Broker order belongs to a different account")
        previous = value["orders"].get(oid)
        previous_identity = dict(previous["identity"]) if previous else None
        if previous_identity is not None and allow_quantity_change:
            if float(identity["qty"]) > float(previous_identity["qty"]):
                raise SharedAccountError("Bracket protective quantity cannot increase without a new intent")
            previous_identity["qty"] = identity["qty"]
        if previous and (previous["strategy"] != owner or previous["root_client_order_id"] != root_cid
                         or previous_identity != identity):
            raise SharedAccountError("Broker order ID has changed its registered identity")
        status = str(raw.get("status", "")).lower()
        if status not in TERMINAL | PENDING:
            raise SharedAccountBlocked("Broker order status remains unconfirmed")
        filled = float(_decimal(raw.get("filled_qty"), positive=False, integer=True))
        qty = float(identity["qty"])
        if filled > qty or (previous and filled < float(previous["filled_qty"])):
            raise SharedAccountError("Broker cumulative fills decreased or exceeded the exact order quantity")
        if (((status == "filled") != (filled == qty)) or (status == "partially_filled" and not 0 < filled < qty)
                or (status in {"rejected", "failed", "voided"} and filled != 0)):
            raise SharedAccountBlocked("Broker status/fill quantities are not yet consistent")
        average = _decimal(raw.get("filled_avg_price")) if filled else None
        if filled:
            if identity["side"] == "buy" and identity["type"] == "limit" and float(average) > float(identity["limit_price"]) + 1e-8:
                raise SharedAccountError("Confirmed buy price exceeds the persisted limit")
            stamp = raw.get("filled_at") or raw.get("updated_at")
            try:
                observed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                raise SharedAccountBlocked("Confirmed fills require a valid source timestamp") from None
            if observed.tzinfo is None or (observed - datetime.now(timezone.utc)).total_seconds() > 5:
                raise SharedAccountBlocked("Confirmed fill timestamp is missing a timezone or lies in the future")
        item = {"id": oid, "account_sha256": self.account_sha256, "strategy": owner,
                "root_client_order_id": root_cid, "identity": identity, "parent_id": parent_id,
                "status": status, "filled_qty": format(Decimal(str(filled)).normalize(), "f"),
                "filled_avg_price": average, "observed_at": _now(), "observation_unknown": False}
        if previous and previous.get("cancel_state"):
            item["cancel_state"] = ("confirmed" if status in TERMINAL | {"pending_cancel"} else previous["cancel_state"])
            if item["cancel_state"] in {"submitting", "unknown"}:
                item["observation_unknown"] = True
        value["orders"][oid] = item

    def _register_order(self, value: dict[str, Any], raw: Mapping[str, Any], *, require_own: bool) -> None:
        oid, cid = _identifier(raw.get("id")), _identifier(raw.get("client_order_id"))
        prior = value["orders"].get(oid)
        if prior and prior.get("parent_id"):
            if require_own and prior["strategy"] != self.strategy:
                raise SharedAccountError("Cannot register another strategy's protective leg")
            actual_identity = _identity(raw, strategy=prior["strategy"])
            previous_identity = dict(prior["identity"])
            if actual_identity != previous_identity:
                previous_identity["qty"] = actual_identity["qty"]
                if previous_identity == actual_identity and float(actual_identity["qty"]) < float(prior["identity"]["qty"]):
                    raise SharedAccountBlocked("Bracket quantity change needs its full parent/sibling confirmation")
                raise SharedAccountError("Protective order identity differs from its registered exact parent")
            self._store_order(value, raw, owner=prior["strategy"], root_cid=prior["root_client_order_id"],
                              expected=prior["identity"], parent_id=prior["parent_id"])
            return
        intent = value["intents"].get(cid)
        if not intent or (require_own and intent["strategy"] != self.strategy):
            raise SharedAccountError("Broker order has no exact persisted owned intent")
        if intent.get("order_id") and intent["order_id"] != oid:
            raise SharedAccountError("One client order ID returned multiple broker order IDs")
        expected_root = dict(intent["identity"])
        if expected_root["order_class"] == "oco":
            actual_root = _identity(raw, strategy=intent["strategy"])
            if actual_root["qty"] != expected_root["qty"]:
                legs = raw.get("legs")
                if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
                    raise SharedAccountBlocked("OCO root quantity change requires the actual nested stop leg")
                sibling_fill = float(_decimal(legs[0].get("filled_qty"), positive=False, integer=True))
                if sibling_fill <= 0 or float(actual_root["qty"]) != float(expected_root["qty"]) - sibling_fill:
                    raise SharedAccountError("OCO root quantity change is not proved by the stop leg's actual fills")
                expected_root["qty"] = actual_root["qty"]
        self._store_order(value, raw, owner=intent["strategy"], root_cid=cid, expected=expected_root,
                          allow_quantity_change=expected_root["order_class"] == "oco")
        if intent["identity"]["order_class"] == "bracket":
            legs = raw.get("legs")
            if ((legs is None or legs == []) and value["orders"][oid]["status"] in TERMINAL
                    and float(value["orders"][oid]["filled_qty"]) == 0):
                known_legs = [item for item in value["orders"].values() if item.get("parent_id") == oid]
                if any(item["status"] not in TERMINAL for item in known_legs):
                    raise SharedAccountBlocked("Unfilled terminal bracket still has unconfirmed registered protective legs")
                intent["order_id"], intent["write_state"] = oid, "submitted"
                intent["write_attempted"] = True
                return
            if not isinstance(legs, list) or len(legs) != 2:
                raise SharedAccountBlocked("Bracket protective leg identities have not been fully returned")
            found: set[str] = set()
            validated: list[tuple[Mapping[str, Any], dict[str, str], str]] = []
            for leg in legs:
                if not isinstance(leg, Mapping):
                    raise SharedAccountError("Bracket leg response is malformed")
                expected = _identity(leg, strategy=intent["strategy"])
                base = intent["identity"]
                if (expected["symbol"] != base["symbol"] or expected["side"] != "sell"
                        or expected["time_in_force"] != base["time_in_force"]):
                    raise SharedAccountError("Bracket leg symbol/side/quantity does not match its exact parent")
                if expected["type"] == "limit" and expected.get("limit_price") == intent["payload"]["take_profit"]["limit_price"]:
                    kind = "take_profit"
                elif (expected["type"] in ("stop", "stop_limit")
                      and expected.get("stop_price") == intent["payload"]["stop_loss"]["stop_price"]
                      and expected.get("limit_price") == intent["payload"]["stop_loss"].get("limit_price")):
                    kind = "stop_loss"
                else:
                    raise SharedAccountError("Bracket protection type/price does not match its exact intent")
                if kind in found or _identifier(leg.get("id")) == oid:
                    raise SharedAccountError("Bracket protection identities are duplicated")
                found.add(kind)
                validated.append((leg, expected, kind))
            for index, (leg, expected, _) in enumerate(validated):
                sibling = validated[1 - index][0]
                sibling_fill = float(_decimal(sibling.get("filled_qty"), positive=False, integer=True))
                qty = float(expected["qty"])
                parent_qty = float(intent["identity"]["qty"])
                if qty != parent_qty and (sibling_fill <= 0 or qty != parent_qty - sibling_fill):
                    raise SharedAccountError("Bracket quantity change is not proved by the sibling's actual fills")
                self._store_order(value, leg, owner=intent["strategy"], root_cid=cid,
                                  expected=expected, parent_id=oid, allow_quantity_change=True)
        elif intent["identity"]["order_class"] == "oco":
            legs = raw.get("legs")
            if ((legs is None or legs == []) and value["orders"][oid]["status"] in TERMINAL
                    and float(value["orders"][oid]["filled_qty"]) == 0):
                known_legs = [item for item in value["orders"].values() if item.get("parent_id") == oid]
                if any(item["status"] not in TERMINAL for item in known_legs):
                    raise SharedAccountBlocked("Terminal OCO root still has an unconfirmed actual protective leg")
            else:
                if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
                    raise SharedAccountBlocked("OCO root must include its one actual stop-loss leg")
                leg = legs[0]
                expected = _identity(leg, strategy=intent["strategy"])
                base = intent["identity"]
                if (expected["symbol"] != base["symbol"] or expected["side"] != "sell"
                        or expected["time_in_force"] != base["time_in_force"]
                        or expected["type"] not in ("stop", "stop_limit")
                        or expected.get("stop_price") != intent["payload"]["stop_loss"]["stop_price"]
                        or expected.get("limit_price") != intent["payload"]["stop_loss"].get("limit_price")
                        or _identifier(leg.get("id")) == oid):
                    raise SharedAccountError("OCO stop leg does not match the exact registered protection")
                parent_fill = float(value["orders"][oid]["filled_qty"])
                qty, original_qty = float(expected["qty"]), float(base["qty"])
                if qty != original_qty and (parent_fill <= 0 or qty != original_qty - parent_fill):
                    raise SharedAccountError("OCO stop quantity change is not proved by actual take-profit fills")
                self._store_order(value, leg, owner=intent["strategy"], root_cid=cid,
                                  expected=expected, parent_id=oid, allow_quantity_change=True)
        intent["order_id"], intent["write_state"] = oid, "submitted"
        intent["write_attempted"] = True

    def register_order(self, raw: Mapping[str, Any]) -> None:
        with self.lock():
            value = self._load()
            self._register_order(value, raw, require_own=True)
            self._risk(value)
            self._save(value)

    def observe_order(self, raw: Mapping[str, Any]) -> bool:
        """Validate a known account order read by either strategy; reject strangers."""
        with self.lock():
            value = self._load()
            oid, cid = raw.get("id"), raw.get("client_order_id")
            if oid not in value["orders"] and cid not in value["intents"]:
                return False
            try:
                self._register_order(value, raw, require_own=False)
            except SharedAccountBlocked:
                if oid in value["orders"]:
                    value["orders"][oid]["observation_unknown"] = True
                if cid in value["intents"]:
                    value["intents"][cid]["write_state"] = "unknown"
                self._save(value)
                raise
            self._risk(value)
            self._save(value)
            return True

    def owns_order(self, raw: Mapping[str, Any], other_strategy: str | None = None) -> bool:
        owner = other_strategy or self.strategy
        if owner not in ASSETS:
            raise SharedAccountError("Requested order owner is invalid")
        with self.lock():
            value = self._load()
            item = value["orders"].get(raw.get("id"))
            if not item or item["strategy"] != owner:
                return False
            return (_identity(raw, strategy=owner) == item["identity"]
                    and item["account_sha256"] == self.account_sha256
                    and (raw.get("account_id") is None or raw["account_id"] == self.account_id))

    def update_state(self, own_positions: list[dict[str, Any]], own_pending: list[dict[str, Any]]) -> None:
        """Persist a strategy's confirmed ledger view; never infer fills from positions."""
        if not isinstance(own_positions, list) or not isinstance(own_pending, list):
            raise SharedAccountError("Shared strategy state requires positions/pending lists")
        with self.lock():
            value = self._load()
            positions, pending = [], []
            unconfirmed = False
            for raw in own_positions:
                if not isinstance(raw, dict):
                    raise SharedAccountError("Shared managed position is malformed")
                inventory_id = raw.get("opening_inventory_id")
                if inventory_id is not None:
                    inventory_id = _identifier(inventory_id)
                    inventory = value["opening_inventory"].get(inventory_id)
                    qty = float(_decimal(raw.get("qty"), positive=False, integer=True))
                    cost = float(_decimal(raw.get("cost_usd"), positive=False))
                    if (self.strategy != "stocks" or not inventory or raw.get("symbol") != inventory["symbol"]
                            or raw.get("entry_client_order_id") or raw.get("entry_order_id")):
                        raise SharedAccountError("Opening inventory state must retain its separate real inventory anchor")
                    remaining = self._inventory_remaining(value, inventory_id)
                    if qty != remaining:
                        raise SharedAccountBlocked("Opening inventory quantity awaits complete actual exit-fill reconciliation")
                    expected_cost = float(inventory["cost_usd"]) * qty / float(inventory["qty"])
                    if not math.isclose(cost, expected_cost, rel_tol=1e-8, abs_tol=0.01):
                        raise SharedAccountError("Opening inventory state must retain its real historical cost basis")
                    if type(raw.get("state_unconfirmed", False)) is not bool:
                        raise SharedAccountError("Shared inventory confirmation flag must be boolean")
                    unconfirmed = unconfirmed or raw.get("state_unconfirmed", False)
                    positions.append({"symbol": raw["symbol"], "qty": qty, "cost_usd": cost,
                                      "opening_inventory_id": inventory_id})
                    continue
                cid = _identifier(raw.get("entry_client_order_id"))
                oid = _identifier(raw.get("entry_order_id"))
                intent, order = value["intents"].get(cid), value["orders"].get(oid)
                qty = float(_decimal(raw.get("qty"), positive=False, integer=True))
                cost = float(_decimal(raw.get("cost_usd"), positive=False))
                if (not intent or not order or intent["strategy"] != self.strategy or order["strategy"] != self.strategy
                        or order["identity"]["side"] != "buy" or raw.get("symbol") != order["identity"]["symbol"]
                        or order["root_client_order_id"] != cid or qty > float(order["filled_qty"])
                        or (qty == 0 and cost != 0) or (qty > 0 and cost <= 0)):
                    raise SharedAccountError("Shared position lacks a confirmed account-bound managed purchase")
                multiplier = 100 if order["identity"]["asset_class"] == "us_option" else 1
                expected_cost = qty * float(order["filled_avg_price"] or 0) * multiplier
                if not math.isclose(cost, expected_cost, rel_tol=1e-8, abs_tol=0.01):
                    raise SharedAccountError("Shared position cost must come from the confirmed entry fill")
                if raw.get("state_unconfirmed", False) is not False:
                    if raw.get("state_unconfirmed") is not True:
                        raise SharedAccountError("Shared position confirmation flag must be boolean")
                    unconfirmed = True
                positions.append({"symbol": raw["symbol"], "qty": qty, "cost_usd": cost,
                                  "entry_client_order_id": cid, "entry_order_id": oid})
            for raw in own_pending:
                if not isinstance(raw, dict):
                    raise SharedAccountError("Shared pending state is malformed")
                cid = _identifier(raw.get("client_order_id"))
                intent = value["intents"].get(cid)
                if not intent or intent["strategy"] != self.strategy:
                    raise SharedAccountError("Shared pending state has no owned exact intent")
                reserve = float(_decimal(raw.get("reserved_usd", 0), positive=False))
                status = raw.get("status")
                if not isinstance(status, str) or len(status) > 64:
                    raise SharedAccountError("Shared pending status is invalid")
                pending.append({"client_order_id": cid, "status": status, "reserved_usd": reserve})
            value["states"][self.strategy] = {"positions": positions, "pending": pending,
                                              "unconfirmed": unconfirmed, "observed_at": _now()}
            self._save(value)
