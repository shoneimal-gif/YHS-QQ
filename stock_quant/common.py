"""Small shared configuration and persistence helpers. Never log credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing a linked state file")
    raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path: Path | str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or any("key" in name.lower() or "secret" in name.lower() for name in data):
        raise ValueError("Use private credentials, not strategy configuration")
    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10 or len(set(symbols)) != len(symbols):
        raise ValueError("A unique list of 1 to 10 stock symbols is required")
    if any(not isinstance(s, str) or not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", s) for s in symbols):
        raise ValueError("Only stock or ETF symbols are supported")
    for name in ("timeframe_minutes", "fast_sma", "slow_sma", "max_positions", "max_daily_entries", "poll_seconds", "max_quote_age_seconds"):
        if type(data.get(name)) is not int or data[name] <= 0:
            raise ValueError("Positive integer configuration is required: " + name)
    if data["timeframe_minutes"] != 15 or not 2 <= data["fast_sma"] < data["slow_sma"] <= 200:
        raise ValueError("This version uses completed 15-minute bars and ordered SMA periods")
    for name in ("strategy_budget_usd", "max_position_usd", "stop_loss_fraction", "take_profit_fraction", "commission_fraction", "spread_fraction", "max_spread_fraction"):
        value = data.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Finite positive configuration is required: " + name)
    if data["max_position_usd"] > data["strategy_budget_usd"] or data["max_positions"] > len(symbols):
        raise ValueError("Stock allocation exceeds the configured budget")
    if not 0 < data["stop_loss_fraction"] < 0.2 or not 0 < data["take_profit_fraction"] < 1:
        raise ValueError("Protective price offsets are outside supported bounds")
    if not 0 < data["max_spread_fraction"] <= 0.01 or not 10 <= data["max_quote_age_seconds"] <= 60:
        raise ValueError("Quote safety settings are outside supported bounds")
    if not 15 <= data["poll_seconds"] <= 300 or data.get("market_data_feed") != "iex":
        raise ValueError("Use explicit IEX data and a supported polling interval")
    if type(data.get("research_passed")) is not bool:
        raise ValueError("An explicit research gate is required")
    if type(data.get("paper_experiment_authorized", False)) is not bool:
        raise ValueError("Paper experiment authorization must be explicit")
    kind = data.get("strategy_kind", "sma_cross")
    if kind not in {"sma_cross", "daily_breakout"}:
        raise ValueError("Unsupported stock strategy")
    if kind == "daily_breakout":
        for name in ("entry_days", "exit_days", "trend_days"):
            if type(data.get(name)) is not int or not 2 <= data[name] <= 100:
                raise ValueError("Daily indicator history is outside supported bounds")
        if data["exit_days"] >= data["entry_days"]:
            raise ValueError("The exit channel must be shorter than the entry channel")
    if type(data.get("history_lookback_days", 120)) is not int or not 90 <= data.get("history_lookback_days", 120) <= 365:
        raise ValueError("Use a bounded history sufficient for daily indicators")
    adopted = data.get("authorized_adoption_symbols", [])
    if (not isinstance(adopted, list) or len(set(adopted)) != len(adopted)
            or any(s not in symbols for s in adopted)):
        raise ValueError("Only explicitly authorized strategy symbols can be adopted")
    expected = data.get("expected_adoption_quantities", {})
    if (not isinstance(expected, dict) or any(s not in adopted for s in expected)
            or any(type(q) is not int or q <= 0 for q in expected.values())):
        raise ValueError("Adoption quantities must match the explicitly verified holdings")
    blocked = data.get("blocked_account_sha256")
    if not isinstance(blocked, list) or any(not re.fullmatch(r"[a-f0-9]{64}", str(s)) for s in blocked):
        raise ValueError("Blocked account fingerprints must be SHA-256 values")
    allowed = data.get("allowed_account_sha256", [])
    if not isinstance(allowed, list) or any(not re.fullmatch(r"[a-f0-9]{64}", str(s)) for s in allowed):
        raise ValueError("Allowed account fingerprints must be SHA-256 values")
    if type(data.get("shared_account", False)) is not bool:
        raise ValueError("Shared-account mode must be explicit")
    return data


def load_credentials() -> dict[str, str]:
    values = {}
    private_path = os.environ.get("STOCK_QUANT_CREDENTIAL_FILE")
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if private_path or credential_dir:
        path = Path(private_path) if private_path else Path(credential_dir) / "api"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Credentials must be a private regular file")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Credential file exceeds supported size")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Credentials must be an object")
        values = parsed
    result = {}
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "YHS_DISCORD_WEBHOOK"):
        value = values.get(name, os.environ.get(name))
        if value is None and name == "YHS_DISCORD_WEBHOOK":
            continue
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Required private credentials are missing or invalid")
        result[name] = value.strip()
    return result


def fixed_error(exc: BaseException) -> dict:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if type(status) is not int and response is not None:
        status = getattr(response, "status_code", None)
    if type(status) is not int:
        status = None
    kind = type(exc).__name__
    category = ("authentication" if status in (401, 403) else "rate_limit" if status == 429
                else "server_error" if status is not None and status >= 500
                else "timeout" if "Timeout" in kind else "network_error" if kind in ("ConnectionError", "OSError")
                else "validation" if isinstance(exc, (ValueError, TypeError, KeyError)) else "request_error")
    return {"category": category, "http_status": status, "recoverable": category not in ("authentication", "validation")}
