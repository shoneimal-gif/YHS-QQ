"""Alpaca Paper 订单执行账本；不支持实盘地址，也不托管策略进程。

程序必须持续运行并调用 poll，阶梯止损由程序驱动，未放置券商保护单。
indicative 报价仅用于 Paper 流程验证；CSV 成交价来自 Alpaca 的模拟成交。
手续费预留是风险预算估计，不是假定真实费用，成交 PnL 不包含未知费用。
"""
from __future__ import annotations

import atexit
import csv
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import eastern_timezone
from alpaca_client import AlpacaError, SubmissionNotSent, SubmissionUnknown

LOGGER = logging.getLogger(__name__)
MULTIPLIER = 100
STATE_VERSION = 1
OCC = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
TICKER = re.compile(r"^[A-Z]{1,6}$")
TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "failed", "voided"}
ACTIVE_STATUSES = {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY", "OPEN", "EXIT_INTENT", "EXIT_UNKNOWN", "PENDING_EXIT", "HOLD"}
CSV_FIELDS = [
    "position_id", "ticker", "action", "contract_symbol", "strike", "entry_time",
    "entry_spot", "entry_option_price", "exit_time", "exit_spot", "exit_option_price",
    "realized_pnl", "realized_roi_pct", "peak_roi_pct", "max_adverse_roi_pct",
    "direction_correct", "exit_reason", "expiration", "right", "quantity",
    "valuation_source", "stale", "signal_id", "status", "entry_order_id",
    "exit_order_id", "entry_client_order_id", "exit_client_order_id", "feed",
    "price_is_estimate", "pnl_basis", "fee_reserve_usd", "program_monitor_required",
    "requested_quantity", "filled_quantity", "exit_filled_quantity",
]


def synchronized(method: Any) -> Any:
    @wraps(method)
    def locked(self: AlpacaPaperTrader, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}不能是布尔值")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}缺失或不是数字") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label}必须为{'正的' if positive else ''}有限数字")
    return result


def parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("缺少有效源时间")
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result


def age(now: datetime, earlier: datetime) -> float:
    return (parse_time(now).astimezone(timezone.utc) - parse_time(earlier).astimezone(timezone.utc)).total_seconds()


def symbol_of(value: Any) -> str:
    symbol = str(value or "").strip().upper().replace(" ", "")
    return symbol[2:] if symbol.startswith("O:") else symbol


def serialized(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: serialized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialized(item) for item in value]
    return value


@dataclass(frozen=True)
class AlpacaRiskConfig:
    strategy_budget_usd: float = 100000.0
    max_trade_debit: float = 1000.0
    max_open_positions: int = 10
    max_total_premium: float = 10000.0
    max_daily_entries: int = 20
    daily_loss_limit: float = 3000.0
    trial_loss: float = 10000.0
    target_premium_fraction: float = 0.01
    max_contracts_per_trade: int = 50
    allowed_symbols: tuple[str, ...] | None = None
    entry_windows: tuple[tuple[str, str], ...] = (("09:45", "11:30"), ("14:30", "15:00"))
    flatten_time: str = "15:45"
    flatten_before_close_minutes: int = 15
    min_expiry_days: int = 5
    max_expiry_days: int = 10
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.55
    max_spread_pct: float = 0.05  # fraction: 0.05 means 5% of midpoint
    max_spread_usd: float = 0.05
    max_quote_age_seconds: int = 30
    order_timeout_seconds: int = 120
    reconciliation_grace_seconds: int = 15
    fee_reserve_usd_per_order: float = 1.0
    feed: str = "indicative"
    hard_stop_roi: float = -0.35
    breakeven_trigger_roi: float = 0.35
    trailing_trigger_roi: float = 0.60
    trailing_drawdown: float = 0.20
    hard_take_profit_roi: float = 1.50
    trailing_mode: str = "premium"

    def __post_init__(self) -> None:
        if self.allowed_symbols is not None:
            if not isinstance(self.allowed_symbols, (tuple, list)):
                raise ValueError("allowed_symbols须为null或非空代码列表")
            symbols = tuple(str(item).strip().upper() for item in self.allowed_symbols)
            if not symbols or len(set(symbols)) != len(symbols) or any(not TICKER.fullmatch(item) for item in symbols):
                raise ValueError("显式交易池不能为空、重复或包含无效期权标的代码")
            object.__setattr__(self, "allowed_symbols", symbols)
        object.__setattr__(self, "entry_windows", tuple(tuple(window) for window in self.entry_windows))
        for name in ("strategy_budget_usd", "max_trade_debit", "max_total_premium", "daily_loss_limit", "trial_loss", "max_spread_pct", "max_spread_usd"):
            number(getattr(self, name), name, positive=True)
        for name in ("max_open_positions", "max_daily_entries", "max_contracts_per_trade", "max_quote_age_seconds", "order_timeout_seconds", "reconciliation_grace_seconds", "flatten_before_close_minutes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name}必须是正整数")
        if not 0 < self.target_premium_fraction <= 0.01:
            raise ValueError("首轮Paper每单目标权利金最多为风险基数1%")
        if not 1 <= self.min_expiry_days <= self.max_expiry_days:
            raise ValueError("到期日范围无效，不允许0DTE")
        if not 0 < self.min_abs_delta <= self.max_abs_delta <= 1:
            raise ValueError("delta范围无效")
        if self.max_trade_debit > self.max_total_premium or self.max_total_premium > self.strategy_budget_usd:
            raise ValueError("单笔/总权利金不得超过策略预算")
        if number(self.fee_reserve_usd_per_order, "手续费预留") < 0:
            raise ValueError("手续费预留不能为负")
        if self.feed not in {"indicative", "opra"} or self.trailing_mode not in {"premium", "roi"}:
            raise ValueError("行情源或追踪模式无效")
        if not -1 < self.hard_stop_roi < 0 < self.breakeven_trigger_roi < self.trailing_trigger_roi < self.hard_take_profit_roi:
            raise ValueError("ROI层级顺序无效")
        if not 0 < self.trailing_drawdown < 1:
            raise ValueError("追踪回撤必须在0至1之间")
        time.fromisoformat(self.flatten_time)
        for window in self.entry_windows:
            if len(window) != 2 or time.fromisoformat(window[0]) >= time.fromisoformat(window[1]):
                raise ValueError("入场时间窗口无效")

    @property
    def candidate_tickers(self) -> tuple[str, ...] | None:
        # None表示沿用main/detector经过股票类型、五只ETF、价格/OI等规则过滤的池。
        return self.allowed_symbols


def load_alpaca_policy(path: str | Path) -> AlpacaRiskConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Alpaca风险配置必须是JSON对象")
    if "alpaca" in payload:
        payload = payload["alpaca"]
    if not isinstance(payload, dict):
        raise ValueError("alpaca配置必须是JSON对象")
    allowed = {field.name for field in fields(AlpacaRiskConfig)}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"未知Alpaca风险字段：{', '.join(sorted(unknown))}")
    return AlpacaRiskConfig(**payload)


@dataclass
class AlpacaPosition:
    position_id: str
    ticker: str
    signal_id: str
    action: str
    contract_symbol: str
    strike: float
    expiration: str
    right: str
    intended_at: datetime
    entry_time: datetime
    entry_spot: float
    quantity: int = 1
    status: str = "ENTRY_INTENT"
    entry_limit_price: float = 0.0
    entry_option_price: float = 0.0
    filled_quantity: float = 0.0
    exit_filled_quantity: float = 0.0
    exit_order_filled_quantity: float = 0.0
    exit_order_notional_usd: float = 0.0
    exit_proceeds_usd: float = 0.0
    exit_realized_by_day: dict[str, float] = field(default_factory=dict)
    entry_order_id: str | None = None
    exit_order_id: str | None = None
    entry_client_order_id: str = ""
    exit_client_order_id: str | None = None
    entry_cancel_requested: bool = False
    exit_cancel_requested: bool = False
    entry_order_state: str | None = None
    exit_order_state: str | None = None
    exit_requested_at: datetime | None = None
    exit_time: datetime | None = None
    exit_spot: float | None = None
    exit_option_price: float | None = None
    realized_pnl: float | None = None
    realized_roi_pct: float | None = None
    peak_roi_pct: float = 0.0
    max_adverse_roi_pct: float = 0.0
    direction_correct: bool | None = None
    exit_reason: str | None = None
    stop_roi: float = -0.35
    delta: float = 0.0
    gamma: float = 0.0
    last_spot: float = 0.0
    last_spot_time: datetime | None = None
    last_mark: float | None = None  # observed broker bid, never a delta-gamma mark
    last_time: datetime | None = None
    last_reconciled_at: datetime | None = None
    last_fill_observed_at: datetime | None = None
    hold_reason: str | None = None
    stale: bool = True
    feed: str = "indicative"
    valuation_source: str = "alpaca_indicative_bid_estimate"
    price_is_estimate: bool = True
    pnl_basis: str = "broker_paper_fills_gross_excludes_fees"
    fee_reserve_usd: float = 2.0
    program_monitor_required: bool = True

    @property
    def entry_cost(self) -> float:
        return self.entry_option_price * self.filled_quantity * MULTIPLIER

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.filled_quantity - self.exit_filled_quantity)

    @property
    def reserved_debit(self) -> float:
        if self.status in {"CLOSED", "CANCELLED"}:
            return 0.0
        if self.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY"}:
            # 撤单请求不释放未成交部分，直到券商确认终态。
            filled_cost = self.entry_option_price * self.filled_quantity * MULTIPLIER
            pending_cost = self.entry_limit_price * (self.quantity - self.filled_quantity) * MULTIPLIER
            return filled_cost + pending_cost + self.fee_reserve_usd
        if self.filled_quantity > 0:
            return self.entry_option_price * self.remaining_quantity * MULTIPLIER + self.fee_reserve_usd
        return self.entry_limit_price * self.quantity * MULTIPLIER + self.fee_reserve_usd

    @property
    def partial_realized_pnl(self) -> float:
        return self.exit_proceeds_usd - self.entry_option_price * self.exit_filled_quantity * MULTIPLIER

    @property
    def roi_pct(self) -> float | None:
        return ((self.last_mark / self.entry_option_price - 1.0) * 100.0
                if self.last_mark is not None and self.entry_option_price > 0 else None)


class AlpacaLedgerError(RuntimeError):
    pass


class AlpacaPaperTrader:
    def __init__(self, client: Any, config: Any, state_path: str | Path | None = None, *,
                 policy: AlpacaRiskConfig | dict[str, Any] | None = None,
                 execute_paper: bool = False, now_fn: Any = None,
                 notification_sink: Any = None):
        self._lock = threading.RLock()
        self._entry_stop = threading.Event()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.client = client
        self.config = config
        self.policy = policy if isinstance(policy, AlpacaRiskConfig) else AlpacaRiskConfig(**(policy or {}))
        self.output_dir = Path(config.output_dir)
        self.state_path = Path(state_path) if state_path is not None else self.output_dir / "alpaca_paper_state.json"
        self.execute_paper = bool(execute_paper)
        self.notification_sink = notification_sink
        self._notification_events: dict[str, dict[str, Any]] = {}
        self.initial_cash = self.policy.strategy_budget_usd
        self.account_id: str | None = None
        self.positions: list[AlpacaPosition] = []
        self._entries_enabled = True
        self._allow_new_entries = True
        self._network_ok = False
        self._account: dict[str, Any] | None = None
        self._remote_positions: dict[str, dict[str, Any]] = {}
        self._remote_orders: list[dict[str, Any]] = []
        self._unmanaged_positions = 0
        self._unmanaged_orders = 0
        self._blocks: set[str] = {"尚未完成券商对账"}
        self._latched_halts: set[str] = set()
        self._clock: dict[str, Any] | None = None
        self._preview_count = 0
        self._quote_batch_symbols = 0
        self._quote_batch_missing = 0
        self._lease: Any = None
        self._policy_hash = hashlib.sha256(json.dumps(serialized(asdict(self.policy)), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._assert_paper()
        self._acquire_lease()
        try:
            self._load()
        except Exception:
            self.release_state_lock()
            raise
        atexit.register(self.release_state_lock)
        LOGGER.warning("Alpaca模式=%s，策略预算$%.2f；阶梯退出由程序监控，未放置券商保护单，失联时无法保证止损", "执行Paper" if self.execute_paper else "只预览", self.initial_cash)
        if self.policy.feed == "indicative":
            LOGGER.warning("使用indicative衍生报价，只验证Paper流程；不可据此确认真实可成交价或实盘收益")

    def _assert_paper(self) -> None:
        endpoint = str(getattr(self.client, "base_url", ""))
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "paper-api.alpaca.markets" or parsed.port not in (None, 443):
            raise ValueError("AlpacaPaperTrader仅允许https://paper-api.alpaca.markets；拒绝实盘及未知地址")
        if getattr(self.client, "paper_only", True) is not True:
            raise ValueError("client未声明Paper隔离，拒绝启动")

    def _acquire_lease(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lease = self.state_path.with_suffix(self.state_path.suffix + ".lock").open("a+b")
        try:
            lease.seek(0, os.SEEK_END)
            if lease.tell() == 0:
                lease.write(b"0")
                lease.flush()
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lease.close()
            raise AlpacaLedgerError("同一Alpaca账本已有进程运行；拒绝重复执行") from exc
        self._lease = lease

    @synchronized
    def release_state_lock(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != STATE_VERSION or payload.get("policy_hash") != self._policy_hash:
                raise ValueError("状态版本或不可变风险配置不一致；请审阅已有账本，不能重置预算继续交易")
            self.account_id = payload.get("account_id")
            if self.account_id is not None and not isinstance(self.account_id, str):
                raise ValueError("账户绑定无效")
            loaded = []
            for row in payload["positions"]:
                values = dict(row)
                for name in ("intended_at", "entry_time", "exit_time", "exit_requested_at", "last_time", "last_spot_time", "last_reconciled_at", "last_fill_observed_at"):
                    if values.get(name) is not None:
                        values[name] = parse_time(values[name])
                position = AlpacaPosition(**values)
                if position.status not in ACTIVE_STATUSES | {"CLOSED", "CANCELLED"} or not OCC.fullmatch(position.contract_symbol):
                    raise ValueError("状态中的期权或订单状态无效")
                if not self._allowed_ticker(position.ticker) or not isinstance(position.quantity, int) or not 1 <= position.quantity <= self.policy.max_contracts_per_trade or not position.entry_client_order_id:
                    raise ValueError("状态持仓归属或数量无效")
                for name in ("entry_limit_price", "entry_option_price", "filled_quantity", "exit_filled_quantity", "exit_order_filled_quantity", "exit_order_notional_usd", "exit_proceeds_usd", "strike", "peak_roi_pct", "max_adverse_roi_pct"):
                    number(getattr(position, name), name)
                if not 0 <= position.exit_filled_quantity <= position.filled_quantity <= position.quantity:
                    raise ValueError("成交数量不一致")
                if position.filled_quantity != int(position.filled_quantity) or position.exit_filled_quantity != int(position.exit_filled_quantity):
                    raise ValueError("期权成交数量必须为整数")
                if not isinstance(position.exit_realized_by_day, dict):
                    raise ValueError("部分成交日期账本无效")
                for day, amount in position.exit_realized_by_day.items():
                    date.fromisoformat(day)
                    number(amount, "部分成交PnL")
                if position.status == "CLOSED" and (position.exit_filled_quantity != position.filled_quantity or position.realized_pnl is None or position.exit_option_price is None):
                    raise ValueError("平仓状态缺少完整券商成交")
                if position.status in {"ENTRY_INTENT", "EXIT_INTENT"}:
                    position.status = "ENTRY_UNKNOWN" if position.status == "ENTRY_INTENT" else "EXIT_UNKNOWN"
                    position.hold_reason = "重启发现提交意图，需按client_order_id查询，不盲目重提"
                loaded.append(position)
            if len({item.signal_id for item in loaded}) != len(loaded):
                raise ValueError("状态signal_id重复")
            self.positions = loaded
            self._latched_halts = set(payload.get("latched_halts", []))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AlpacaLedgerError(f"Alpaca账本无法安全恢复：{exc}；不会重新创建虚拟预算") from exc

    @staticmethod
    def _atomic_write(path: Path, writer: Any, *, csv_file: bool = False) -> None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig" if csv_file else "utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError) as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AlpacaLedgerError(f"无法原子保存Alpaca账本：{exc}；必须停止执行并保留订单ID供恢复") from exc

    def _persist(self) -> None:
        payload = {"schema_version": STATE_VERSION, "account_id": self.account_id,
                   "policy_hash": self._policy_hash, "policy": serialized(asdict(self.policy)),
                   "positions": serialized([asdict(item) for item in self.positions]),
                   "latched_halts": sorted(self._latched_halts)}
        self._atomic_write(self.state_path, lambda stream: json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False))
        # Trade state is committed first. Notification failures never change order/risk state.
        if self.notification_sink is not None:
            pending, self._notification_events = self._notification_events, {}
            for identifier, event in pending.items():
                try:
                    accepted = self.notification_sink(event)
                except Exception:
                    accepted = False
                if not accepted:
                    self._notification_events[identifier] = event
                    LOGGER.warning("企业微信通知未能入队，后续持久化时重试；交易账本已保存")

    def _queue_order_notice(self, position: AlpacaPosition, kind: str, now: datetime, *,
                            entry: bool, client_id: str | None = None,
                            order_id: str | None = None, requested: float | None = None,
                            filled: float = 0, price: float | None = None) -> None:
        if self.notification_sink is None or not self.execute_paper:
            return
        identifier = client_id or (position.entry_client_order_id if entry else position.exit_client_order_id)
        if not identifier:
            return
        side = "buy" if entry else "sell"
        key = hashlib.sha256(f"{identifier}:{side}:{kind}:{int(filled)}".encode()).hexdigest()
        event = {
            "event_id": key, "kind": kind, "side": side,
            "ticker": position.ticker, "contract_symbol": position.contract_symbol,
            "requested_quantity": int(requested if requested is not None else
                                      position.quantity if entry else position.remaining_quantity),
            "filled_quantity": int(filled), "price": price,
            "order_id": order_id, "client_order_id": identifier,
            "timestamp": now.astimezone(timezone(timedelta(hours=8))).isoformat(),
            "exit_reason": position.exit_reason if not entry else None,
            "realized_pnl": position.realized_pnl if kind == "filled" and not entry and position.status == "CLOSED" else None,
        }
        self._notification_events[key] = event

    @property
    @synchronized
    def open_positions(self) -> list[AlpacaPosition]:
        # 提交中及未知提交也占用名额，不能因尚未成交而重复开仓。
        return [item for item in self.positions if item.status in ACTIVE_STATUSES]

    @property
    @synchronized
    def trades(self) -> list[AlpacaPosition]:
        # 全部尝试都参与信号去重；CSV和绩效仅计算已完整成交平仓的记录。
        return list(self.positions)

    @property
    @synchronized
    def closed_positions(self) -> list[AlpacaPosition]:
        return [item for item in self.positions if item.status == "CLOSED"]

    def stop_new_entries(self) -> None:
        # HTTP请求期间仍可先发出停止信号，提交前会再次检查；不等RLock。
        self._entry_stop.set()
        with self._lock:
            self._entries_enabled = False
            LOGGER.info("本进程停止Alpaca新开仓；继续对账和已有仓位退出")

    def _current_time(self) -> datetime:
        return parse_time(self._now_fn())

    def _allowed_ticker(self, ticker: str) -> bool:
        return bool(TICKER.fullmatch(ticker)) and (self.policy.allowed_symbols is None or ticker in self.policy.allowed_symbols)

    def _owned_order_ids(self) -> set[str]:
        return {value for item in self.positions for value in (item.entry_order_id, item.exit_order_id, item.entry_client_order_id, item.exit_client_order_id) if value}

    def _refresh_account_and_exposure(self, now: datetime) -> None:
        account = self.client.get_account()
        if not isinstance(account, dict) or not account.get("id"):
            raise ValueError("Alpaca账户响应缺少id")
        identifier = str(account["id"])
        if self.account_id is not None and identifier != self.account_id:
            raise AlpacaLedgerError("Alpaca账户与账本绑定不一致，禁止继续操作")
        if self.account_id is None:
            self.account_id = identifier
            self._persist()
        self._account = account
        remote = self.client.get_positions()
        remote_orders = self.client.get_orders(status="open")
        if not isinstance(remote, list) or not isinstance(remote_orders, list):
            raise ValueError("Alpaca持仓/挂单响应不是列表")
        self._remote_positions = {symbol_of(item.get("symbol")): item for item in remote if isinstance(item, dict)}
        self._remote_orders = remote_orders
        owned_symbols = {item.contract_symbol for item in self.open_positions}
        foreign_options = [symbol for symbol, item in self._remote_positions.items()
                           if (item.get("asset_class") == "us_option" or OCC.fullmatch(symbol))
                           and number(item.get("qty", 0), "远程数量") != 0 and symbol not in owned_symbols]
        owned_ids = self._owned_order_ids()
        foreign_orders = [item for item in remote_orders if isinstance(item, dict)
                          and str(item.get("id", "")) not in owned_ids
                          and str(item.get("client_order_id", "")) not in owned_ids]
        self._unmanaged_positions = len(foreign_options)
        self._unmanaged_orders = len(foreign_orders)
        if foreign_options:
            self._latched_halts.add("发现未管理期权仓位，需人工核对")
            LOGGER.error("发现未管理期权仓位%s；停止新开仓，不操作这些仓位", ",".join(foreign_options))
        if foreign_orders:
            self._latched_halts.add("发现其他策略或人工挂单，需人工核对")
            LOGGER.error("发现%d笔未管理挂单（包括股票）；停止新开仓，不撤销旧挂单", len(foreign_orders))
        if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
            self._blocks.add("账户不可交易")
        level = account.get("options_trading_level", account.get("options_approved_level", 0))
        try:
            valid_level = int(level) >= 2
        except (TypeError, ValueError):
            valid_level = False
        if not valid_level:
            self._blocks.add("账户未获期权Level 2权限")
        self._persist()

    def _quote(self, raw: Any, now: datetime, *, entry: bool = True) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("缺少券商期权报价")
        bid = number(raw.get("bid", raw.get("bp")), "bid", positive=entry)
        ask = number(raw.get("ask", raw.get("ap")), "ask", positive=True)
        if bid < 0 or ask < bid:
            raise ValueError("期权报价倒挂")
        stamp = parse_time(raw.get("quoted_at", raw.get("timestamp", raw.get("t"))))
        seconds = age(now, stamp)
        if seconds < -5 or seconds > self.policy.max_quote_age_seconds:
            raise ValueError(f"期权报价源时间不合格：{seconds:.1f}秒")
        midpoint = (bid + ask) / 2
        if entry and (ask - bid > self.policy.max_spread_usd + 1e-8 or (ask - bid) / midpoint > self.policy.max_spread_pct + 1e-8):
            raise ValueError("点差超过风险上限")
        for name in (("bid_size", "ask_size", "bs", "as") if entry else ()):
            if raw.get(name) is not None and number(raw[name], name) <= 0:
                raise ValueError("报价对应买卖盘数量为零")
        return {"bid": bid, "ask": ask, "mid": midpoint, "quoted_at": stamp}

    def _flatten_at(self, now: datetime, clock: dict[str, Any]) -> datetime:
        local = parse_time(now).astimezone(eastern_timezone())
        planned = datetime.combine(local.date(), time.fromisoformat(self.policy.flatten_time), eastern_timezone())
        try:
            actual = parse_time(clock["next_close"]).astimezone(eastern_timezone())
            if actual.date() == local.date():
                planned = min(planned, actual - timedelta(minutes=self.policy.flatten_before_close_minutes))
        except (KeyError, TypeError, ValueError):
            self._blocks.add("缺少实际收盘时钟")
        return planned

    def _in_entry_window(self, now: datetime, clock: dict[str, Any]) -> bool:
        local = parse_time(now).astimezone(eastern_timezone())
        if clock.get("is_open") is not True or now >= self._flatten_at(now, clock):
            return False
        stamp = parse_time(clock.get("timestamp"))
        actual_close = parse_time(clock.get("next_close")).astimezone(eastern_timezone())
        if actual_close.date() != local.date() or actual_close <= now:
            return False
        if not -5 <= age(now, stamp) <= 30:
            return False
        wall = local.time().replace(tzinfo=None)
        return any(time.fromisoformat(start) <= wall < time.fromisoformat(end) for start, end in self.policy.entry_windows)

    def _daily_entries(self, now: datetime) -> int:
        today = now.astimezone(eastern_timezone()).date()
        return sum(item.intended_at.astimezone(eastern_timezone()).date() == today for item in self.positions)

    def _pnl(self, now: datetime) -> tuple[float, float]:
        today = now.astimezone(eastern_timezone()).date()
        closed = self.closed_positions
        realized = sum(item.realized_pnl or 0.0 for item in closed) + sum(item.partial_realized_pnl for item in self.open_positions)
        day_realized = sum(item.exit_realized_by_day.get(today.isoformat(), 0.0) for item in self.positions)
        unrealized = sum((item.last_mark - item.entry_option_price) * item.remaining_quantity * MULTIPLIER
                         for item in self.open_positions if item.last_mark is not None and item.entry_option_price > 0)
        day_fee_reserve = sum(item.fee_reserve_usd for item in self.positions if item.filled_quantity > 0 and item.intended_at.astimezone(eastern_timezone()).date() == today)
        total_fee_reserve = sum(item.fee_reserve_usd for item in self.positions if item.filled_quantity > 0)
        return day_realized + unrealized - day_fee_reserve, realized + unrealized - total_fee_reserve

    def _apply_risk_blocks(self, now: datetime) -> None:
        daily, cumulative = self._pnl(now)
        today = now.astimezone(eastern_timezone()).date().isoformat()
        if daily <= -self.policy.daily_loss_limit:
            self._latched_halts.add(f"每日亏损熔断:{today}")
        if cumulative <= -self.policy.trial_loss:
            self._latched_halts.add("累计试验亏损熔断")
        # 只移除过去交易日的日熔断；其他人工核对条件不能靠重启解除。
        self._latched_halts = {value for value in self._latched_halts if not value.startswith("每日亏损熔断:") or value.endswith(today)}
        if self._daily_entries(now) >= self.policy.max_daily_entries:
            self._blocks.add("达到每日开仓次数")

    @synchronized
    def open_position(self, signal: Any, quote: dict[str, Any], now: datetime) -> AlpacaPosition | None:
        parse_time(now)
        now = self._current_time()
        if self._entry_stop.is_set() or not self._entries_enabled or not self._allow_new_entries:
            LOGGER.info("Alpaca新开仓入口已停止，忽略迟到信号")
            return None
        for item in self.positions:
            if item.signal_id == str(signal.signal_id):
                LOGGER.info("信号已在Alpaca账本中，跳过重复提交")
                return item if item.status in ACTIVE_STATUSES else None
        try:
            self._assert_paper()
            clock = self.client.get_clock()
            self.poll(now, clock, allow_new_entries=True)
            now = self._current_time()
            if not self._network_ok or self._blocks or self._latched_halts:
                raise ValueError("新开仓被安全门槛阻止：" + "；".join(sorted(self._blocks | self._latched_halts)))
            if not self._in_entry_window(now, clock):
                raise ValueError("当前不在入场窗口，或市场时钟不可用")
            ticker = str(signal.ticker).upper()
            if not self._allowed_ticker(ticker) or signal.action not in {"BUY_CALL", "BUY_PUT"}:
                raise ValueError("标的代码/显式交易池不合格，或不是单腿买Call/Put")
            symbol = symbol_of(quote.get("contract_symbol") or signal.flow_contract)
            if not OCC.fullmatch(symbol):
                raise ValueError("缺少真实OCC合约代码")
            contract = self.client.get_contract(symbol)
            now = self._current_time()
            if not isinstance(contract, dict) or contract.get("tradable") is not True or str(contract.get("status", "")).lower() != "active":
                raise ValueError("券商合约不可交易")
            expiration = str(contract.get("expiration_date", ""))
            days = (date.fromisoformat(expiration) - now.astimezone(eastern_timezone()).date()).days
            expected = "call" if signal.action == "BUY_CALL" else "put"
            match = OCC.fullmatch(symbol)
            if symbol_of(contract.get("symbol")) != symbol or str(contract.get("underlying_symbol", "")).upper() != ticker or contract.get("type") != expected:
                raise ValueError("券商合约与信号方向不一致")
            if expiration != str(signal.expiration) or not self.policy.min_expiry_days <= days <= self.policy.max_expiry_days:
                raise ValueError("到期日不一致或超出5–10日策略范围")
            strike = number(contract.get("strike_price"), "行权价", positive=True)
            occ_expiry = datetime.strptime(match.group(2), "%y%m%d").date().isoformat()
            if match.group(1) != ticker or match.group(3) != ("C" if expected == "call" else "P") or occ_expiry != expiration or not math.isclose(int(match.group(4)) / 1000, strike, abs_tol=0.0005):
                raise ValueError("OCC编码和券商合约元数据不一致")
            if number(contract.get("size"), "合约乘数") != MULTIPLIER:
                raise ValueError("仅允许每张100股的标准期权")
            # 前序网络读取可能耗时：重新读账户、挂单及市场时钟，最后读快照。
            self._refresh_account_and_exposure(now)
            clock = self.client.get_clock()
            self._clock = clock
            snapshot = self.client.get_option_snapshot(symbol, feed=self.policy.feed)
            now = self._current_time()
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("greeks"), dict):
                raise ValueError("缺少券商Greeks，不使用模型默认delta冒充")
            delta = number(snapshot["greeks"].get("delta"), "真实delta")
            if not self.policy.min_abs_delta <= abs(delta) <= self.policy.max_abs_delta or (expected == "call" and delta <= 0) or (expected == "put" and delta >= 0):
                raise ValueError("期权delta超出策略范围或方向异常")
            gamma = number(snapshot["greeks"].get("gamma", 0), "gamma")
            raw_quote = snapshot.get("latestQuote", snapshot.get("latest_quote"))
            fresh_quote = self._quote(raw_quote, now)
            # 接近ask的限价仍不保证成交；计价单位是每股权利金，合约乘数100。
            limit_price = float(Decimal(str(fresh_quote["ask"])).quantize(Decimal("0.01"), rounding=ROUND_CEILING))
            fee_reserve = self.policy.fee_reserve_usd_per_order * 2
            risk_base = min(self.initial_cash, number(self._account.get("equity"), "账户权益", positive=True))
            target_debit = min(self.policy.max_trade_debit, risk_base * self.policy.target_premium_fraction)
            quantity = min(self.policy.max_contracts_per_trade, math.floor((target_debit - fee_reserve) / (limit_price * MULTIPLIER)))
            if quantity < 1:
                raise ValueError("一张合约超过1%目标权利金/绝对单笔上限，放弃信号")
            debit = limit_price * MULTIPLIER * quantity + fee_reserve
            total_reserved = sum(item.reserved_debit for item in self.open_positions)
            realized = sum(item.realized_pnl or 0.0 for item in self.closed_positions) + sum(item.partial_realized_pnl for item in self.open_positions)
            if len(self.open_positions) >= self.policy.max_open_positions or any(item.contract_symbol == symbol or item.ticker == ticker for item in self.open_positions):
                raise ValueError("持仓/挂单名额已满或同标的已有订单")
            if debit > self.policy.max_trade_debit or total_reserved + debit > self.policy.max_total_premium:
                raise ValueError("权利金含估计手续费预留超过单笔/总在仓上限")
            if total_reserved + debit > self.initial_cash + realized:
                raise ValueError("策略独立风险预算不足，不能使用账户现金扩大100000美元预算")
            if number(self._account.get("options_buying_power"), "期权购买力") < debit or number(self._account.get("cash"), "账户现金") < debit:
                raise ValueError("实际Paper账户现金/期权购买力不足，旧股票及其他策略仍占用资金")
            now = self._current_time()
            self._quote(raw_quote, now)
            self._apply_risk_blocks(now)
            if self._entry_stop.is_set() or not self._entries_enabled or not self._allow_new_entries:
                raise ValueError("提交前已收到停止新开仓信号")
            if self._blocks or self._latched_halts or not self._in_entry_window(now, clock):
                raise ValueError("提交前时钟/入场窗口/风险状态已不合格")
            if not self.execute_paper:
                self._preview_count += 1
                LOGGER.info("仅预览：%s %s %d张，限价%.2f，权利金含预留$%.2f（目标风险基数1%%）；未启用--execute-paper，不提交订单", ticker, symbol, quantity, limit_price, debit)
                return None
            client_id = "yhs-opt-" + uuid.uuid4().hex
            position = AlpacaPosition(position_id=str(uuid.uuid4()), ticker=ticker, signal_id=str(signal.signal_id), action=signal.action,
                                      contract_symbol=symbol, strike=strike, expiration=expiration, right="C" if expected == "call" else "P",
                                      intended_at=now, entry_time=now, entry_spot=number(signal.spot, "信号现价", positive=True),
                                      quantity=quantity, entry_limit_price=limit_price, entry_client_order_id=client_id, stop_roi=self.policy.hard_stop_roi,
                                      delta=delta, gamma=gamma, last_spot=number(signal.spot, "信号现价", positive=True), last_spot_time=now,
                                      feed=self.policy.feed, price_is_estimate=self.policy.feed == "indicative",
                                      valuation_source="alpaca_indicative_bid_estimate" if self.policy.feed == "indicative" else "alpaca_opra_bid",
                                      fee_reserve_usd=fee_reserve)
            self.positions.append(position)
            try:
                self._persist()  # 先持久化ID、预算及意图，再POST，崩溃后可追踪。
            except AlpacaLedgerError:
                self.positions.remove(position)
                raise
            payload = {"symbol": symbol, "qty": str(quantity), "side": "buy", "position_intent": "buy_to_open", "type": "limit", "limit_price": f"{limit_price:.2f}", "time_in_force": "day", "extended_hours": False, "client_order_id": client_id}
            def before_send() -> None:
                # client在限流等待后、真正发送前调用；回调失败代表尚未POST。
                actual_now = self._current_time()
                try:
                    if (self._entry_stop.is_set() or not self._entries_enabled or not self._allow_new_entries
                            or not self._in_entry_window(actual_now, clock)):
                        raise ValueError("开仓已停止或市场窗口结束")
                    self._quote(raw_quote, actual_now)
                except (ValueError, TypeError, KeyError) as exc:
                    raise AlpacaError(f"发送前开仓校验拒绝，尚未提交订单：{exc}") from exc
            try:
                # 持久化期间也可能跨窗口/收到停止；取消未POST的本地意图。
                now = self._current_time()
                if self._entry_stop.is_set() or not self._entries_enabled or not self._in_entry_window(now, clock):
                    position.status = "CANCELLED"
                    position.entry_order_state = "cancelled_before_submission"
                    position.hold_reason = "提交前窗口已结束或停止信号生效；未向券商POST"
                    self._persist()
                    return None
                try:
                    self._quote(raw_quote, now)
                except ValueError:
                    position.status = "CANCELLED"
                    position.entry_order_state = "cancelled_before_submission"
                    position.hold_reason = "持久化后报价已失效；未向券商POST"
                    self._persist()
                    raise
                order = self.client.submit_order(payload, before_send=before_send)
                self._apply_order(position, order, self._current_time(), entry=True)
                if position.status == "ENTRY_INTENT":
                    position.status = "PENDING_ENTRY"
            except SubmissionUnknown as exc:
                position.status = "ENTRY_UNKNOWN"
                position.hold_reason = "提交响应未知；保留预算并查询client_order_id，不重提"
                self._network_ok = False
                self._blocks.add("订单提交未知")
                LOGGER.error("%s %s：%s", ticker, position.hold_reason, exc)
                self._queue_order_notice(position, "unknown", now, entry=True)
            except SubmissionNotSent as exc:
                position.status = "CANCELLED"
                position.entry_order_state = "cancelled_before_submission"
                position.hold_reason = "限流等待后停止信号/时钟/报价校验未通过，client确认未POST"
                LOGGER.info("Paper开仓意图已取消且未发送：%s", exc)
            except AlpacaError as exc:
                position.status = "CANCELLED"
                position.entry_order_state = "rejected"
                position.hold_reason = str(exc)
                LOGGER.error("Paper开仓明确拒绝：%s", exc)
                self._queue_order_notice(position, "rejected", now, entry=True)
            except (ValueError, TypeError, KeyError) as exc:
                if position.status != "CANCELLED":
                    position.status = "ENTRY_UNKNOWN"
                    position.hold_reason = "提交返回内容无法安全解析，保留client_order_id等待对账"
                    self._network_ok = False
                    self._blocks.add("订单响应无法安全解析")
                    LOGGER.error("Paper提交状态需对账：%s", exc)
                    self._queue_order_notice(position, "unknown", now, entry=True)
            self._persist()
            return position if position.status in ACTIVE_STATUSES else None
        except AlpacaLedgerError:
            self._entries_enabled = False
            raise
        except (AlpacaError, ValueError, TypeError, KeyError, AttributeError) as exc:
            if isinstance(exc, AlpacaError):
                self._network_ok = False
            LOGGER.warning("Alpaca Paper开仓拒绝：%s", exc)
            return None

    def _apply_order(self, position: AlpacaPosition, order: Any, now: datetime, *, entry: bool) -> None:
        if not isinstance(order, dict) or not order.get("id"):
            raise ValueError("券商订单响应缺少订单ID")
        expected_id = position.entry_client_order_id if entry else position.exit_client_order_id
        if str(order.get("client_order_id", "")) != expected_id or symbol_of(order.get("symbol")) != position.contract_symbol or str(order.get("side", "")) != ("buy" if entry else "sell"):
            raise AlpacaLedgerError("订单响应与本地归属不一致，拒绝使用其成交")
        state = str(order.get("status", "")).lower()
        previous_order_id = position.entry_order_id if entry else position.exit_order_id
        previous_state = position.entry_order_state if entry else position.exit_order_state
        previous_fill = position.filled_quantity if entry else position.exit_order_filled_quantity
        filled = number(order.get("filled_qty", 0), "已成交张数")
        maximum_fill = position.quantity if entry else position.remaining_quantity + position.exit_order_filled_quantity
        if number(order.get("qty"), "订单请求张数", positive=True) != maximum_fill:
            raise AlpacaLedgerError("券商订单请求数量与已持久化意图不一致")
        if not 0 <= filled <= maximum_fill:
            raise AlpacaLedgerError("券商返回超出本策略数量的成交")
        if filled != int(filled):
            position.status = "HOLD"
            position.hold_reason = "标准期权出现非整数部分成交，需人工核对"
            self._blocks.add(position.hold_reason)
            return
        if entry:
            position.entry_order_id, position.entry_order_state = str(order["id"]), state
            if position.entry_cancel_requested and state not in TERMINAL | {"pending_cancel"}:
                # GET明确仍活动后可重新发DELETE；未知取消不释放预算。
                position.entry_cancel_requested = False
            if filled < position.filled_quantity:
                raise AlpacaLedgerError("券商入场成交数量倒退，需核对更正成交")
            if filled > 0:
                price = number(order.get("filled_avg_price"), "入场成交均价", positive=True)
                if price > position.entry_limit_price + 1e-8:
                    raise AlpacaLedgerError("入场成交均价超过买入限价，需人工核对")
                if filled != position.filled_quantity or price != position.entry_option_price:
                    position.last_fill_observed_at = now
                if position.filled_quantity == 0:
                    position.entry_time = parse_time(order.get("filled_at") or order.get("updated_at") or now)
                position.filled_quantity, position.entry_option_price = filled, price
                position.status = "OPEN" if state in TERMINAL else "PENDING_ENTRY"
                position.hold_reason = None
            elif state in TERMINAL:
                position.status = "CANCELLED"
            else:
                position.status = "PENDING_ENTRY"
        else:
            position.exit_order_id, position.exit_order_state = str(order["id"]), state
            if filled < position.exit_order_filled_quantity:
                raise AlpacaLedgerError("出场成交数量倒退或超过已买入数量")
            added_quantity = filled - position.exit_order_filled_quantity
            if filled > 0:
                average = number(order.get("filled_avg_price"), "出场成交均价", positive=True)
                notional = average * filled * MULTIPLIER
                added_proceeds = notional - position.exit_order_notional_usd
                if added_quantity or added_proceeds:
                    position.last_fill_observed_at = now
                position.exit_proceeds_usd += added_proceeds
                position.exit_filled_quantity += added_quantity
                source_time = parse_time(order.get("filled_at") or order.get("updated_at") or now)
                day = source_time.astimezone(eastern_timezone()).date().isoformat()
                position.exit_realized_by_day[day] = position.exit_realized_by_day.get(day, 0.0) + added_proceeds - position.entry_option_price * added_quantity * MULTIPLIER
                position.exit_order_notional_usd = notional
                position.exit_option_price = position.exit_proceeds_usd / (position.exit_filled_quantity * MULTIPLIER)
            position.exit_order_filled_quantity = filled
            if position.exit_filled_quantity == position.filled_quantity and position.filled_quantity > 0:
                position.status = "CLOSED"
                position.exit_time = parse_time(order.get("filled_at") or order.get("updated_at") or now)
                position.exit_spot = position.last_spot
                position.realized_pnl = position.exit_proceeds_usd - position.entry_cost
                position.realized_roi_pct = (position.exit_option_price / position.entry_option_price - 1) * 100
                position.peak_roi_pct = max(position.peak_roi_pct, position.realized_roi_pct)
                position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, position.realized_roi_pct)
                position.direction_correct = position.peak_roi_pct >= 15.0
                position.hold_reason = None
                LOGGER.info("Paper券商已完整成交平仓%s，PnL$%.2f（未含未知费用），原因%s", position.contract_symbol, position.realized_pnl, position.exit_reason)
            elif state in TERMINAL:
                # 已确认旧卖单结束，后续poll才允许新的sell_to_close。
                position.status = "OPEN"
                position.exit_order_id = None
                position.exit_client_order_id = None
                position.exit_cancel_requested = False
                position.exit_order_filled_quantity = 0.0
                position.exit_order_notional_usd = 0.0
                position.hold_reason = "上次出场订单未成交且已终止，待下一轮核对后重试退出"
            else:
                position.status = "PENDING_EXIT"
        position.last_reconciled_at = now
        # Preserve the validated response before terminal exits clear per-order fields.
        client_id = str(order["client_order_id"])
        order_id = str(order["id"])
        if previous_order_id != order_id and state not in {"rejected", "failed", "voided", "canceled", "cancelled", "expired"}:
            self._queue_order_notice(position, "submitted", now, entry=entry,
                                     client_id=client_id, order_id=order_id, requested=maximum_fill,
                                     filled=filled, price=position.entry_limit_price if entry else None)
        if filled > previous_fill:
            self._queue_order_notice(position, "filled", now, entry=entry,
                                     client_id=client_id, order_id=order_id, requested=maximum_fill,
                                     filled=filled, price=float(order["filled_avg_price"]))
        if state != previous_state or previous_order_id != order_id:
            if state in {"rejected", "failed", "voided"}:
                kind = "rejected"
            elif state in {"canceled", "cancelled", "expired"}:
                kind = "cancelled"
            else:
                kind = None
            if kind:
                self._queue_order_notice(position, kind, now, entry=entry,
                                         client_id=client_id, order_id=order_id,
                                         requested=maximum_fill, filled=filled)

    def _find_order(self, position: AlpacaPosition, *, entry: bool) -> dict[str, Any] | None:
        order_id = position.entry_order_id if entry else position.exit_order_id
        client_id = position.entry_client_order_id if entry else position.exit_client_order_id
        for order in self._remote_orders:
            if not isinstance(order, dict):
                continue
            if ((order_id and str(order.get("id", "")) == order_id)
                    or (client_id and str(order.get("client_order_id", "")) == client_id)):
                if str(order.get("status", "")).lower() not in TERMINAL:
                    return order
                break
        if order_id:
            result = self.client.get_order(order_id)
            if result is not None:
                return result
        return self.client.get_order_by_client_id(client_id) if client_id else None

    def _cancel_entry(self, position: AlpacaPosition) -> None:
        if not self.execute_paper or not position.entry_order_id or position.entry_cancel_requested or position.entry_order_state in TERMINAL:
            return
        position.entry_cancel_requested = True
        self._persist()
        try:
            self.client.cancel_order(position.entry_order_id)
            LOGGER.info("Paper入场撤单已请求%s；须等待终态，不能当作已撤销", position.contract_symbol)
        except (AlpacaError, SubmissionUnknown) as exc:
            self._network_ok = False
            LOGGER.warning("入场撤单响应异常；保留预算等待GET终态：%s", exc)

    def _request_exit(self, position: AlpacaPosition, now: datetime, reason: str) -> None:
        if position.status != "OPEN" or position.remaining_quantity <= 0:
            return
        position.exit_reason = reason
        if not self.execute_paper:
            self._blocks.add("已有Paper仓位但未启用执行，需人工管理退出")
            LOGGER.warning("仅预览退出%s原因%s；未提交卖单", position.contract_symbol, reason)
            return
        # 从券商确认可卖的long数量；不使用本地假定仓位卖出，不触碰旧股票。
        self._assert_paper()
        account = self.client.get_account()
        if not isinstance(account, dict) or str(account.get("id", "")) != self.account_id:
            raise AlpacaLedgerError("出场前发现账户与账本绑定不一致")
        remote = self.client.get_positions()
        remote_orders = self.client.get_orders(status="open")
        now = self._current_time()
        if not isinstance(remote, list) or not isinstance(remote_orders, list):
            raise ValueError("出场前远程持仓/挂单响应无效")
        self._remote_orders = remote_orders
        matching = next((item for item in remote if isinstance(item, dict) and symbol_of(item.get("symbol")) == position.contract_symbol), None)
        held = number(matching.get("qty", 0), "可卖期权数量") if matching else 0.0
        same_contract_orders = [item for item in remote_orders if isinstance(item, dict) and symbol_of(item.get("symbol")) == position.contract_symbol]
        if (not matching or matching.get("side") != "long" or matching.get("asset_class") != "us_option"
                or held != position.remaining_quantity or held <= 0 or same_contract_orders):
            position.hold_reason = "远程标准long数量未精确匹配或该合约仍有挂单；拒绝叠加卖单，需继续对账"
            self._blocks.add(position.hold_reason)
            LOGGER.error("%s %s", position.contract_symbol, position.hold_reason)
            return
        identifier = "yhs-opt-exit-" + uuid.uuid4().hex
        position.exit_client_order_id = identifier
        position.exit_requested_at = now
        position.exit_order_filled_quantity = 0.0
        position.exit_order_notional_usd = 0.0
        position.status = "EXIT_INTENT"
        self._persist()
        payload = {"symbol": position.contract_symbol, "qty": str(int(position.remaining_quantity)), "side": "sell", "position_intent": "sell_to_close", "type": "market", "time_in_force": "day", "extended_hours": False, "client_order_id": identifier}
        try:
            order = self.client.submit_order(payload)
            self._apply_order(position, order, self._current_time(), entry=False)
        except SubmissionUnknown as exc:
            position.status = "EXIT_UNKNOWN"
            position.hold_reason = "退出提交响应未知，按client_order_id对账；不重复卖出"
            self._network_ok = False
            self._blocks.add("退出提交未知")
            LOGGER.error("%s：%s", position.hold_reason, exc)
            self._queue_order_notice(position, "unknown", now, entry=False,
                                     client_id=identifier, requested=int(payload["qty"]))
        except AlpacaError as exc:
            position.status = "OPEN"
            position.exit_order_id = None
            position.exit_client_order_id = None
            position.hold_reason = "出场明确拒绝，须下一轮重新核对仓位"
            LOGGER.error("Paper出场订单拒绝：%s", exc)
            self._queue_order_notice(position, "rejected", now, entry=False,
                                     client_id=identifier, requested=int(payload["qty"]))
        except (ValueError, TypeError, KeyError):
            self._queue_order_notice(position, "unknown", now, entry=False,
                                     client_id=identifier, requested=int(payload["qty"]))
            raise
        self._persist()

    def _mark_quote(self, position: AlpacaPosition, now: datetime, raw: Any) -> str | None:
        try:
            now = self._current_time()
            quote = self._quote(raw, now, entry=False)
            if position.last_time is not None and age(quote["quoted_at"], position.last_time) < 0:
                raise ValueError("期权报价时间倒退")
            position.last_mark, position.last_time = quote["bid"], quote["quoted_at"]
            position.stale = False
            roi = position.roi_pct / 100
            position.peak_roi_pct = max(position.peak_roi_pct, roi * 100)
            position.max_adverse_roi_pct = min(position.max_adverse_roi_pct, roi * 100)
            peak = position.peak_roi_pct / 100
            reason = "HARD_STOP"
            if peak >= self.policy.breakeven_trigger_roi:
                position.stop_roi = max(position.stop_roi, 0.0)
                reason = "BREAKEVEN_STOP"
            if peak >= self.policy.trailing_trigger_roi:
                trailing = ((1 + peak) * (1 - self.policy.trailing_drawdown) - 1
                            if self.policy.trailing_mode == "premium" else peak - self.policy.trailing_drawdown)
                position.stop_roi = max(position.stop_roi, trailing)
                reason = "TRAILING_STOP"
            if roi >= self.policy.hard_take_profit_roi:
                return "HARD_TAKE_PROFIT"
            return reason if roi <= position.stop_roi else None
        except (AlpacaError, ValueError, TypeError) as exc:
            position.stale = True
            self._network_ok = False
            self._blocks.add("持仓期权报价不可用，停止新开仓")
            LOGGER.warning("%s无法刷新期权估值：%s；程序止损暂不能判断，日终仍尝试核对后卖出", position.contract_symbol, exc)
            return None

    @synchronized
    def poll(self, now: datetime, clock: dict[str, Any], *, allow_new_entries: bool = True) -> list[AlpacaPosition]:
        parse_time(now)
        now = self._current_time()
        previously_closed = {item.position_id for item in self.closed_positions}
        self._allow_new_entries = bool(allow_new_entries)
        self._blocks = set()
        self._network_ok = True
        self._clock = clock
        try:
            self._assert_paper()
            self._refresh_account_and_exposure(now)
            now = self._current_time()
            flatten = now >= self._flatten_at(now, clock)
            if flatten:
                self._blocks.add("已到提前清仓时间")
            for position in list(self.open_positions):
                now = self._current_time()
                flatten = now >= self._flatten_at(now, clock)
                if flatten or self._entry_stop.is_set():
                    position.exit_reason = "SESSION_CLOSE" if flatten else position.exit_reason or "RUNNER_STOP"
                entry_pending = position.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY"}
                exit_pending = position.status in {"EXIT_INTENT", "EXIT_UNKNOWN", "PENDING_EXIT"}
                if entry_pending or exit_pending:
                    order = self._find_order(position, entry=entry_pending)
                    now = self._current_time()
                    flatten = now >= self._flatten_at(now, clock)
                    if order is None:
                        position.hold_reason = "查询尚未找到订单，保持未知状态和预算；不会重提"
                        self._blocks.add("未知订单尚未对账")
                        submitted_at = position.intended_at if entry_pending else position.exit_requested_at
                        if submitted_at and age(now, submitted_at) >= self.policy.order_timeout_seconds:
                            LOGGER.error("%s未知订单超过等待期限；停止新仓并需人工核对订单/仓位", position.contract_symbol)
                        continue
                    self._apply_order(position, order, now, entry=entry_pending)
                    if entry_pending and position.status == "PENDING_ENTRY" and (position.filled_quantity > 0 or flatten or self._entry_stop.is_set() or position.exit_reason or age(now, position.intended_at) >= self.policy.order_timeout_seconds):
                        self._cancel_entry(position)
                    if exit_pending and position.status == "PENDING_EXIT" and position.exit_requested_at and age(now, position.exit_requested_at) >= self.policy.order_timeout_seconds:
                        self._blocks.add("出场订单超时未终态，禁止重复卖出")
                        LOGGER.error("%s退出挂单超时，等待券商终态；不能假定已平仓", position.contract_symbol)

            # 先对账本轮成交，再一次取全部实际在仓合约，新增部分成交也包含在批次中。
            symbols = sorted({item.contract_symbol for item in self.open_positions if item.remaining_quantity > 0})
            self._quote_batch_symbols = len(symbols)
            quote_cache: dict[str, Any] = {}
            if symbols:
                try:
                    quotes = self.client.get_option_quotes(symbols, feed=self.policy.feed)
                    if not isinstance(quotes, dict):
                        raise ValueError("批量期权报价响应不是字典")
                    quote_cache = {symbol_of(key): value for key, value in quotes.items()}
                except (AlpacaError, ValueError, TypeError) as exc:
                    self._network_ok = False
                    self._blocks.add("批量持仓期权报价不可用，停止新开仓")
                    LOGGER.error("批量期权报价不可用：%s；不逐合约补请求，清仓仍按核对后的long数量尝试", exc)
            self._quote_batch_missing = sum(not isinstance(quote_cache.get(symbol), dict) for symbol in symbols)

            for position in list(self.open_positions):
                now = self._current_time()
                flatten = now >= self._flatten_at(now, clock)
                if flatten or self._entry_stop.is_set():
                    position.exit_reason = "SESSION_CLOSE" if flatten else position.exit_reason or "RUNNER_STOP"
                raw_quote = quote_cache.get(position.contract_symbol)
                if position.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY"}:
                    if position.remaining_quantity > 0:
                        trigger = self._mark_quote(position, now, raw_quote)
                        if trigger:
                            position.exit_reason = position.exit_reason or trigger
                        self._blocks.add("部分买入成交，等待剩余入场撤单终态后退出")
                    continue
                if position.status in {"EXIT_INTENT", "EXIT_UNKNOWN", "PENDING_EXIT"}:
                    if position.remaining_quantity > 0:
                        self._mark_quote(position, now, raw_quote)
                    continue
                if position.status == "HOLD":
                    self._blocks.add(position.hold_reason or "持仓需要人工核对")
                    if position.remaining_quantity > 0:
                        self._mark_quote(position, now, raw_quote)
                    continue
                if position.status == "OPEN":
                    remote = self._remote_positions.get(position.contract_symbol)
                    remote_qty = number(remote.get("qty", 0), "远程期权仓位") if remote else 0.0
                    if remote_qty < 0 or (remote and (remote.get("asset_class") != "us_option" or remote.get("side") != "long")):
                        position.status = "HOLD"
                        position.hold_reason = "远程期权数量与本策略不一致，停止操作该合约"
                        self._blocks.add(position.hold_reason)
                        continue
                    if remote_qty != position.remaining_quantity:
                        self._blocks.add("持仓同步未完成或被外部修改")
                        synchronization_since = position.last_fill_observed_at or position.entry_time
                        if age(now, synchronization_since) >= self.policy.reconciliation_grace_seconds:
                            position.status = "HOLD"
                            position.hold_reason = "券商数量持续不一致，可能有外部操作；不能虚构成交或PnL"
                        self._mark_quote(position, now, raw_quote)
                        continue
                    trigger = self._mark_quote(position, now, raw_quote)
                    now = self._current_time()
                    flatten = now >= self._flatten_at(now, clock)
                    if flatten or self._entry_stop.is_set() or position.entry_time.astimezone(eastern_timezone()).date() < now.astimezone(eastern_timezone()).date():
                        if flatten:
                            exit_reason = "SESSION_CLOSE"
                        elif self._entry_stop.is_set():
                            exit_reason = position.exit_reason or "RUNNER_STOP"
                        else:
                            exit_reason = "OVERNIGHT_CARRY"
                        self._request_exit(position, now, exit_reason)
                    elif trigger or position.exit_reason:
                        self._request_exit(position, now, trigger or position.exit_reason)
            now = self._current_time()
            self._apply_risk_blocks(now)
            if any(value.startswith("每日亏损熔断:") or value == "累计试验亏损熔断" for value in self._latched_halts):
                for position in list(self.open_positions):
                    if position.status == "OPEN":
                        self._request_exit(position, now, "RISK_LIMIT")
                    elif position.status == "PENDING_ENTRY":
                        self._cancel_entry(position)
            self._persist()
        except AlpacaLedgerError:
            self._entries_enabled = False
            raise
        except (AlpacaError, SubmissionUnknown, ValueError, TypeError, KeyError) as exc:
            self._network_ok = False
            self._blocks.add("券商失联或对账异常，停止新开仓")
            for position in self.open_positions:
                position.stale = True
            LOGGER.error("Alpaca对账失败：%s；已有仓位未被假定平仓，恢复后继续核对。需要时人工进入Paper账户清仓", exc)
            self._persist()
        return [item for item in self.closed_positions if item.position_id not in previously_closed]

    @synchronized
    def mark_to_market(self, ticker: str, spot: float, now: datetime, *, observed_at: datetime | None = None) -> list[AlpacaPosition]:
        # main的YHS采样只补记录标的现价；真实Paper退出必须由poll读取期权bid。
        stamp = parse_time(observed_at if observed_at is not None else now)
        if age(now, stamp) < 0:
            return []
        value = number(spot, "标的现价", positive=True)
        changed = False
        for position in self.open_positions:
            if position.ticker == ticker.upper() and (position.last_spot_time is None or age(stamp, position.last_spot_time) >= 0):
                position.last_spot, position.last_spot_time = value, stamp
                changed = True
        if changed:
            self._persist()
        return []

    @synchronized
    def close_all(self, now: datetime, reason: str = "SESSION_CLOSE") -> list[AlpacaPosition]:
        parse_time(now)
        now = self._current_time()
        closed_before = {item.position_id for item in self.closed_positions}
        active = list(self.open_positions)
        if active:
            # close_all可能在本轮poll之外调用，不能使用旧open-orders缓存。
            self._remote_orders = []
            try:
                account = self.client.get_account()
                if not isinstance(account, dict) or str(account.get("id", "")) != self.account_id:
                    raise AlpacaLedgerError("清仓账户与本地账本绑定不一致")
                orders = self.client.get_orders(status="open")
                if not isinstance(orders, list):
                    raise ValueError("清仓前open-orders响应无效")
                self._remote_orders = orders
            except (AlpacaError, ValueError, TypeError) as exc:
                self._network_ok = False
                self._blocks.add("清仓前远程挂单无法核对，需恢复后继续")
                for position in active:
                    position.exit_reason = reason
                    position.stale = True
                LOGGER.error("清仓前对账不可用：%s；保留退出意图，不撤/卖未核实订单", exc)
                self._persist()
                return []
        for position in list(self.open_positions):
            position.exit_reason = reason
            try:
                if position.status == "OPEN":
                    self._request_exit(position, now, reason)
                elif position.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY"}:
                    order = self._find_order(position, entry=True)
                    if order:
                        self._apply_order(position, order, now, entry=True)
                        if position.status == "OPEN":
                            self._request_exit(position, now, reason)
                        elif position.status == "PENDING_ENTRY":
                            self._cancel_entry(position)
                # 已有退出单或未知退出保持等待，不重复sell_to_close。
            except (AlpacaError, SubmissionUnknown, ValueError, TypeError) as exc:
                self._network_ok = False
                self._blocks.add("清仓尚未完成，需恢复对账或人工处理")
                position.stale = True
                LOGGER.error("%s清仓未完成：%s；不会写虚构平仓CSV", position.contract_symbol, exc)
        self._persist()
        return [item for item in self.closed_positions if item.position_id not in closed_before]

    @synchronized
    def export_trades_csv(self, path: str | Path | None = None) -> Path:
        destination = Path(path) if path is not None else self.output_dir / "trades_history.csv"
        def write(stream: Any) -> None:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for position in self.closed_positions:
                row = asdict(position)
                row["requested_quantity"] = position.quantity
                row["quantity"] = int(position.filled_quantity)
                writer.writerow(serialized(row))
        self._atomic_write(destination, write, csv_file=True)
        return destination

    def _closed_performance(self, trades: list[AlpacaPosition], *, include_curve: bool = False) -> dict[str, Any]:
        # 合约张数不是独立信号样本；胜率按完整平仓交易记录计算。
        count = len(trades)
        profits = [number(item.realized_pnl, "已平仓PnL") for item in trades]
        wins = [value for value in profits if value > 1e-8]
        losses = [value for value in profits if value < -1e-8]
        gross_profit, gross_loss = sum(wins), -sum(losses)
        performance = {
            "total_trades": count, "winning_trades": len(wins), "losing_trades": len(losses),
            "breakeven_trades": count - len(wins) - len(losses),
            "realized_win_rate_pct": len(wins) / count * 100 if count else 0.0,
            "avg_realized_roi_pct": sum(item.realized_roi_pct or 0.0 for item in trades) / count if count else 0.0,
            "avg_realized_pnl_usd": sum(profits) / count if count else 0.0,
            "avg_win_pnl_usd": gross_profit / len(wins) if wins else None,
            "avg_loss_pnl_usd": sum(losses) / len(losses) if losses else None,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
            "profit_factor_status": "defined" if gross_loss > 0 else "no_closed_trades" if not count else "no_losing_trades",
            "closed_realized_pnl": sum(profits), "sample_unit": "closed_signal_trade",
            "sample_independence_assumed": False,
            "performance_basis": "complete_closed_broker_paper_trades_gross_excludes_open_positions_and_fees",
        }
        if include_curve:
            curve = peak = self.initial_cash
            max_drawdown_usd = max_drawdown_pct = 0.0
            for trade in sorted(trades, key=lambda item: item.exit_time or item.intended_at):
                curve += trade.realized_pnl or 0.0
                peak = max(peak, curve)
                drawdown = peak - curve
                max_drawdown_usd = max(max_drawdown_usd, drawdown)
                max_drawdown_pct = max(max_drawdown_pct, drawdown / peak * 100 if peak > 0 else 0.0)
            performance.update({
                "closed_trade_curve_max_drawdown_usd": max_drawdown_usd,
                "closed_trade_curve_max_drawdown_pct": max_drawdown_pct,
                "drawdown_basis": "initial_strategy_budget_plus_complete_closed_trade_pnl_excludes_open_positions_and_fees",
            })
        return performance

    @synchronized
    def statistics(self) -> dict[str, Any]:
        closed = self.closed_positions
        active = self.open_positions
        realized = sum(item.realized_pnl or 0.0 for item in closed) + sum(item.partial_realized_pnl for item in active)
        unrealized = sum((item.last_mark - item.entry_option_price) * item.remaining_quantity * MULTIPLIER
                         for item in active if item.last_mark is not None and item.entry_option_price > 0)
        fee_reserve = sum(item.fee_reserve_usd for item in self.positions if item.filled_quantity > 0)
        cash = self.initial_cash + realized - sum(item.entry_option_price * item.remaining_quantity * MULTIPLIER for item in active)
        latest = self._clock.get("timestamp") if self._clock else None
        try:
            now = parse_time(latest) if latest else datetime.now(timezone.utc)
        except ValueError:
            now = datetime.now(timezone.utc)
        daily_risk, cumulative_risk = self._pnl(now)
        performance = self._closed_performance(closed, include_curve=True)
        performance["by_ticker"] = {ticker: self._closed_performance([item for item in closed if item.ticker == ticker]) for ticker in sorted({item.ticker for item in closed})}
        performance["by_action"] = {action: self._closed_performance([item for item in closed if item.action == action]) for action in sorted({item.action for item in closed})}
        pending_entry_contracts = int(sum(item.quantity - item.filled_quantity for item in active if item.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY"}))
        pending_exit_contracts = int(sum(item.remaining_quantity for item in active if item.status in {"EXIT_INTENT", "EXIT_UNKNOWN", "PENDING_EXIT"}))
        return {
            **performance,
            "total_trades": len(closed), "open_positions": sum(item.remaining_quantity > 0 for item in active),
            "reserved_slots": len(active), "pending_orders": sum(item.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "PENDING_ENTRY", "EXIT_INTENT", "EXIT_UNKNOWN", "PENDING_EXIT"} for item in active),
            "unknown_orders": sum(item.status in {"ENTRY_INTENT", "ENTRY_UNKNOWN", "EXIT_INTENT", "EXIT_UNKNOWN"} for item in active),
            "total_filled_contracts": int(sum(item.filled_quantity for item in self.positions)),
            "open_contracts": int(sum(item.remaining_quantity for item in active)),
            "pending_contracts": pending_entry_contracts + pending_exit_contracts,
            "pending_entry_contracts": pending_entry_contracts,
            "pending_exit_contracts": pending_exit_contracts,
            "hold_positions": sum(item.status == "HOLD" for item in active),
            "realized_pnl": realized, "unrealized_pnl": unrealized,
            "directional_accuracy_pct": 100 * sum(item.peak_roi_pct >= 15 for item in closed) / len(closed) if closed else 0.0,
            "avg_peak_mfe_pct": sum(item.peak_roi_pct for item in closed) / len(closed) if closed else 0.0,
            "initial_cash": self.initial_cash, "cash": cash, "equity": self.initial_cash + realized + unrealized,
            "strategy_budget_usd": self.initial_cash, "reserved_debit_usd": sum(item.reserved_debit for item in active),
            "fee_reserve_usd": fee_reserve, "fees_are_estimates": True, "pnl_basis": "broker_paper_fills_gross_excludes_fees",
            "daily_risk_pnl_estimate": daily_risk, "cumulative_risk_pnl_estimate": cumulative_risk,
            "broker": "alpaca", "state_mode": "alpaca_paper", "execute_paper": self.execute_paper,
            "allowed_symbols": list(self.policy.allowed_symbols) if self.policy.allowed_symbols is not None else None,
            "universe_source": "original_yhs_filtered_universe" if self.policy.allowed_symbols is None else "explicit_symbols_with_original_yhs_filters",
            "max_open_positions": self.policy.max_open_positions, "max_daily_entries": self.policy.max_daily_entries,
            "max_contracts_per_trade": self.policy.max_contracts_per_trade, "target_premium_fraction": self.policy.target_premium_fraction,
            "quote_fetch_mode": "one_batch_per_poll_no_per_contract_fallback",
            "quote_batch_symbols": self._quote_batch_symbols, "quote_batch_missing": self._quote_batch_missing,
            "feed": self.policy.feed, "price_is_estimate": self.policy.feed == "indicative",
            "valuation_source": "alpaca_indicative_bid_estimate" if self.policy.feed == "indicative" else "alpaca_opra_bid",
            "program_monitor_required": True, "broker_protective_orders": False,
            "valuation_stale": any(item.stale for item in active if item.remaining_quantity > 0),
            "network_ok": self._network_ok, "entries_enabled": not self._entry_stop.is_set() and self._entries_enabled and self._allow_new_entries and self._network_ok and not self._blocks and not self._latched_halts,
            "entry_blocks": sorted(self._blocks | self._latched_halts), "previews": self._preview_count,
            "unmanaged_positions": self._unmanaged_positions, "unmanaged_orders": self._unmanaged_orders,
            "shared_stock_positions": sum(item.get("asset_class") != "us_option" and not OCC.fullmatch(symbol) for symbol, item in self._remote_positions.items()),
            "positions_status": [{"ticker": item.ticker, "contract_symbol": item.contract_symbol, "status": item.status, "requested_quantity": item.quantity, "filled_quantity": item.filled_quantity, "remaining_quantity": item.remaining_quantity, "hold_reason": item.hold_reason} for item in active],
        }
