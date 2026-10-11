"""系统配置与美东交易日时钟；仅使用 Python 标准库。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, tzinfo
from pathlib import Path
from typing import Any


class USEastern(tzinfo):
    """2007 年起美国 DST 规则，避免 Windows 无 IANA tzdb 时需要 pip。"""

    @staticmethod
    def _bounds(year: int) -> tuple[datetime, datetime]:
        march = datetime(year, 3, 8, 2)
        november = datetime(year, 11, 1, 2)
        return (march + timedelta(days=(6 - march.weekday()) % 7),
                november + timedelta(days=(6 - november.weekday()) % 7))

    def dst(self, value: datetime | None) -> timedelta:
        if value is None:
            return timedelta(0)
        start, end = self._bounds(value.year)
        wall = value.replace(tzinfo=None)
        hour = timedelta(hours=1)
        if start + hour <= wall < end - hour:
            return hour
        if end - hour <= wall < end:
            return timedelta(0) if value.fold else hour
        if start <= wall < start + hour:
            return hour if value.fold else timedelta(0)
        return timedelta(0)

    def utcoffset(self, value: datetime | None) -> timedelta:
        return timedelta(hours=-5) + self.dst(value)

    def tzname(self, value: datetime | None) -> str:
        return "EDT" if self.dst(value) else "EST"

    def fromutc(self, value: datetime) -> datetime:
        start, end = self._bounds(value.year)
        start = start.replace(tzinfo=self)
        end = end.replace(tzinfo=self)
        standard = value - timedelta(hours=5)
        daylight = value - timedelta(hours=4)
        if end <= daylight < end + timedelta(hours=1):
            return standard.replace(fold=1)
        if standard < start or daylight >= end:
            return standard
        return daylight


def eastern_timezone() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except (ImportError, KeyError):
        return USEastern()


@dataclass
class SystemConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("YHS_API_KEY", ""), repr=False)
    api_base_url: str = "https://api.yehangshe.com/v1"
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    discover_path: str = "/discover"
    health_path: str = "/health"
    volume_path: str = "/market/options-volume-rank"
    heatmap_path: str = "/derived/heatmap/{ticker}/snapshot"
    flow_path: str = "/flow/trades"
    chain_path: str = "/options/chain-snapshot/{ticker}"
    request_timeout_seconds: float = 20.0
    max_rpm: int = 50
    min_request_interval_seconds: float = 1.2
    max_concurrency: int = 3
    monthly_quota: int = 100_000
    quota_reserve: int = 5_000
    initial_backoff_seconds: float = 1.2
    max_backoff_seconds: float = 60.0
    max_error_retries: int = 4
    user_agent: str = "YHS-Options-Paper/1.0"
    output_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "runtime")
    quota_state_path: Path | None = None
    max_pool_size: int = 60
    volume_top_limit: int = 100
    min_spot_usd: float = 20.0
    min_volume_oi_ratio: float = 0.65
    min_oi_change_pct: float = 5.0
    bullish_pcr: float = 0.60
    bearish_pcr: float = 1.60
    etf_whitelist: tuple[str, ...] = ("SPY", "QQQ", "TLT", "SOXL", "TQQQ")
    excluded_indexes: tuple[str, ...] = ("SPX", "SPXW", "NDX", "RUT", "VIX", "DJX", "XSP", "OEX")
    symbol_types: dict[str, str] = field(default_factory=dict)
    morning_interval_minutes: int = 5
    midday_interval_minutes: int = 12
    afternoon_interval_minutes: int = 5
    market_open: str = "09:30"
    market_close: str = "16:00"
    session_overrides: dict[str, str | None] = field(default_factory=dict)
    max_node_distance_pct: float = 2.5
    node_touch_band_pct: float = 0.35
    node_reset_distance_pct: float = 0.75
    node_fatigue_limit: int = 3
    max_expiry_days: int = 10
    min_flow_premium_usd: float = 50_000.0
    flow_lookback_minutes: int = 15
    signal_cooldown_minutes: int = 30
    max_snapshot_age_seconds: int = 900
    max_quote_age_seconds: int = 900
    daily_flow_request_cap: int = 60
    daily_chain_request_cap: int = 20
    initial_cash: float = 100_000.0
    contracts_per_trade: int = 1
    max_open_positions: int = 5
    max_trade_cost_usd: float = 5_000.0
    max_total_premium_usd: float = 20_000.0
    slippage_pct: float = 0.02
    default_delta: float = 0.45
    default_gamma: float = 0.04
    hard_stop_roi: float = -0.35
    breakeven_trigger_roi: float = 0.35
    trailing_trigger_roi: float = 0.60
    trailing_drawdown: float = 0.20
    trailing_mode: str = "premium"
    hard_take_profit_roi: float = 1.50

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.quota_state_path = (Path(self.quota_state_path) if self.quota_state_path
                                 else self.output_dir / "quota_state.json")
        self.etf_whitelist = tuple(self.etf_whitelist)
        self.excluded_indexes = tuple(self.excluded_indexes)
        if not 1 <= self.max_rpm <= 50 or self.min_request_interval_seconds < 1.2:
            raise ValueError("限频不得超过50 RPM，请求间隔不得低于1.2秒")
        if not 1 <= self.max_concurrency <= 3:
            raise ValueError("最大并发必须在1–3之间")
        if not 0 < self.monthly_quota <= 100_000 or not 0 <= self.quota_reserve < self.monthly_quota:
            raise ValueError("月额度/预留额度配置错误")
        if not 1 <= self.volume_top_limit <= 100 or not 1 <= self.max_pool_size <= 100:
            raise ValueError("标的池/初筛数量配置错误")
        if self.trailing_mode not in ("premium", "roi"):
            raise ValueError("trailing_mode只允许premium或roi")
        if not 0 <= self.slippage_pct < 1 or not 0 < self.trailing_drawdown < 1:
            raise ValueError("滑点/回撤比例配置错误")
        if not self.hard_stop_roi < 0 < self.breakeven_trigger_roi < self.trailing_trigger_roi < self.hard_take_profit_roi:
            raise ValueError("风控阈值顺序错误")
        if self.initial_cash <= 0 or self.contracts_per_trade <= 0:
            raise ValueError("模拟资金及合约数量必须为正")
        if min(self.morning_interval_minutes, self.midday_interval_minutes, self.afternoon_interval_minutes) < 1:
            raise ValueError("轮询周期必须为正")
        for close_time in (self.market_open, self.market_close):
            dt_time.fromisoformat(close_time)
        if dt_time.fromisoformat(self.market_open) >= dt_time.fromisoformat(self.market_close):
            raise ValueError("收盘时间必须晚于开盘时间")
        for day, close_time in self.session_overrides.items():
            date.fromisoformat(day)
            if close_time is not None and dt_time.fromisoformat(close_time) <= dt_time.fromisoformat(self.market_open):
                raise ValueError("半日市收盘时间必须晚于开盘时间")

    def eastern_timezone(self) -> tzinfo:
        return eastern_timezone()

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        key = day.isoformat()
        if key in self.session_overrides:
            close = self.session_overrides[key]
            if close is None:
                return None
        elif day.weekday() >= 5:
            return None
        else:
            close = self.market_close
        tz = eastern_timezone()
        return (datetime.combine(day, dt_time.fromisoformat(self.market_open), tz),
                datetime.combine(day, dt_time.fromisoformat(close), tz))

    def polling_minutes(self, now: datetime) -> int:
        wall = now.astimezone(eastern_timezone()).time().replace(tzinfo=None)
        if wall < dt_time(11, 30):
            return self.morning_interval_minutes
        if wall < dt_time(14, 30):
            return self.midday_interval_minutes
        return self.afternoon_interval_minutes

    def polling_slots(self, day: date) -> list[datetime]:
        bounds = self.session_bounds(day)
        if bounds is None:
            return []
        start, close = bounds
        slots: list[datetime] = []
        for begin, end, interval in ((start, start.replace(hour=11, minute=30), self.morning_interval_minutes),
                                     (start.replace(hour=11, minute=30), start.replace(hour=14, minute=30), self.midday_interval_minutes),
                                     (start.replace(hour=14, minute=30), close, self.afternoon_interval_minutes)):
            cursor = max(start, begin)
            while cursor < min(close, end):
                slots.append(cursor)
                cursor += timedelta(minutes=interval)
        return sorted(set(slots))

    def daily_budget(self, day: date) -> int:
        return 1 + self.max_pool_size * len(self.polling_slots(day)) + self.daily_flow_request_cap + self.daily_chain_request_cap


def load_config(path: str | Path | None = None, output_dir: str | Path | None = None) -> SystemConfig:
    values: dict[str, Any] = {}
    if path:
        values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise ValueError("配置JSON必须是对象")
        if "api_key" in values:
            raise ValueError("请用YHS_API_KEY环境变量提供密钥，不要写入配置文件")
    if output_dir:
        values["output_dir"] = Path(output_dir)
    return SystemConfig(**values)
