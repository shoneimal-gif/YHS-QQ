"""仅在本地账本模拟期权交易，不连接经纪商、不提交真实订单。

价格模型从入场报价中间价锚定：mid + delta*dS + gamma*dS**2/2。
delta、gamma 固定不变；模型未覆盖时间价值衰减、隐含波动率及价差变化。
入场支付 mid*(1+slippage)，退出收取 model_mid*(1-slippage)。
止损在离散观察时触发，记录观察到的模型价格，不保证在止损价成交。
trailing_mode='premium' 表示期权净清算价值从峰值回撤 20%；
trailing_mode='roi' 表示收益率从峰值下降 20 个百分点。
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import math
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import SystemConfig
    from detector import Signal


LOGGER = logging.getLogger(__name__)
MULTIPLIER = 100
STATE_VERSION = 1
CSV_FIELDS = [
    "position_id", "ticker", "action", "contract_symbol", "strike", "entry_time",
    "entry_spot", "entry_option_price", "exit_time", "exit_spot", "exit_option_price",
    "realized_pnl", "realized_roi_pct", "peak_roi_pct", "max_adverse_roi_pct",
    "direction_correct", "exit_reason", "expiration", "right", "quantity",
    "valuation_source", "stale", "signal_id", "target_strike", "delta", "gamma",
    "entry_mark", "last_spot", "last_mark", "last_time", "status", "stop_roi",
    "simulated_quote", "quote_timestamp_known", "entry_quote_time", "model_version",
]
_DATETIME_FIELDS = ("entry_time", "exit_time", "last_time", "entry_quote_time")
_OCC = re.compile(r"^(?:O:)?([A-Z0-9.]{1,6})\s*(\d{6})([CP])(\d{8})$")


class PaperStateError(RuntimeError):
    """纸面账本无法安全保存或恢复。"""


def _synchronized(method: Any) -> Any:
    """日终计时器与数据循环共享一个账本，串行提交短暂的本地变更。"""
    @wraps(method)
    def locked(self: PaperTrader, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


@dataclass
class Position:
    position_id: str
    ticker: str
    action: str
    contract_symbol: str
    strike: float
    entry_time: datetime
    entry_spot: float
    entry_option_price: float
    exit_time: datetime | None
    exit_spot: float | None
    exit_option_price: float | None
    realized_pnl: float | None
    realized_roi_pct: float | None
    peak_roi_pct: float
    max_adverse_roi_pct: float
    direction_correct: bool | None
    exit_reason: str | None
    expiration: str
    right: str
    quantity: int
    valuation_source: str
    stale: bool
    signal_id: str
    target_strike: float
    delta: float
    gamma: float
    entry_mark: float
    last_spot: float
    last_mark: float
    last_time: datetime
    status: str
    stop_roi: float
    simulated_quote: bool = False
    quote_timestamp_known: bool = False
    entry_quote_time: datetime | None = None
    model_version: str = "delta_gamma_v1"
    slippage_pct: float = 0.02

    @property
    def entry_cost(self) -> float:
        return self.entry_option_price * self.quantity * MULTIPLIER

    @property
    def liquidation_price(self) -> float:
        return max(0.0, self.last_mark) * (1.0 - self.slippage_pct)

    @property
    def roi_pct(self) -> float:
        return (self.liquidation_price / self.entry_option_price - 1.0) * 100.0

    @property
    def current_roi_pct(self) -> float:
        return self.roi_pct

    @property
    def entry_price(self) -> float:
        return self.entry_option_price

    @property
    def exit_price(self) -> float | None:
        return self.exit_option_price

    @property
    def pnl_usd(self) -> float | None:
        return self.realized_pnl


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 不能是布尔值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是有限数字") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} 必须是{'大于零的' if positive else ''}有限数字")
    return result


def _utc(value: datetime) -> datetime:
    # 混合时区的比较统一为 UTC；无时区输入按统一时间轴处理，不读取机器时区。
    if not isinstance(value, datetime):
        raise ValueError("时间必须是 datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _age(now: datetime, earlier: datetime) -> float:
    return (_utc(now) - _utc(earlier)).total_seconds()


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = _number(value, "报价时间")
        # 显式支持 Unix 秒与毫秒，不把缺失时间补成市场时间。
        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("报价时间必须是 ISO 8601、Unix 时间戳或 datetime")


def _json_position(position: Position) -> dict[str, Any]:
    payload = asdict(position)
    for name in _DATETIME_FIELDS:
        value = payload[name]
        payload[name] = value.isoformat() if value is not None else None
    return payload


def _position_from_json(payload: dict[str, Any]) -> Position:
    if not isinstance(payload, dict):
        raise ValueError("持仓记录必须是对象")
    allowed = {field.name for field in fields(Position)}
    data = {key: value for key, value in payload.items() if key in allowed}
    for name in _DATETIME_FIELDS:
        if data.get(name) is not None:
            data[name] = _parse_time(data[name])
    position = Position(**data)
    if position.status not in {"OPEN", "CLOSED"}:
        raise ValueError("持仓状态无效")
    if position.right not in {"C", "P"} or position.action not in {"BUY_CALL", "BUY_PUT"}:
        raise ValueError("持仓方向无效")
    if position.right != ("C" if position.action == "BUY_CALL" else "P"):
        raise ValueError("持仓动作与期权方向不一致")
    if isinstance(position.quantity, bool) or not isinstance(position.quantity, int) or position.quantity < 1:
        raise ValueError("持仓张数无效")
    for name in ("strike", "entry_spot", "entry_option_price", "entry_mark", "target_strike"):
        setattr(position, name, _number(getattr(position, name), name, positive=True))
    for name in ("delta", "gamma", "last_spot", "last_mark", "stop_roi", "peak_roi_pct", "max_adverse_roi_pct", "slippage_pct"):
        setattr(position, name, _number(getattr(position, name), name))
    if position.last_spot <= 0 or position.last_mark < 0 or position.gamma < 0 or not 0 <= position.slippage_pct < 1:
        raise ValueError("持仓模型参数无效")
    if not -1 <= position.delta <= 1 or (position.right == "C" and position.delta < 0) or (position.right == "P" and position.delta > 0):
        raise ValueError("持仓 delta 范围或符号无效")
    if position.peak_roi_pct < 0 or position.max_adverse_roi_pct > 0 or position.stop_roi < -1:
        raise ValueError("持仓收益率或止损阈值无效")
    if any(not isinstance(getattr(position, name), bool) for name in ("stale", "simulated_quote", "quote_timestamp_known")):
        raise ValueError("持仓数据质量标记必须是布尔值")
    if not position.position_id or not position.signal_id or not isinstance(position.entry_time, datetime) or not isinstance(position.last_time, datetime):
        raise ValueError("持仓标识或时间缺失")
    if _age(position.last_time, position.entry_time) < 0:
        raise ValueError("持仓观察时间早于开仓")
    if position.status == "CLOSED":
        if any(getattr(position, name) is None for name in ("exit_time", "exit_spot", "exit_option_price", "realized_pnl", "realized_roi_pct", "direction_correct", "exit_reason")):
            raise ValueError("已平仓记录缺少结果字段")
        for name in ("exit_spot", "exit_option_price", "realized_pnl", "realized_roi_pct"):
            setattr(position, name, _number(getattr(position, name), name))
        if _age(position.exit_time, position.entry_time) < 0 or position.exit_spot <= 0 or position.exit_option_price < 0:
            raise ValueError("已平仓记录的时间或价格无效")
        expected_pnl = (position.exit_option_price - position.entry_option_price) * position.quantity * MULTIPLIER
        expected_roi = (position.exit_option_price / position.entry_option_price - 1.0) * 100.0
        if not math.isclose(position.realized_pnl, expected_pnl, rel_tol=1e-9, abs_tol=0.01) or not math.isclose(position.realized_roi_pct, expected_roi, rel_tol=1e-9, abs_tol=0.0001):
            raise ValueError("已平仓记录的收益与入场、退出价格不一致")
        if not isinstance(position.direction_correct, bool):
            raise ValueError("方向正确标记必须是布尔值")
    elif position.exit_time is not None or position.realized_pnl is not None:
        raise ValueError("未平仓记录包含已平仓结果")
    return position


class PaperTrader:
    def __init__(self, config: SystemConfig, state_path: Path | None = None, *, model_version: str | None = None):
        self._lock = threading.RLock()
        self._entries_enabled = True
        self.config = config
        self.output_dir = Path(getattr(config, "output_dir", "outputs"))
        self.state_path = Path(state_path) if state_path is not None else self.output_dir / "paper_state.json"
        self.model_version = model_version or str(getattr(config, "paper_model_version", getattr(config, "model_version", "delta_gamma_v1")))
        if self.model_version != "delta_gamma_v1":
            raise ValueError(f"不支持的纸面定价模型：{self.model_version}；当前仅支持 delta_gamma_v1")
        self.initial_cash = _number(getattr(config, "initial_cash", 100000.0), "初始虚拟资金", positive=True)
        self.cash = self.initial_cash
        self.positions: list[Position] = []
        self._validate_config()
        if self.state_path.exists():
            self._load()

    @property
    @_synchronized
    def open_positions(self) -> list[Position]:
        return [position for position in self.positions if position.status == "OPEN"]

    @property
    @_synchronized
    def closed_positions(self) -> list[Position]:
        return [position for position in self.positions if position.status == "CLOSED"]

    @property
    def trades(self) -> list[Position]:
        return self.closed_positions

    def _cfg(self, name: str, default: Any) -> Any:
        return getattr(self.config, name, default)

    def _validate_config(self) -> None:
        for name, default in (("contracts_per_trade", 1), ("max_open_positions", 5)):
            value = self._cfg(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"配置 {name} 必须是正整数")
        for name, default in (("max_trade_cost_usd", 5000), ("max_total_premium_usd", 20000), ("max_quote_age_seconds", 900)):
            _number(self._cfg(name, default), name, positive=True)
        slippage = _number(self._cfg("slippage_pct", 0.02), "slippage_pct")
        drawdown = _number(self._cfg("trailing_drawdown", 0.20), "trailing_drawdown")
        if not 0 <= slippage < 1 or not 0 < drawdown < 1:
            raise ValueError("滑点必须处于 [0,1)，回撤比例必须处于 (0,1)")
        stop = _number(self._cfg("hard_stop_roi", -0.35), "hard_stop_roi")
        if not -1 <= stop < 0:
            raise ValueError("初始硬止损收益率必须处于 [-1,0)")
        breakeven = _number(self._cfg("breakeven_trigger_roi", 0.35), "breakeven_trigger_roi")
        trailing = _number(self._cfg("trailing_trigger_roi", 0.60), "trailing_trigger_roi")
        take_profit = _number(self._cfg("hard_take_profit_roi", 1.50), "hard_take_profit_roi")
        if not 0 <= breakeven <= trailing <= take_profit:
            raise ValueError("保本、跟踪止盈、硬止盈触发收益率必须非负且依次不降")
        if self._cfg("trailing_mode", "premium") not in {"premium", "roi"}:
            raise ValueError("trailing_mode 只能是 premium 或 roi")
        if not 0 < abs(_number(self._cfg("default_delta", 0.45), "default_delta")) <= 1:
            raise ValueError("默认 delta 的绝对值必须处于 (0,1]")
        if _number(self._cfg("default_gamma", 0.04), "default_gamma") < 0:
            raise ValueError("默认 gamma 不能为负数")

    def _load(self) -> None:
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, dict) or payload.get("schema_version") != STATE_VERSION:
                raise ValueError("纸面账本版本无效或不受支持")
            if payload.get("model_version") != self.model_version:
                raise ValueError("纸面账本定价模型与当前配置不一致")
            initial = _number(payload["initial_cash"], "账本初始虚拟资金", positive=True)
            cash = _number(payload["cash"], "账本虚拟余额")
            positions = [_position_from_json(value) for value in payload["positions"]]
            if cash < -1e-6:
                raise ValueError("纸面账本虚拟余额不能为负")
            if len({position.position_id for position in positions}) != len(positions) or len({position.signal_id for position in positions}) != len(positions):
                raise ValueError("纸面账本包含重复持仓或重复信号")
            expected_cash = initial + sum(position.realized_pnl or 0 for position in positions if position.status == "CLOSED") - sum(position.entry_cost for position in positions if position.status == "OPEN")
            if not math.isclose(cash, expected_cash, rel_tol=1e-9, abs_tol=0.01):
                raise ValueError("纸面账本余额与交易记录不一致")
            self.initial_cash, self.cash, self.positions = initial, cash, positions
            LOGGER.info("恢复纸面账本：未平仓 %d 笔，已平仓 %d 笔，虚拟余额 %.2f", len(self.open_positions), len(self.closed_positions), self.cash)
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise PaperStateError(f"无法安全恢复纸面账本 {self.state_path}：{exc}；请保留原文件后人工检查，不会自动重置资金") from exc

    def _persist(self) -> None:
        payload = {
            "schema_version": STATE_VERSION,
            "model_version": self.model_version,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "positions": [_json_position(position) for position in self.positions],
        }
        self._atomic_write(self.state_path, lambda stream: json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False))

    @staticmethod
    def _atomic_write(path: Path, writer: Any, *, csv_file: bool = False) -> None:
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig" if csv_file else "utf-8", newline="", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
                temporary = stream.name
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError) as exc:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
            raise PaperStateError(f"无法原子保存纸面交易文件 {path}：{exc}；本次内存变更将回滚") from exc

    def _save_or_rollback(self, snapshot: tuple[float, list[Position]]) -> None:
        try:
            self._persist()
        except PaperStateError:
            self.cash, self.positions = snapshot
            raise

    def _quote(self, signal: Signal, quote: dict[str, Any], now: datetime) -> dict[str, Any]:
        if not isinstance(quote, dict):
            raise ValueError("期权报价必须是字典")
        ticker = str(signal.ticker).strip().upper()
        action = str(signal.action).upper()
        if action not in {"BUY_CALL", "BUY_PUT"}:
            raise ValueError("信号 action 必须是 BUY_CALL 或 BUY_PUT")
        right = str(quote.get("right", "")).upper()
        if right not in {"C", "P"} or right != ("C" if action == "BUY_CALL" else "P"):
            raise ValueError("报价期权方向与信号不一致")
        expiration = str(quote.get("expiration", ""))
        expiry = date.fromisoformat(expiration)
        if expiry.isoformat() != expiration or expiration != str(signal.expiration):
            raise ValueError("报价到期日无效或与信号不一致")
        if expiry < now.date():
            raise ValueError("不能开仓已经到期的期权")
        strike = _number(quote.get("strike"), "期权行权价", positive=True)
        symbol = str(quote.get("contract_symbol", "")).strip().upper()
        simulated = quote.get("simulated", False)
        if not isinstance(simulated, bool):
            raise ValueError("simulated 必须是布尔值")
        if not symbol:
            raise ValueError("缺少真实期权合约代码；模拟数据也必须显式标记合约")
        match = _OCC.fullmatch(symbol)
        if not simulated:
            if match is None:
                raise ValueError("真实报价必须包含有效 OCC 合约代码，不会自行拼造合约")
            root, occ_date, occ_right, occ_strike = match.groups()
            occ_expiration = datetime.strptime(occ_date, "%y%m%d").date().isoformat()
            if root.replace(".", "") != ticker.replace(".", "") or occ_right != right or occ_expiration != expiration or not math.isclose(int(occ_strike) / 1000.0, strike, abs_tol=0.0005):
                raise ValueError("OCC 合约代码与 ticker、到期日、方向或行权价不一致")
        bid = _number(quote.get("bid"), "bid")
        ask = _number(quote.get("ask"), "ask", positive=True)
        if bid < 0 or ask < bid:
            raise ValueError("报价价差无效：要求 0 <= bid <= ask")
        midpoint = (bid + ask) / 2.0
        if quote.get("mid") is not None and not math.isclose(_number(quote["mid"], "mid", positive=True), midpoint, rel_tol=1e-6, abs_tol=0.005):
            raise ValueError("mid 与 bid/ask 中间价不一致")
        quote_time = None
        for name in ("quoted_at", "quote_time", "timestamp", "updated_at", "as_of"):
            if quote.get(name) is not None:
                quote_time = _parse_time(quote[name])
                break
        if quote_time is not None:
            age = _age(now, quote_time)
            if age < -5:
                raise ValueError("报价源时间位于未来，拒绝开仓")
            if age > self._cfg("max_quote_age_seconds", 900):
                raise ValueError(f"期权报价已过期（{age:.0f} 秒），拒绝开仓")
        else:
            LOGGER.warning("%s 报价缺少源时间戳，无法确认报价新鲜度；账本保留该标记", ticker)
        delta = _number(quote.get("delta") if quote.get("delta") is not None else self._cfg("default_delta", 0.45), "delta")
        if abs(delta) > 1:
            raise ValueError("delta 绝对值不能大于 1")
        delta = abs(delta) if right == "C" else -abs(delta)
        gamma = _number(quote.get("gamma") if quote.get("gamma") is not None else self._cfg("default_gamma", 0.04), "gamma")
        if gamma < 0:
            raise ValueError("gamma 不能为负数")
        return {"ticker": ticker, "action": action, "right": right, "expiration": expiration, "strike": strike, "contract_symbol": symbol, "entry_mark": midpoint, "delta": delta, "gamma": gamma, "simulated_quote": simulated, "entry_quote_time": quote_time, "quote_timestamp_known": quote_time is not None}

    @_synchronized
    def stop_new_entries(self) -> None:
        """日终关闭本进程的新开仓入口；与开仓共用锁以消除检查时差。"""
        self._entries_enabled = False
        LOGGER.info("本进程已停止纸面新开仓，后续迟到信号将忽略")

    @_synchronized
    def open_position(self, signal: Signal, quote: dict[str, Any], now: datetime) -> Position | None:
        if not self._entries_enabled:
            LOGGER.info("纸面新开仓已停止，忽略收盘后或退出期间的迟到信号")
            return None
        try:
            _utc(now)
            signal_id = str(signal.signal_id).strip()
            if not signal_id:
                raise ValueError("缺少 signal_id，无法保证重启后的信号去重")
            for existing in self.positions:
                if existing.signal_id == signal_id:
                    LOGGER.info("信号 %s 已处理，跳过重复开仓", signal_id)
                    return existing if existing.status == "OPEN" else None
            if _age(now, signal.detected_at) < 0:
                raise ValueError("入场时间早于信号检测时间")
            spot = _number(signal.spot, "信号现价", positive=True)
            target = _number(signal.target_strike, "目标行权价", positive=True)
            normalized = self._quote(signal, quote, now)
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            LOGGER.warning("纸面开仓拒绝：%s", exc)
            return None
        quantity = self._cfg("contracts_per_trade", 1)
        slippage = self._cfg("slippage_pct", 0.02)
        entry_price = normalized["entry_mark"] * (1.0 + slippage)
        cost = entry_price * quantity * MULTIPLIER
        reasons = []
        if len(self.open_positions) >= self._cfg("max_open_positions", 5):
            reasons.append("超过最大未平仓笔数")
        if cost > self._cfg("max_trade_cost_usd", 5000) + 1e-8:
            reasons.append("超过单笔权利金限额")
        if sum(position.entry_cost for position in self.open_positions) + cost > self._cfg("max_total_premium_usd", 20000) + 1e-8:
            reasons.append("超过总在仓权利金限额")
        if cost > self.cash + 1e-8:
            reasons.append("虚拟现金不足")
        if reasons:
            LOGGER.warning("%s 纸面开仓跳过：%s；入场成本 %.2f", normalized["ticker"], "；".join(reasons), cost)
            return None
        initial_net_roi = (normalized["entry_mark"] * (1.0 - slippage) / entry_price - 1.0) * 100.0
        position = Position(
            position_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"yhs-paper:{signal_id}")),
            signal_id=signal_id, target_strike=target, quantity=quantity,
            entry_time=now, entry_spot=spot, entry_option_price=entry_price,
            exit_time=None, exit_spot=None, exit_option_price=None,
            realized_pnl=None, realized_roi_pct=None, direction_correct=None, exit_reason=None,
            peak_roi_pct=max(0.0, initial_net_roi), max_adverse_roi_pct=min(0.0, initial_net_roi),
            last_spot=spot, last_mark=normalized["entry_mark"], last_time=now,
            status="OPEN", stop_roi=self._cfg("hard_stop_roi", -0.35),
            valuation_source="delta_gamma_model", stale=False,
            model_version=self.model_version, slippage_pct=slippage, **normalized,
        )
        snapshot = (self.cash, copy.deepcopy(self.positions))
        self.cash -= cost
        self.positions.append(position)
        self._save_or_rollback(snapshot)
        LOGGER.info("纸面开仓 %s %s %s %d 张，原始中间价 %.4f，含滑点入场价 %.4f，成本 %.2f", position.ticker, position.action, position.contract_symbol, quantity, position.entry_mark, entry_price, cost)
        return position

    @staticmethod
    def _model_mark(position: Position, spot: float) -> float:
        move = spot - position.entry_spot
        return max(0.0, position.entry_mark + position.delta * move + 0.5 * position.gamma * move * move)

    def _update_tiers(self, position: Position, roi: float) -> str:
        position.peak_roi_pct = max(position.peak_roi_pct, roi * 100.0)
        position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, roi * 100.0)
        peak = position.peak_roi_pct / 100.0
        reason = "HARD_STOP"
        if peak >= self._cfg("breakeven_trigger_roi", 0.35):
            position.stop_roi = max(position.stop_roi, 0.0)
            reason = "BREAKEVEN_STOP"
        if peak >= self._cfg("trailing_trigger_roi", 0.60):
            drawdown = self._cfg("trailing_drawdown", 0.20)
            candidate = (1.0 + peak) * (1.0 - drawdown) - 1.0 if self._cfg("trailing_mode", "premium") == "premium" else peak - drawdown
            position.stop_roi = max(position.stop_roi, candidate)
            reason = "TRAILING_STOP"
        return reason

    def _close(self, position: Position, now: datetime, reason: str) -> None:
        position.stale = position.stale or _age(now, position.last_time) > self._cfg("max_quote_age_seconds", 900)
        position.exit_time = now
        position.exit_spot = position.last_spot
        position.exit_option_price = position.liquidation_price
        position.realized_pnl = (position.exit_option_price - position.entry_option_price) * position.quantity * MULTIPLIER
        position.realized_roi_pct = (position.exit_option_price / position.entry_option_price - 1.0) * 100.0
        # 用户定义：只要持仓过程中净清算 ROI 曾达到 15%，即为方向正确。
        # 保本或止损退出不能抹掉此前出现过的有效方向收益。
        position.direction_correct = position.peak_roi_pct >= 15.0
        position.exit_reason = reason
        position.status = "CLOSED"
        self.cash += position.exit_option_price * position.quantity * MULTIPLIER
        LOGGER.info("纸面平仓 %s %s，原因 %s，模型退出价 %.4f，PnL %.2f，ROI %.2f%%，方向正确=%s，数据过期=%s", position.ticker, position.contract_symbol, reason, position.exit_option_price, position.realized_pnl, position.realized_roi_pct, position.direction_correct, position.stale)

    @_synchronized
    def mark_to_market(self, ticker: str, spot: float, now: datetime, *, observed_at: datetime | None = None) -> list[Position]:
        """now 是决策时刻；observed_at 是源现价时刻，避免倒填平仓时间。"""
        spot = _number(spot, "标的现价", positive=True)
        _utc(now)
        observed_at = now if observed_at is None else observed_at
        _utc(observed_at)
        if _age(now, observed_at) < 0:
            LOGGER.warning("%s 现价源时间位于决策时刻之后，忽略未来数据", ticker)
            return []
        relevant = [position for position in self.open_positions if position.ticker == ticker.strip().upper()]
        if not relevant:
            return []
        if any(_age(observed_at, position.last_time) <= 0 for position in relevant):
            LOGGER.warning("%s 现价源时间未递增，忽略重复或乱序输入以保护纸面账本", ticker)
            return []
        snapshot = (self.cash, copy.deepcopy(self.positions))
        closed = []
        try:
            for position in relevant:
                position.last_spot = spot
                position.last_mark = self._model_mark(position, spot)
                if not math.isfinite(position.last_mark):
                    raise ValueError("delta-gamma 模型价格溢出")
                position.last_time = observed_at
                position.stale = _age(now, observed_at) > self._cfg("max_quote_age_seconds", 900)
                roi = position.roi_pct / 100.0
                stop_reason = self._update_tiers(position, roi)
                reason = None
                if roi >= self._cfg("hard_take_profit_roi", 1.50):
                    reason = "HARD_TAKE_PROFIT"
                elif roi <= position.stop_roi:
                    reason = stop_reason
                if reason is not None:
                    self._close(position, now, reason)
                    closed.append(position)
            self._save_or_rollback(snapshot)
        except (ValueError, OverflowError) as exc:
            self.cash, self.positions = snapshot
            raise ValueError(f"纸面盯市失败，变更已回滚：{exc}") from exc
        return closed

    @_synchronized
    def close_all(self, now: datetime, reason: str = "SESSION_CLOSE") -> list[Position]:
        _utc(now)
        positions = self.open_positions
        if not positions:
            return []
        if not reason or not isinstance(reason, str):
            raise ValueError("平仓原因必须是非空字符串")
        if any(_age(now, position.last_time) < 0 for position in positions):
            raise ValueError("批量平仓时间不能早于最后观察时间")
        snapshot = (self.cash, copy.deepcopy(self.positions))
        for position in positions:
            # 复用最后观察值，不捏造收盘报价或现价；stale 记录观测时效。
            self._close(position, now, reason)
        self._save_or_rollback(snapshot)
        return positions

    @_synchronized
    def export_trades_csv(self, path: Path | None = None) -> Path:
        destination = Path(path) if path is not None else self.output_dir / "trades_history.csv"

        def write(stream: Any) -> None:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            # 每次完整重写已平仓历史，重启和重复导出不会追加重复成交。
            for position in self.closed_positions:
                writer.writerow(_json_position(position))

        self._atomic_write(destination, write, csv_file=True)
        return destination

    @_synchronized
    def statistics(self) -> dict[str, Any]:
        closed = self.closed_positions
        opened = self.open_positions
        count = len(closed)
        realized = sum(position.realized_pnl or 0.0 for position in closed)
        open_value = sum(position.liquidation_price * position.quantity * MULTIPLIER for position in opened)
        return {
            "total_trades": count,
            "open_positions": len(opened),
            "realized_pnl": realized,
            "directional_accuracy_pct": 100.0 * sum(position.peak_roi_pct >= 15.0 for position in closed) / count if count else 0.0,
            "avg_peak_mfe_pct": sum(position.peak_roi_pct for position in closed) / count if count else 0.0,
            "win_rate_pct": 100.0 * sum((position.realized_pnl or 0.0) > 0 for position in closed) / count if count else 0.0,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "open_liquidation_value": open_value,
            "unrealized_pnl": sum((position.liquidation_price - position.entry_option_price) * position.quantity * MULTIPLIER for position in opened),
            "equity": self.cash + open_value,
            "total_return_pct": ((self.cash + open_value) / self.initial_cash - 1.0) * 100.0,
            "stale_closed_trades": sum(position.stale for position in closed),
            "model_version": self.model_version,
        }
