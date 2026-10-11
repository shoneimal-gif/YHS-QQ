"""可重复的全日模拟行情；不请求网络，也不代表历史市场表现。

Heatmap 使用 YHS 文档的 data/cells 格式。榜单、Flow 和链字段是本项目
明确约定的兼容样本，不能用于声称未公开的 YHS 端点具有这些字段。
每个返回值都标注 simulated=True；合约标识是 OCC 格式的合成标识，
并未向交易所核实实际挂牌。09:30 到 15:55 共 78 个五分钟观察时刻，
16:00 清仓由入口执行，不能把清仓时刻算成第 79 次普通轮询。
"""
from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Iterator

from config import SystemConfig, eastern_timezone


LOGGER = logging.getLogger(__name__)
STOCKS = (
    "NVDA", "AAPL", "TSLA", "AMZN", "GOOGL", "META", "MSFT", "AMD",
    "AVGO", "GOOG", "NFLX", "CRM", "ORCL", "ADBE", "NOW", "INTU",
    "UBER", "ABNB", "SHOP", "COIN", "PLTR", "SNOW", "CRWD", "PANW",
    "DDOG", "NET", "ARM", "MU", "AMAT", "LRCX", "KLAC", "QCOM",
    "INTC", "TXN", "DELL", "SMCI", "JPM", "BAC", "GS", "MS", "V",
    "MA", "COST", "WMT", "HD", "LOW", "UNH", "LLY", "XOM", "CVX",
    "BA", "CAT",
)
CORE_ETFS = ("SPY", "QQQ", "TLT", "SOXL", "TQQQ")
OTHER_ETFS = (
    "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLB", "XLP",
    "XLU", "XLRE", "XLC", "XLY", "ARKK", "HYG", "LQD", "GLD", "SLV",
    "USO", "UNG", "EEM", "FXI", "KWEB", "SMH", "SOXX", "IYR", "VNQ",
    "SQQQ", "SPXS", "UVXY",
)
INDEXES = ("SPX", "SPXW", "NDX", "RUT", "VIX", "DJX", "XSP", "OEX")


def iter_replay_times(session_date: date, config: SystemConfig) -> Iterator[datetime]:
    """固定标准全日 78 步，使用美东 DST 时区，不受本机时区影响。"""
    if isinstance(session_date, datetime):
        session_date = session_date.date()
    if not isinstance(session_date, date):
        raise ValueError("回放日期必须是 datetime.date")
    tz = config.eastern_timezone() if hasattr(config, "eastern_timezone") else eastern_timezone()
    start = datetime.combine(session_date, dt_time(9, 30), tz)
    for step in range(78):
        yield start + timedelta(minutes=5 * step)


def replay_close_time(session_date: date, config: SystemConfig) -> datetime:
    """标准全日模拟收盘时刻；真行情的半日/休市由主入口配置处理。"""
    tz = config.eastern_timezone() if hasattr(config, "eastern_timezone") else eastern_timezone()
    return datetime.combine(session_date, dt_time(16), tz)


class MockYHSClient:
    """YHSClient 的离线、可复现替身；计数仅用于估算请求量。

    默认种子改变背景行情细微振幅，不改变五条核心风控用例：
    NVDA 硬顶止盈、AAPL 保本、TSLA PUT 追踪止盈、AMZN 硬止损、
    GOOGL 收盘清仓。META 反复接近节点但 Flow 不合格，覆盖疲劳过滤。
    不启用真实限频等待，不消耗任何真实 API 配额。
    """

    def __init__(self, config: SystemConfig, seed: int = 7):
        self.config = config
        self.seed = seed
        self.calls: dict[str, int] = {}
        self.success_count = 0
        self.quota_remaining = int(config.monthly_quota)
        self.rate_limit_remaining = int(config.max_rpm)
        self.simulated = True
        self.now = datetime(2026, 10, 9, 9, 30, tzinfo=eastern_timezone())
        self._random = random.Random(seed)
        self._phase = {ticker: self._random.uniform(0, math.tau) for ticker in STOCKS + CORE_ETFS}
        self._base = {ticker: float(80 + 7 * i) for i, ticker in enumerate(STOCKS)}
        self._base.update({"NVDA": 188.0, "AAPL": 230.0, "TSLA": 250.0,
                           "AMZN": 205.0, "GOOGL": 165.0, "META": 540.0,
                           "SPY": 600.0, "QQQ": 510.0, "TLT": 95.0,
                           "SOXL": 40.0, "TQQQ": 80.0})
        self.scenarios = {
            "NVDA": "CALL_HARD_TAKE_PROFIT", "AAPL": "CALL_BREAKEVEN",
            "TSLA": "PUT_TRAILING_STOP", "AMZN": "CALL_HARD_STOP",
            "GOOGL": "CALL_SESSION_CLOSE", "META": "NODE_FATIGUE_NO_VALID_FLOW",
        }

    @property
    def total_requests(self) -> int:
        return self.success_count

    def set_time(self, now: datetime) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("模拟时间必须是含时区的 datetime")
        self.now = now.astimezone(eastern_timezone())

    def _count(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1
        self.success_count += 1
        self.quota_remaining = max(0, int(self.config.monthly_quota) - self.success_count)

    def _step(self) -> int:
        start = self.now.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(0, int((self.now - start).total_seconds() // 300))

    def _expiration(self) -> date:
        day = self.now.date()
        days = (4 - day.weekday()) % 7
        return day + timedelta(days=days or 7)

    @staticmethod
    def _occ(ticker: str, expiration: date, right: str, strike: float) -> str:
        return f"O:{ticker}{expiration:%y%m%d}{right}{round(strike * 1000):08d}"

    def _spot(self, ticker: str) -> float:
        base = self._base.get(ticker, 100.0)
        step = self._step()
        routes = {
            "NVDA": (0.0, 1.0, 2.0, 4.0, 7.0, 7.2),
            "AAPL": (0.0, 1.2, 2.4, 1.5, 0.0, -0.2),
            "TSLA": (0.0, -1.0, -4.0, -2.2, -2.0),
            "AMZN": (0.0, -1.0, -2.3, -2.5),
        }
        if ticker in routes:
            route = routes[ticker]
            move = route[min(step, len(route) - 1)]
        elif ticker == "GOOGL":
            move = min(step, 8) * 0.06 + 0.02 * math.sin(step / 5)
        elif ticker == "META":
            move = base * (0.001 if step % 2 == 0 else 0.012)
        else:
            phase = self._phase.get(ticker, 0.0)
            move = base * 0.002 * (math.sin(step / 7 + phase) - math.sin(phase))
        return round(base + move, 4)

    def fetch_volume_top(self, limit: int = 100, **_: Any) -> dict[str, Any]:
        self._count("volume_top")
        rows: list[dict[str, Any]] = []
        universe = ([(ticker, "stock", self._base[ticker]) for ticker in STOCKS]
                    + [(ticker, "etf", self._base[ticker]) for ticker in CORE_ETFS]
                    + [(ticker, "etf", 50.0) for ticker in OTHER_ETFS]
                    + [(ticker, "index", 5000.0) for ticker in INDEXES]
                    + [("F", "stock", 7.5), ("SOFI", "stock", 15.25), ("PENN", "stock", 19.99)]
                    + [("UNKNOWN1", "unknown", 100.0), ("UNKNOWN2", "unknown", 100.0)])
        for rank, (ticker, kind, spot) in enumerate(universe, 1):
            total_volume = 500_000 - rank * 2_500
            rows.append({
                "rank": rank, "ticker": ticker, "asset_type": kind,
                "spot_usd": spot, "total_volume": total_volume,
                "total_oi": int(total_volume / 0.75), "oi_change_pct": 0.06,
                "pcr": 1.85 if ticker == "TSLA" else 0.55,
                "simulated": True,
            })
        return {"data": rows[:max(0, min(100, int(limit)))], "simulated": True,
                "generated_at": self.now.isoformat(), "session_date_et": self.now.date().isoformat()}

    def fetch_heatmap(self, ticker: str, **_: Any) -> dict[str, Any]:
        self._count("heatmap")
        ticker = ticker.upper()
        spot = self._spot(ticker)
        base = self._base.get(ticker, 100.0)
        first = self._expiration()
        expirations = (first, first + timedelta(days=7))
        if ticker == "META":
            positive, negative = base, base + 8.0
        elif self._step() == 0 and ticker in self.scenarios:
            if ticker == "TSLA":
                positive, negative = base + 1.0, base - 7.0
            else:
                positive, negative = base - 1.0, base + 7.0
        else:
            # 入场后关闭新的结构用例，持仓仍持续以 spot_usd 盯市。
            positive, negative = round(spot + 20.0, 2), round(spot + 25.0, 2)
        cells = []
        for week, expiration in enumerate(expirations):
            scale = 1.0 if week == 0 else 0.30
            cells.extend([
                {"expiration": expiration.isoformat(), "strike_usd": positive,
                 "net_dealer_gex_usd": round(9_000_000 * scale), "simulated": True},
                {"expiration": expiration.isoformat(), "strike_usd": negative,
                 "net_dealer_gex_usd": round(-12_000_000 * scale), "simulated": True},
                {"expiration": expiration.isoformat(), "strike_usd": positive - 5,
                 "net_dealer_gex_usd": round(250_000 * scale), "simulated": True},
            ])
        return {"data": {
            "ticker": ticker, "generated_at": self.now.isoformat(),
            "session_date_et": self.now.date().isoformat(), "spot_usd": spot,
            "expirations": [item.isoformat() for item in expirations], "cells": cells,
            "simulated": True, "scenario": self.scenarios.get(ticker, "NO_STRUCTURE"),
        }, "simulated": True}

    def fetch_flow(self, ticker: str, min_premium: float = 50_000, **_: Any) -> dict[str, Any]:
        self._count("flow")
        ticker = ticker.upper()
        base = self._base.get(ticker, 100.0)
        expiration = self._expiration()
        right = "P" if ticker == "TSLA" else "C"
        premium = 75_000.0
        trades: list[dict[str, Any]] = []
        if premium >= min_premium:
            stamp = self.now.replace(hour=9, minute=29, second=0, microsecond=0)
            trade = {
                "timestamp": stamp.isoformat(), "ticker": ticker,
                "side": "ASK" if ticker in {"NVDA", "AAPL", "TSLA", "AMZN", "GOOGL"} else "BID",
                "option_type": "PUT" if right == "P" else "CALL",
                "premium_usd": premium, "volume": 600, "open_interest": 500,
                "contract_symbol": self._occ(ticker, expiration, right, base),
                "strike": base, "expiration": expiration.isoformat(), "simulated": True,
            }
            trades.append(trade)
            # 第二条专门验证方向/主动买入/新鲜度筛选，永远不能作为准入。
            trades.append({**trade, "timestamp": (stamp - timedelta(minutes=30)).isoformat(),
                           "side": "BID", "volume": 10, "open_interest": 500})
        return {"data": {"trades": trades, "ticker": ticker,
                          "generated_at": self.now.isoformat(), "simulated": True}, "simulated": True}

    def fetch_option_chain(self, ticker: str, expiration: str | None = None, **_: Any) -> dict[str, Any]:
        self._count("option_chain")
        ticker = ticker.upper()
        expiry = date.fromisoformat(expiration) if expiration else self._expiration()
        base = self._base.get(ticker, 100.0)
        contracts = []
        for right in ("C", "P"):
            for offset in (-5.0, 0.0, 5.0):
                strike = base + offset
                contracts.append({
                    "contract_symbol": self._occ(ticker, expiry, right, strike),
                    "ticker": ticker, "strike": strike, "expiration": expiry.isoformat(),
                    "right": right, "bid": 2.45, "ask": 2.55, "mid": 2.50,
                    "delta": 0.45 if right == "C" else -0.45, "gamma": 0.04,
                    "timestamp": self.now.isoformat(), "simulated": True,
                    "valuation_source": "MOCK_SYNTHETIC_OCC",
                })
        return {"data": {"ticker": ticker, "contracts": contracts,
                          "generated_at": self.now.isoformat(), "simulated": True}, "simulated": True}

    def discover(self) -> dict[str, Any]:
        return {"simulated": True, "endpoints": []}

    def health(self) -> dict[str, Any]:
        return {"simulated": True, "status": "ok"}

    def summary(self) -> dict[str, Any]:
        return {"simulated": True, "requests": self.success_count,
                "calls": dict(self.calls), "quota_remaining": self.quota_remaining,
                "actual_api_quota_used": 0, "seed": self.seed}

    def close(self) -> None:
        """接口兼容真实客户端；离线替身没有需要释放的网络资源。"""
