"""Local configuration and state for the separate IBKR read-only monitor."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CONFIG = ROOT / ".private_config" / "ibkr_bridge.json"
RUNTIME = ROOT / "runtime" / "ibkr_readonly"
SHA = re.compile(r"[a-f0-9]{64}\Z")


class MonitorError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: object) -> datetime:
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise MonitorError("timestamp_unconfirmed") from None


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise MonitorError("linked_local_state")
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, *, maximum: int = 2_000_000) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            raise ValueError
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError):
        raise MonitorError("local_json_unavailable") from None


def _number(data: dict, key: str, minimum: float, maximum: float, *, integer=False) -> None:
    value = data.get(key)
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise MonitorError("configuration_invalid")


def load_config(path: Path) -> dict:
    data = read_json(path, maximum=32_768)
    if (set(data) != {"schema_version", "mode", "readonly", "ibkr", "source", "display", "notifications"}
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("mode") != "ibkr_live_read_only" or data.get("readonly") is not True):
        raise MonitorError("read_only_configuration_required")
    if any(not isinstance(data.get(k), dict) for k in ("ibkr", "source", "display", "notifications")):
        raise MonitorError("configuration_invalid")
    ib, source, display, notices = (data[k] for k in ("ibkr", "source", "display", "notifications"))
    if (set(ib) != {"host", "port", "client_id", "request_timeout_seconds", "quote_timeout_seconds",
                    "max_quote_age_seconds", "account_sha256"}
            or ib.get("host") != "127.0.0.1"
            or type(ib.get("port")) is not int or ib["port"] not in {7496, 4001}
            or (ib.get("account_sha256") is not None and not SHA.fullmatch(str(ib["account_sha256"])))):
        raise MonitorError("local_read_only_binding_required")
    _number(ib, "client_id", 1, 2_147_483_647, integer=True)
    _number(ib, "request_timeout_seconds", 1, 15)
    _number(ib, "quote_timeout_seconds", 1, 10)
    _number(ib, "max_quote_age_seconds", 0.25, 5)
    if (set(source) != {"kind", "ssh_host", "ssh_user", "ssh_port", "identity_file", "known_hosts_file"}
            or source.get("kind") != "ssh_live_readonly"):
        raise MonitorError("source_configuration_invalid")
    _number(source, "ssh_port", 1, 65535, integer=True)
    for key in ("ssh_host", "ssh_user", "identity_file", "known_hosts_file"):
        value = source.get(key)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 4096
                                  or any(ord(c) < 32 for c in value)):
            raise MonitorError("source_configuration_invalid")
    if (set(display) != {"max_open_signal_age_seconds", "max_close_signal_age_seconds", "max_buy_premium_pct",
                         "quantity", "poll_seconds", "limit"} or display.get("quantity") is not None):
        raise MonitorError("preview_has_no_quantity")
    _number(display, "max_open_signal_age_seconds", 1, 30)
    _number(display, "max_close_signal_age_seconds", 1, 120)
    _number(display, "max_buy_premium_pct", 0, 3)
    _number(display, "poll_seconds", 1, 60)
    _number(display, "limit", 1, 50, integer=True)
    if (set(notices) != {"enabled", "use_existing_discord"}
            or type(notices.get("enabled")) is not bool or notices.get("use_existing_discord") is not True):
        raise MonitorError("notification_configuration_invalid")
    return data


@contextmanager
def monitor_lock():
    """One local monitor/outbox, independent of all Paper trading locks."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / "monitor.lock"
    if path.is_symlink():
        raise MonitorError("linked_local_state")
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorError("monitor_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
