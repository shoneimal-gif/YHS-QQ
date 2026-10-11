"""标的池、净 GEX 结构和按需 Flow 确认。未知必需字段拒绝信号。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import SystemConfig, eastern_timezone

LOGGER = logging.getLogger(__name__)


class DataContractError(ValueError):
    """数据缺少安全决策所需字段。"""


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime.fromtimestamp(value / 1000 if abs(value) > 1e11 else value, timezone.utc)
    else:
        raise DataContractError("缺少时间戳")
    if parsed.tzinfo is None:
        raise DataContractError("时间戳必须带时区；不根据本机时区猜测")
    return parsed


def unwrap(payload: Any) -> Any:
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def rows_from(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    value = unwrap(payload)
    if isinstance(value, dict):
        value = pick(value, *keys)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise DataContractError(f"响应必须包含行数组；支持字段{keys}")
    return value


@dataclass
class Candidate:
    ticker: str
    spot: float
    total_volume: float
    total_oi: float
    oi_change_pct: float | None
    pcr: float | None
    bias: str
    asset_type: str
    score: float


@dataclass
class HeatmapSnapshot:
    ticker: str
    spot: float
    timestamp: datetime
    expiration: str
    positive_strike: float
    negative_strike: float
    positive_gex: float
    negative_gex: float


@dataclass
class Signal:
    ticker: str
    action: str
    spot: float
    target_strike: float
    positive_strike: float
    negative_strike: float
    expiration: str
    detected_at: datetime
    flow_contract: str | None
    signal_id: str
    structure: str = ""
    flow_premium_usd: float = 0.0
    data_timestamp: datetime | None = None


def snapshot_observation(payload: Any, ticker: str, now: datetime, config: SystemConfig) -> tuple[dict[str, Any], float, datetime]:
    data = unwrap(payload)
    if not isinstance(data, dict):
        raise DataContractError("Heatmap data应为对象")
    if str(data.get("ticker", "")).upper() != ticker:
        raise DataContractError("Heatmap标的不一致")
    spot = number(data.get("spot_usd"))
    if spot is None or spot <= 0:
        raise DataContractError("Heatmap spot_usd缺失或非正")
    timestamp = parse_timestamp(pick(data, "generated_at", "timestamp", "minute_at"))
    # 官方generated_at为段起点，现货来自该段第4分钟，段结束后才可见。
    simulated = isinstance(payload, dict) and payload.get("simulated") is True
    if not simulated:
        if now < timestamp + timedelta(minutes=5):
            raise DataContractError("Heatmap采样段尚未完成，避免前视数据")
        timestamp += timedelta(minutes=4)
    age = (now - timestamp).total_seconds()
    if age < -5 or age > config.max_snapshot_age_seconds:
        raise DataContractError(f"Heatmap时间过期或位于未来：age={age:.0f}s")
    session = data.get("session_date_et")
    today = now.astimezone(eastern_timezone()).date()
    if (session and session != today.isoformat()) or timestamp.astimezone(eastern_timezone()).date() != today:
        raise DataContractError("Heatmap不属于当前美东交易日")
    return data, spot, timestamp


def parse_heatmap(payload: Any, ticker: str, now: datetime, config: SystemConfig) -> HeatmapSnapshot:
    data, spot, timestamp = snapshot_observation(payload, ticker, now, config)
    today = now.astimezone(eastern_timezone()).date()
    cells = data.get("cells")
    if not isinstance(cells, list):
        raise DataContractError("官方Heatmap必须含cells数组")
    groups: dict[str, list[tuple[float, float]]] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        expiry = str(cell.get("expiration", ""))
        try:
            days = (date.fromisoformat(expiry) - today).days
        except ValueError:
            continue
        strike = number(cell.get("strike_usd"))
        gex = number(cell.get("net_dealer_gex_usd"))
        if 0 <= days <= config.max_expiry_days and strike and strike > 0 and gex is not None and gex != 0:
            groups.setdefault(expiry, []).append((strike, gex))
    choices = [(expiry, nodes) for expiry, nodes in groups.items()
               if any(gex > 0 for _, gex in nodes) and any(gex < 0 for _, gex in nodes)]
    if not choices:
        raise DataContractError("近周到期日缺少同一到期日的正/负净GEX节点")
    expiry, nodes = min(choices, key=lambda item: (-sum(abs(gex) for _, gex in item[1]), item[0]))
    positive = min((node for node in nodes if node[1] > 0), key=lambda node: (-node[1], node[0]))
    negative = min((node for node in nodes if node[1] < 0), key=lambda node: (node[1], node[0]))
    return HeatmapSnapshot(ticker, spot, timestamp, expiry, positive[0], negative[0], positive[1], negative[1])


class SignalDetector:
    def __init__(self, config: SystemConfig, state_path: Path | None = None):
        self.config = config
        self.state_path = Path(state_path) if state_path else None
        self.touches: dict[str, dict[str, Any]] = {}
        self.confirmed: dict[str, str] = {}
        if self.state_path and self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("touches"), dict) or not isinstance(data.get("confirmed"), dict):
                raise DataContractError("节点状态损坏，请保留文件并检查")
            self.touches, self.confirmed = data["touches"], data["confirmed"]

    def save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "touches": self.touches, "confirmed": self.confirmed}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)

    def filter_pool(self, payload: Any) -> list[Candidate]:
        raw_rows = rows_from(payload, ("items", "rows", "rankings", "tickers", "results", "stocks"))
        # 先取成交量Top100，再做清洗，不用第101名补齐被剔除者。
        ordered = sorted(raw_rows, key=lambda row: number(pick(row, "total_volume", "option_volume", "options_volume", "volume")) or 0, reverse=True)
        result: list[Candidate] = []
        reasons: dict[str, int] = {}

        def reject(reason: str) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1

        seen: set[str] = set()
        for row in ordered[:self.config.volume_top_limit]:
            ticker = str(pick(row, "ticker", "symbol") or "").upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", ticker) or ticker in seen:
                reject("代码无效/重复")
                continue
            seen.add(ticker)
            if ticker in self.config.excluded_indexes or ticker.startswith("^"):
                reject("指数")
                continue
            kind = str(pick(row, "asset_type", "security_type", "instrument_type") or self.config.symbol_types.get(ticker, "")).lower()
            if row.get("is_etf") is True:
                kind = "etf"
            elif row.get("is_etf") is False and not kind:
                kind = "stock"
            if ticker in self.config.etf_whitelist:
                kind = "etf"
            if kind in ("index", "indices", "mutual_fund") or (kind in ("etf", "etn", "fund") and ticker not in self.config.etf_whitelist):
                reject("非白名单基金/指数")
                continue
            if kind not in ("stock", "equity", "common_stock", "etf"):
                reject("资产类型缺失；需配置symbol_types")
                continue
            spot = number(pick(row, "spot_usd", "stock_price_usd", "underlying_price_usd", "price_usd", "stock_price", "price"))
            if spot is None or spot < self.config.min_spot_usd:
                reject("现货缺失/低于20美元")
                continue
            volume = number(pick(row, "total_volume", "option_volume", "options_volume", "volume"))
            oi = number(pick(row, "total_oi", "open_interest", "total_open_interest", "oi"))
            change = number(pick(row, "oi_change_pct", "open_interest_change_pct"))
            # YHS所有*_pct为fraction，内部Candidate显示为百分点。
            change_pct = change * 100 if change is not None else None
            if change_pct is None:
                previous = number(pick(row, "previous_oi", "prior_open_interest"))
                if previous is not None and previous > 0 and oi is not None:
                    change_pct = (oi / previous - 1) * 100
            ratio = volume / oi if volume is not None and oi is not None and oi > 0 else None
            if volume is None or volume < 0 or oi is None or oi < 0 or not (
                (ratio is not None and ratio >= self.config.min_volume_oi_ratio) or
                (change_pct is not None and change_pct >= self.config.min_oi_change_pct)):
                reject("量能不足/字段缺失")
                continue
            pcr = number(pick(row, "pcr", "put_call_ratio", "put_call_volume_ratio"))
            if pcr is None:
                calls, puts = number(row.get("call_volume")), number(row.get("put_volume"))
                if calls is not None and calls > 0 and puts is not None and puts >= 0:
                    pcr = puts / calls
            bias = "BULL" if pcr is not None and 0 <= pcr <= self.config.bullish_pcr else "BEAR" if pcr is not None and pcr >= self.config.bearish_pcr else "NEUTRAL"
            score = min(ratio or 0, 10) + min(max(change_pct or 0, 0), 100) / 100 + (1 if bias != "NEUTRAL" else 0)
            result.append(Candidate(ticker, spot, volume, oi, change_pct, pcr, bias, kind, score))
        result.sort(key=lambda candidate: (-candidate.score, -candidate.total_volume, candidate.ticker))
        result = result[:self.config.max_pool_size]
        LOGGER.info("每日初筛：Top%d原始%d行，保留%d只，剔除统计=%s", self.config.volume_top_limit, len(raw_rows), len(result), reasons)
        if not result:
            LOGGER.warning("标的池为空：请核对资产类型、现货和榜单字段；系统不猜测ETF身份或补造现货")
        return result

    def _update_fatigue(self, snap: HeatmapSnapshot) -> int:
        day = snap.timestamp.astimezone(eastern_timezone()).date().isoformat()
        key = f"{day}|{snap.ticker}|{snap.expiration}|{snap.positive_strike:.3f}"
        state = self.touches.setdefault(key, {"count": 0, "in_band": False, "timestamp": ""})
        stamp = snap.timestamp.isoformat()
        if state["timestamp"] and parse_timestamp(state["timestamp"]) >= snap.timestamp:
            return int(state["count"])
        distance = abs(snap.spot / snap.positive_strike - 1) * 100
        if distance <= self.config.node_touch_band_pct and not state["in_band"]:
            state["count"] += 1
            state["in_band"] = True
        elif distance >= self.config.node_reset_distance_pct:
            state["in_band"] = False
        state["timestamp"] = stamp
        # 有界保存，旧交易日保留7天用于恢复诊断。
        cutoff = (snap.timestamp.date() - timedelta(days=7)).isoformat()
        self.touches = {k: v for k, v in self.touches.items() if k.split("|", 1)[0] >= cutoff}
        self.save()
        return int(state["count"])

    def inspect_structure(self, candidate: Candidate, payload: Any, now: datetime) -> Signal | None:
        snap = parse_heatmap(payload, candidate.ticker, now, self.config)
        touches = self._update_fatigue(snap)
        if touches > self.config.node_fatigue_limit:
            LOGGER.info("%s 正节点%.2f已独立回踩%d次，疲劳过滤", snap.ticker, snap.positive_strike, touches)
            return None
        distance = abs(snap.spot / snap.positive_strike - 1) * 100
        if distance > self.config.max_node_distance_pct:
            return None
        if snap.positive_strike < snap.negative_strike and snap.spot >= snap.positive_strike:
            action, structure = "BUY_CALL", "SPRINGBOARD"
        elif snap.positive_strike > snap.negative_strike and snap.spot <= snap.positive_strike:
            action, structure = "BUY_PUT", "CLIFF"
        else:
            return None
        if (action == "BUY_CALL" and candidate.bias == "BEAR") or (action == "BUY_PUT" and candidate.bias == "BULL"):
            return None
        identity = f"{now.astimezone(eastern_timezone()).date()}|{snap.ticker}|{action}|{snap.expiration}|{snap.positive_strike:.3f}|{snap.negative_strike:.3f}"
        signal_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        last = self.confirmed.get(f"{snap.ticker}|{action}")
        if last and (now - parse_timestamp(last)).total_seconds() < self.config.signal_cooldown_minutes * 60:
            return None
        return Signal(snap.ticker, action, snap.spot, snap.negative_strike, snap.positive_strike,
                      snap.negative_strike, snap.expiration, now, None, signal_id, structure, data_timestamp=snap.timestamp)

    def evaluate_full_signal(self, candidate: Candidate, heatmap: Any, flow: Any, now: datetime,
                             structure: Signal | None = None) -> Signal | None:
        signal = structure or self.inspect_structure(candidate, heatmap, now)
        if signal is None:
            return None
        required_right = "C" if signal.action == "BUY_CALL" else "P"
        trades = rows_from(flow, ("trades", "items", "rows", "results"))
        for row in trades:
            try:
                stamp = parse_timestamp(pick(row, "timestamp", "occurred_at", "executed_at", "time"))
            except (ValueError, TypeError, OverflowError):
                continue
            age = (now - stamp).total_seconds()
            if not 0 <= age <= self.config.flow_lookback_minutes * 60:
                continue
            if str(pick(row, "ticker", "underlying_ticker", "symbol") or "").upper() != signal.ticker:
                continue
            side = str(pick(row, "side", "trade_side") or "").upper()
            right = str(pick(row, "right", "option_type", "type") or "").upper()
            right = {"CALL": "C", "PUT": "P"}.get(right, right)
            if side not in ("ASK", "AT_ASK", "ABOVE_ASK") or right != required_right:
                continue
            premium = number(pick(row, "premium_usd", "premium"))
            # 不用大单张数contracts冒充合约全天成交量。
            volume = number(pick(row, "volume", "day_volume", "contract_volume", "daily_volume"))
            oi = number(pick(row, "open_interest", "oi"))
            expiration = str(pick(row, "expiration", "expiration_date") or "")
            contract = str(pick(row, "contract_symbol", "occ_symbol") or "")
            if premium is None or premium < self.config.min_flow_premium_usd or volume is None or oi is None or not volume > 0 or oi < 0 or volume < oi:
                continue
            if expiration != signal.expiration or not contract:
                continue
            signal.flow_contract, signal.flow_premium_usd = contract, premium
            LOGGER.info("%s %s确认：正GEX=%.2f，负GEX=%.2f，ASK资金=$%.0f，合约=%s", signal.ticker, signal.structure, signal.positive_strike, signal.negative_strike, premium, contract)
            return signal
        return None

    def commit_signal(self, signal: Signal) -> None:
        self.confirmed[f"{signal.ticker}|{signal.action}"] = signal.detected_at.isoformat()
        self.save()


def evaluate_full_signal(candidate: Candidate, heatmap: Any, flow: Any, now: datetime,
                         config: SystemConfig, detector: SignalDetector | None = None) -> Signal | None:
    return (detector or SignalDetector(config)).evaluate_full_signal(candidate, heatmap, flow, now)


def select_entry_quote(payload: Any, signal: Signal, now: datetime, config: SystemConfig, *, offline: bool = False) -> dict[str, Any] | None:
    """单页链中仅选择Flow确认的同一真实合约；缺失/分页未覆盖时跳过，不额外追链。"""
    contracts = rows_from(payload, ("contracts", "items", "rows", "results"))
    expected = str(signal.flow_contract or "").removeprefix("O:").replace(" ", "")
    for row in contracts:
        if not offline and (row.get("simulated") is True or (isinstance(payload, dict) and payload.get("simulated") is True)):
            LOGGER.warning("实时模式拒绝模拟报价")
            continue
        symbol = str(pick(row, "contract_symbol", "symbol", "occ_symbol") or "")
        if symbol.removeprefix("O:").replace(" ", "") != expected:
            continue
        stamp_value = pick(row, "quoted_at", "timestamp", "as_of")
        try:
            stamp = parse_timestamp(stamp_value)
        except (ValueError, TypeError, OverflowError):
            LOGGER.warning("%s 合约报价时间缺失，拒绝开仓", signal.ticker)
            return None
        if not 0 <= (now - stamp).total_seconds() <= config.max_quote_age_seconds:
            LOGGER.warning("%s 合约报价过期/未来，拒绝开仓", signal.ticker)
            return None
        bid = number(pick(row, "bid", "bid_usd", "bid_price_usd"))
        ask = number(pick(row, "ask", "ask_usd", "ask_price_usd"))
        if bid is None or ask is None or not 0 < bid <= ask:
            return None
        right = str(pick(row, "right", "option_type") or "").upper()
        right = {"CALL": "C", "PUT": "P"}.get(right, right)
        if right != ("C" if signal.action == "BUY_CALL" else "P") or str(pick(row, "expiration", "expiration_date")) != signal.expiration:
            continue
        return {"contract_symbol": symbol, "strike": pick(row, "strike", "strike_usd"),
                "expiration": pick(row, "expiration", "expiration_date"), "right": right,
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "delta": pick(row, "delta"), "gamma": pick(row, "gamma"),
                "timestamp": stamp.isoformat(), "simulated": bool(row.get("simulated", False))}
    LOGGER.info("%s 当前单页链中无已确认合约；不翻页、不补造合约", signal.ticker)
    return None
