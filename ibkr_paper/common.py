"""Configuration and local locking for IBKR simulated trading only."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re

from ibkr_bridge.common import atomic_json, read_json, now, timestamp, ROOT


CONFIG = ROOT / ".private_config" / "ibkr_paper.json"
RUNTIME = ROOT / "runtime" / "ibkr_paper"
SHA = re.compile(r"[a-f0-9]{64}\Z")


class PaperError(ValueError):
    def __init__(self, code: str):
        self.code = code
        self.category = code
        super().__init__(code)


def _number(mapping, key, low, high, *, integer=False):
    value = mapping.get(key)
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise PaperError("paper_configuration_invalid")


def load_config(path: Path) -> dict:
    data = read_json(path, maximum=32768)
    if (set(data) != {"schema_version", "mode", "paper_only", "execute_paper", "paper_login_confirmed",
                      "ibkr", "source", "strategy", "notifications"}
            or type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("mode") != "ibkr_paper" or data.get("paper_only") is not True
            or type(data.get("execute_paper")) is not bool
            or type(data.get("paper_login_confirmed")) is not bool):
        raise PaperError("paper_only_configuration_required")
    if any(not isinstance(data.get(key), dict) for key in ("ibkr", "source", "strategy", "notifications")):
        raise PaperError("paper_configuration_invalid")
    ib, source, strategy, notices = (data[key] for key in ("ibkr", "source", "strategy", "notifications"))
    if (set(ib) != {"host", "port", "client_id", "request_timeout_seconds", "quote_timeout_seconds",
                    "max_quote_age_seconds", "account_sha256"}
            or ib.get("host") != "127.0.0.1" or type(ib.get("port")) is not int
            or ib["port"] not in {4002, 7497}
            or (ib.get("account_sha256") is not None and not SHA.fullmatch(str(ib["account_sha256"])))):
        raise PaperError("paper_endpoint_and_account_binding_required")
    _number(ib, "client_id", 1, 2**31 - 1, integer=True)
    _number(ib, "request_timeout_seconds", 1, 15)
    _number(ib, "quote_timeout_seconds", 1, 10)
    _number(ib, "max_quote_age_seconds", 0.25, 2)
    if (set(source) != {"kind", "ssh_host", "ssh_user", "ssh_port", "identity_file", "known_hosts_file"}
            or source.get("kind") != "ssh_live_readonly"):
        raise PaperError("paper_source_configuration_invalid")
    _number(source, "ssh_port", 1, 65535, integer=True)
    for key in ("ssh_host", "ssh_user", "identity_file", "known_hosts_file"):
        value = source.get(key)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 4096
                                  or any(ord(char) < 32 for char in value)):
            raise PaperError("paper_source_configuration_invalid")
    if set(strategy) != {"max_open_signal_age_seconds", "max_close_signal_age_seconds", "max_buy_premium_pct",
                         "target_debit_usd", "min_debit_usd", "max_debit_usd", "fee_reserve_usd",
                         "max_option_exposure_usd", "max_contracts_per_trade", "entry_cancel_after_seconds", "poll_seconds"}:
        raise PaperError("paper_strategy_configuration_invalid")
    for key, low, high in (("max_open_signal_age_seconds", 1, 30), ("max_close_signal_age_seconds", 1, 120),
                           ("max_buy_premium_pct", 0, 3), ("target_debit_usd", 3000, 5000),
                           ("min_debit_usd", 3000, 5000), ("max_debit_usd", 3000, 5000),
                           ("max_option_exposure_usd", 3000, 10000), ("fee_reserve_usd", 2, 100),
                           ("entry_cancel_after_seconds", 1, 30), ("poll_seconds", 1, 60)):
        _number(strategy, key, low, high)
    _number(strategy, "max_contracts_per_trade", 1, 50, integer=True)
    if not strategy["min_debit_usd"] <= strategy["target_debit_usd"] <= strategy["max_debit_usd"]:
        raise PaperError("paper_strategy_configuration_invalid")
    if strategy["max_buy_premium_pct"] != 3 or strategy["fee_reserve_usd"] != 2:
        raise PaperError("paper_price_and_fee_policy_mismatch")
    if (set(notices) != {"enabled", "use_existing_discord"}
            or type(notices.get("enabled")) is not bool or notices.get("use_existing_discord") is not True):
        raise PaperError("paper_notification_configuration_invalid")
    return data


def config_identity(config: dict) -> str:
    """Pin financial policy/source/client identity without storing secrets."""
    fields = {key: config[key] for key in ("schema_version", "mode", "paper_only", "ibkr", "source", "strategy")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def executor_lock():
    """Hold one local IBKR Paper runner through disconnect and notification close."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / "executor.lock"
    if path.is_symlink():
        raise PaperError("paper_linked_state_refused")
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
            raise PaperError("paper_executor_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
