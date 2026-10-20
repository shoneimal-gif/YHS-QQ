"""YHS 本地模拟/回放，或显式启用 Alpaca Paper 适配；禁止实盘接口。"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import logging
import os
import signal as os_signal
import sys
import threading
import time
import uuid
from dataclasses import asdict, replace
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Iterator

from config import SystemConfig, eastern_timezone, load_config
from detector import Candidate, DataContractError, SignalDetector, select_entry_quote, snapshot_observation
from mock_replay import MockYHSClient, iter_replay_times, replay_close_time
from paper_trader import PaperStateError, PaperTrader
from yhs_client import APIError, AuthenticationError, QuotaExceeded, RequestCancelled, YHSClient

LOGGER = logging.getLogger(__name__)
BROKER_POLL_SECONDS = 5.0
BROKER_RECONCILE_SECONDS = 60.0


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextlib.contextmanager
def runner_lock(output_dir: Path) -> Iterator[None]:
    """同一账本只允许一个监控进程；OS锁在崩溃后自动释放。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "runner.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("同一输出目录已有运行进程；不能并发写同一模拟账本") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_event(path: Path | None, data: dict[str, Any]) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def new_session(day: date) -> dict[str, Any]:
    return {"version": 1, "session_date": day.isoformat(), "volume_claimed": False,
            "pool": [], "flow_calls": 0, "chain_calls": 0, "processed_samples": {}}


def daily_universe(config: SystemConfig, client: Any, day: date) -> Any:
    """共用额度文件的模式/输出目录只领取一次榜单，不以换目录重置每日claim。"""
    quota_path = Path(config.quota_state_path)
    directory = quota_path.parent / "daily_universe" / day.isoformat()
    claim_path = directory / "claim.json"
    with runner_lock(directory):
        if claim_path.exists():
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            if claim.get("version") != 1 or claim.get("session_date") != day.isoformat() or claim.get("claimed") is not True:
                raise DataContractError("共享每日榜单claim损坏；不能重置后重复扣额")
            if "payload" in claim:
                LOGGER.info("读取共享每日榜单缓存；本模式不再次请求榜单")
                return claim["payload"]
            LOGGER.error("共享每日榜单已领取但原始响应未保存；本日禁止重抓，保持空标的池")
            return {"data": []}
        claim = {"version": 1, "session_date": day.isoformat(), "claimed": True}
        atomic_json(claim_path, claim)
        universe = client.fetch_volume_top(config.volume_top_limit)
        atomic_json(claim_path, {**claim, "payload": universe})
        return universe


def process_frame(client: Any, detector: SignalDetector, trader: PaperTrader,
                  candidate: Candidate, heatmap: Any, now: datetime, session: dict[str, Any], *,
                  offline: bool, flow: Any = None, chain: Any = None,
                  can_fetch: bool = True, now_fn: Any = None, session_close: datetime | None = None,
                  persist_session: Any = None, entry_guard: Any = None) -> dict[str, Any]:
    """持仓盯市先执行；仅结构有效且无重复信号时请求Flow和一次链。"""
    config = detector.config
    frame = {"kind": "frame", "time": now.isoformat(), "ticker": candidate.ticker, "heatmap": heatmap}
    if session_close and now >= session_close:
        return frame
    _, spot, sample_time = snapshot_observation(heatmap, candidate.ticker, now, config)
    previous = session["processed_samples"].get(candidate.ticker)
    if previous and datetime.fromisoformat(previous) >= sample_time:
        return frame
    # 盯市采用实际观测时间，旧样本不伪装成新行情；不依赖GEX节点是否完整。
    trader.mark_to_market(candidate.ticker, spot, now, observed_at=sample_time)
    session["processed_samples"][candidate.ticker] = sample_time.isoformat()
    if any(position.ticker == candidate.ticker for position in trader.open_positions):
        return frame
    if len(trader.open_positions) >= config.max_open_positions:
        return frame
    try:
        structure = detector.inspect_structure(candidate, heatmap, now)
    except DataContractError as exc:
        LOGGER.debug("%s 无可用结构：%s", candidate.ticker, exc)
        return frame
    if structure is None or any(position.signal_id == structure.signal_id for position in trader.trades):
        return frame
    if entry_guard and not entry_guard(now):
        return frame
    if flow is None and can_fetch:
        if session["flow_calls"] >= config.daily_flow_request_cap:
            LOGGER.info("日内Flow预算已用完，跳过新增确认")
            return frame
        session["flow_calls"] += 1
        if persist_session:
            persist_session()
        flow = client.fetch_flow(candidate.ticker, config.min_flow_premium_usd)
    if flow is None:
        return frame
    frame["flow"] = flow
    current = now_fn() if now_fn else now
    frame["flow_received_at"] = current.isoformat()
    if session_close and current >= session_close:
        return frame
    if entry_guard and not entry_guard(current):
        return frame
    confirmed = detector.evaluate_full_signal(candidate, heatmap, flow, current, structure)
    if confirmed is None:
        return frame
    if chain is None and can_fetch:
        if session["chain_calls"] >= config.daily_chain_request_cap:
            LOGGER.info("日内开仓链预算已用完，跳过新增纸面仓位")
            return frame
        session["chain_calls"] += 1
        if persist_session:
            persist_session()
        chain = client.fetch_option_chain(candidate.ticker, confirmed.expiration)
    if chain is None:
        return frame
    frame["chain"] = chain
    current = now_fn() if now_fn else now
    frame["chain_received_at"] = current.isoformat()
    if session_close and current >= session_close:
        return frame
    if entry_guard and not entry_guard(current):
        return frame
    snapshot_observation(heatmap, candidate.ticker, current, config)
    quote = select_entry_quote(chain, confirmed, current, config, offline=offline)
    if quote is not None:
        if not offline and quote.get("simulated"):
            raise DataContractError("实时模式不得使用合成合约")
        confirmed.detected_at = current
        if trader.open_position(confirmed, quote, current):
            detector.commit_signal(confirmed)
    return frame


def save_results(config: SystemConfig, trader: PaperTrader, *, mode: str,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    csv_path = trader.export_trades_csv(config.output_dir / "trades_history.csv")
    statistics = trader.statistics()
    broker_backed = mode == "alpaca_paper"
    report = {"mode": mode, "paper_only": True,
              "valuation_model": "alpaca_option_quotes_and_confirmed_fills" if broker_backed else "fixed_entry_delta_gamma",
              "valuation_source": "alpaca_paper_broker" if broker_backed else "delta_gamma_model",
              "price_is_estimate": True,
              "quote_mark_is_estimate": True,
              "closed_fill_source": "alpaca_confirmed_paper_fills" if broker_backed else "delta_gamma_model",
              "trailing_mode": config.trailing_mode,
              "direction_correct_definition": "peak_roi_pct >= 15",
              **statistics, **(extra or {}), "csv_path": str(csv_path.resolve())}
    atomic_json(config.output_dir / "summary.json", report)
    LOGGER.info("复盘统计：已平仓%d笔，方向正确率%.2f%%，平均Peak MFE %.2f%%，已实现PnL $%.2f，CSV=%s",
                report["total_trades"], report["directional_accuracy_pct"], report["avg_peak_mfe_pct"], report["realized_pnl"], csv_path)
    return report


def run_replay(config: SystemConfig, session_date: date, seed: int = 7,
               replay_file: Path | None = None) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if (config.output_dir / "replay_state.json").exists():
        raise RuntimeError("该目录已有回放账本，请指定新的 --output-dir；不混合重复回放")
    with runner_lock(config.output_dir):
        trader = PaperTrader(config, config.output_dir / "replay_state.json")
        detector = SignalDetector(config)  # 离线回放不需每帧fsync；结尾保存疲劳状态。
        session = new_session(session_date)
        client = MockYHSClient(config, seed)
        steps = 0
        if replay_file:
            universe: Any = None
            frames: list[dict[str, Any]] = []
            with Path(replay_file).open(encoding="utf-8-sig") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("kind") == "universe":
                        if universe is not None:
                            raise DataContractError("回放文件包含多个交易日/榜单；请按单日拆分")
                        universe = event["payload"]
                        session_date = date.fromisoformat(event["session_date"])
                        session = new_session(session_date)
                    elif event.get("kind") == "frame":
                        frames.append(event)
                    else:
                        raise DataContractError(f"回放第{line_number}行类型未知")
            if universe is None:
                raise DataContractError("回放缺少universe记录")
            pool = {candidate.ticker: candidate for candidate in detector.filter_pool(universe)}
            bounds = config.session_bounds(session_date)
            if bounds is None:
                raise DataContractError("采集回放日期为休市/周末；需要正确的session_overrides")
            times: set[str] = set()
            prior: datetime | None = None
            for event in frames:
                now = datetime.fromisoformat(event["time"])
                if now.tzinfo is None or now.astimezone(eastern_timezone()).date() != session_date:
                    raise DataContractError("回放时间缺少时区或跨交易日")
                if not bounds[0] <= now < bounds[1]:
                    raise DataContractError("回放帧位于开盘前或收盘后")
                if prior and now < prior:
                    raise DataContractError("回放必须按采集时间递增，不自动排序未来信息")
                times.add(event["time"])
                candidate = pool.get(event["ticker"])
                if candidate:
                    stage_times = [datetime.fromisoformat(event.get("flow_received_at", event["time"])),
                                   datetime.fromisoformat(event.get("chain_received_at", event.get("flow_received_at", event["time"])))]
                    if any(stage.tzinfo is None for stage in stage_times) or not now <= stage_times[0] <= stage_times[1]:
                        raise DataContractError("回放的Flow/链阶段时间必须带时区且不早于Heatmap接收时间")
                    prior = stage_times[-1]
                    progression = iter(stage_times)
                    process_frame(client, detector, trader, candidate, event["heatmap"], now, session,
                                  offline=True, flow=event.get("flow"), chain=event.get("chain"), can_fetch=False,
                                  now_fn=lambda: next(progression), session_close=bounds[1])
                else:
                    prior = now
            steps = len(times)
            source = "recorded_yhs_jsonl"
            simulated = (isinstance(universe, dict) and universe.get("simulated") is True) or any(
                isinstance(event.get(key), dict) and event[key].get("simulated") is True
                for event in frames for key in ("heatmap", "flow", "chain"))
        else:
            client.set_time(next(iter_replay_times(session_date, config)))
            universe = client.fetch_volume_top(config.volume_top_limit)
            pool = detector.filter_pool(universe)
            session["pool"] = [asdict(candidate) for candidate in pool]
            for now in iter_replay_times(session_date, config):
                client.set_time(now)
                for candidate in pool:
                    process_frame(client, detector, trader, candidate, client.fetch_heatmap(candidate.ticker),
                                  now, session, offline=True)
                steps += 1
            source, simulated = "deterministic_synthetic_fixture", True
        bounds = config.session_bounds(session_date)
        close = bounds[1] if replay_file and bounds else replay_close_time(session_date, config)
        trader.close_all(close)
        detector.state_path = config.output_dir / "detector_state.json"
        detector.save()
        return save_results(config, trader, mode="replay", extra={"source": source, "simulated_data": simulated,
                "session_date": session_date.isoformat(), "observation_steps": steps,
                "api_quota_used": 0, "mock_request_counts": client.calls if not replay_file else {},
                "daily_budget_upper_bound": config.daily_budget(session_date)})


def remaining_weekdays(day: date) -> int:
    end = calendar.monthrange(day.year, day.month)[1]
    return max(1, sum(1 for d in range(day.day, end + 1) if date(day.year, day.month, d).weekday() < 5))


def fit_pool_budget(pool: list[Candidate], config: SystemConfig, client: YHSClient, day: date) -> list[Candidate]:
    status = client.budget_status()
    daily_share = status["normal_available"] // remaining_weekdays(day)
    overhead = 1 + config.daily_flow_request_cap + config.daily_chain_request_cap
    slots = max(1, len(config.polling_slots(day)))
    allowed = max(0, min(len(pool), (daily_share - overhead) // slots))
    if allowed < len(pool):
        LOGGER.warning("月度安全预算压缩标的池：%d->%d只，日预算%d，剩余工作日%d", len(pool), allowed, daily_share, remaining_weekdays(day))
    return pool[:allowed]


def _broker_has_pending(trader: Any, statistics: dict[str, Any]) -> bool:
    if trader.open_positions:
        return True
    for key in ("pending_orders", "pending_order_count", "open_orders", "open_order_count",
                "unmanaged_positions", "unmanaged_orders", "submission_unknown_count",
                "unknown_orders", "hold_positions", "reserved_slots"):
        value = statistics.get(key)
        if value:
            return True
    return False


def _broker_symbol_policy(trader: Any) -> frozenset[str] | None:
    """None不增加标的限制；显式白名单仍只作为原YHS过滤池的子集。"""
    symbols = trader.policy.allowed_symbols
    return None if symbols is None else frozenset(str(symbol).upper() for symbol in symbols)


def _broker_pool_metadata(trader: Any, pool: list[Candidate] | None = None) -> dict[str, Any]:
    symbols = _broker_symbol_policy(trader)
    return {"allowed_tickers": None if symbols is None else sorted(symbols),
            "candidate_universe_mode": "yhs_filtered_pool" if symbols is None else "explicit_policy_subset",
            "uses_yhs_filtered_pool": True,
            "candidate_pool_loaded": pool is not None,
            "active_pool_tickers": None if pool is None else sorted(candidate.ticker for candidate in pool)}


def _broker_filter_pool(trader: Any, pool: list[Candidate]) -> list[Candidate]:
    symbols = _broker_symbol_policy(trader)
    filtered = pool if symbols is None else [candidate for candidate in pool if candidate.ticker in symbols]
    LOGGER.info("Alpaca Paper标的范围=%s，原YHS筛选后保留%d只",
                "原YHS股票/核心ETF池" if symbols is None else ",".join(sorted(symbols)), len(filtered))
    return filtered


def _broker_entry_window(trader: Any, now: datetime, start: datetime, cutoff: datetime) -> bool:
    if not start <= now < cutoff:
        return False
    windows = getattr(getattr(trader, "policy", None), "entry_windows", ())
    wall = now.astimezone(eastern_timezone()).time().replace(tzinfo=None)
    return any(dt_time.fromisoformat(begin) <= wall < dt_time.fromisoformat(end)
               for begin, end in windows)


def _reconcile_broker_shutdown(trader: Any, broker_client: Any, now_fn: Any,
                               reason: str) -> dict[str, Any]:
    """发送 Paper 退出意图后有限对账，未确认成交的头寸必须保留为未结。"""
    trader.stop_new_entries()
    failures = 0
    try:
        trader.close_all(now_fn(), reason)
    except Exception:
        failures += 1
        LOGGER.exception("Alpaca Paper退出请求未确认；保留可恢复状态，不补造平仓成交")
    deadline = time.monotonic() + BROKER_RECONCILE_SECONDS
    result = {"pending": True, "errors": failures}
    wait = threading.Event()

    def reconcile() -> None:
        while time.monotonic() < deadline:
            try:
                clock = broker_client.get_clock()
                trader.poll(now_fn(), clock, allow_new_entries=False)
                statistics = trader.statistics()
                # poll会把部分网络错误保存在统计中；必须确认本轮远程对账健康。
                healthy = statistics.get("network_ok") is True
                result["pending"] = not healthy or _broker_has_pending(trader, statistics)
                if not healthy:
                    result["errors"] += 1
                if not result["pending"]:
                    return
            except Exception:
                result["errors"] += 1
                LOGGER.exception("Alpaca Paper关停对账失败；待恢复后再次核对远程订单/仓位")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            wait.wait(min(BROKER_POLL_SECONDS, remaining))

    worker = threading.Thread(target=reconcile, name="alpaca-paper-shutdown", daemon=True)
    worker.start()
    worker.join(timeout=BROKER_RECONCILE_SECONDS)
    timed_out = worker.is_alive()
    if timed_out:
        # 取消后续HTTP/限频/重试；已经在途的urllib读取仍受4秒socket超时限制。
        # 不释放账户锁让另一执行器进入，直到对账线程实际结束。
        broker_client.close()
        wait.set()
        LOGGER.warning("60秒对账预算已到；取消后续broker请求，等待已有网络调用退出后保留未结状态")
        worker.join()
    pending = result["pending"]
    if pending:
        LOGGER.warning("关停对账期限已到或仍有未结Paper仓位/订单；不会宣称全部平仓")
    return {"shutdown_reconciled": not pending, "shutdown_pending": pending,
            "shutdown_reconcile_errors": result["errors"],
            "shutdown_reconcile_seconds_limit": BROKER_RECONCILE_SECONDS,
            "shutdown_reconcile_timeout": timed_out,
            "shutdown_inflight_http_may_extend_deadline": timed_out}


def _masked_account(account: dict[str, Any]) -> str:
    identifier = str(account.get("id") or account.get("account_number") or "")
    return "****" + identifier[-4:] if identifier else "unknown"


def _calendar_bound(day: date, value: Any) -> datetime:
    if not isinstance(value, str):
        raise DataContractError("Alpaca交易日历缺少开/收盘时间")
    if "T" in value:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise DataContractError("Alpaca日历绝对时间必须带时区")
        result = result.astimezone(eastern_timezone())
    else:
        result = datetime.combine(day, dt_time.fromisoformat(value), eastern_timezone())
    if result.date() != day:
        raise DataContractError("Alpaca日历时间与交易日不一致")
    return result


def _alpaca_session(config: SystemConfig, broker_client: Any) -> tuple[datetime, dict[str, Any], bool]:
    clock = broker_client.get_clock()
    if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool):
        raise DataContractError("Alpaca Clock缺少明确的is_open状态")
    stamp = datetime.fromisoformat(str(clock.get("timestamp", "")).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise DataContractError("Alpaca Clock必须带时区")
    now = datetime.now(eastern_timezone())
    if abs((now - stamp).total_seconds()) > 60:
        raise DataContractError("本机时钟与Alpaca Clock差异超过60秒；先校准时钟")
    day = stamp.astimezone(eastern_timezone()).date()
    rows = broker_client.get_calendar(day, day)
    match = next((row for row in rows if str(row.get("date", "")) == day.isoformat()), None)
    if match is None:
        config.session_overrides[day.isoformat()] = None
        return now, clock, False
    start, close = _calendar_bound(day, match.get("open")), _calendar_bound(day, match.get("close"))
    if close <= start:
        raise DataContractError("Alpaca日历收盘时间必须晚于开盘")
    config.market_open = start.strftime("%H:%M")
    config.session_overrides[day.isoformat()] = close.strftime("%H:%M")
    return now, clock, True


def _load_alpaca_client(env_file: Path | None) -> Any:
    from alpaca_client import AlpacaPaperClient
    # 每个HTTP请求最多4秒且不自动重试，使失联可尽快进入持久化/恢复流程。
    client = AlpacaPaperClient.from_environment(env_file, timeout_seconds=4, max_get_retries=0)
    if client.paper is not True or client.base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        client.close()
        raise RuntimeError("仅允许Alpaca Paper端点")
    return client


def run_alpaca_check(config: SystemConfig, env_file: Path | None = None) -> dict[str, Any]:
    """仅GET检查；不要求YHS Key，不创建/撤销/替换任何订单。"""
    broker = _load_alpaca_client(env_file)
    try:
        account = broker.get_account()
        clock = broker.get_clock()
        positions, orders = broker.get_positions(), broker.get_orders(status="open")
        today = datetime.now(eastern_timezone()).date()
        calendar_rows = broker.get_calendar(today, today + timedelta(days=7))
        options = sum(1 for position in positions if position.get("asset_class") == "us_option")
        report = {
            "mode": "alpaca_check", "paper": True, "read_only": True,
            "account": _masked_account(account), "status": account.get("status"),
            "equity": account.get("equity"), "cash": account.get("cash"),
            "options_buying_power": account.get("options_buying_power"),
            "options_trading_level": account.get("options_trading_level"),
            "options_approved_level": account.get("options_approved_level"),
            "trading_blocked": account.get("trading_blocked"),
            "account_blocked": account.get("account_blocked"),
            "stock_position_count": len(positions) - options, "option_position_count": options,
            "open_order_count": len(orders), "is_open": clock.get("is_open"),
            "calendar": [{key: row.get(key) for key in ("date", "open", "close")} for row in calendar_rows],
            "program_monitor_required": True,
        }
        atomic_json(config.output_dir / "alpaca_check.json", report)
        return report
    finally:
        broker.close()


def run_alpaca_paper(config: SystemConfig, capture_path: Path | None = None, *,
                     env_file: Path | None = None, policy_path: Path | None = None,
                     execute_paper: bool = False) -> dict[str, Any]:
    """只连接 Paper；未显式--execute-paper时交易适配器不得写订单。"""
    if not config.api_key:
        raise ValueError("--alpaca-paper需要YHS_API_KEY；只读账户检查使用--alpaca-check")
    from alpaca_trader import AlpacaPaperTrader, AlpacaRiskConfig, load_alpaca_policy
    policy = load_alpaca_policy(policy_path) if policy_path else AlpacaRiskConfig()
    broker = _load_alpaca_client(env_file)
    created_traders: list[Any] = []
    created_notifiers: list[Any] = []
    try:
        account = broker.get_account()
        identifier = str(account.get("id") or "")
        if not identifier:
            raise DataContractError("Alpaca账户缺少稳定id，拒绝建立执行账本")
        now, clock, trading_day = _alpaca_session(config, broker)
        # 同一模块安装下，账户状态/OS锁不随--output-dir改变。
        # 不能约束另一主机或旧Autopilot；适配器还须拒绝未管理的远程订单/仓位。
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        account_dir = Path(__file__).resolve().parent / "runtime" / "alpaca_accounts" / digest
        state_path = account_dir / "alpaca_paper_state.json"
        LOGGER.info("Alpaca Paper账户%s，订单写入=%s；程序退出/失联时无法保证风险订单成交", _masked_account(account), execute_paper)
        with runner_lock(account_dir):
            def factory(settings: SystemConfig) -> Any:
                trader = AlpacaPaperTrader(broker, settings, state_path=state_path,
                                           policy=policy, execute_paper=execute_paper)
                created_traders.append(trader)
                if execute_paper:
                    order_sinks: list[Any] = []
                    try:
                        from discord_notifications import DiscordNotifier, load_webhook as load_discord_webhook
                        webhook = load_discord_webhook(Path(__file__).resolve().parent)
                        if webhook:
                            notifier = DiscordNotifier(webhook, account_dir / "discord_outbox.json")
                            created_notifiers.append(notifier)
                            order_sinks.append(notifier.enqueue)
                            LOGGER.info("Discord订单推送已启用；提交、成交及失败状态会分别通知")
                        else:
                            LOGGER.info("未配置Discord Webhook；Discord订单推送未启用")
                    except Exception:
                        LOGGER.warning("Discord配置或通知队列无法加载，Discord推送未启用；不影响Paper账本")
                    if order_sinks:
                        def notify_order(event: dict[str, Any], sinks: tuple[Any, ...] = tuple(order_sinks)) -> bool:
                            accepted = True
                            for sink in sinks:
                                try:
                                    if sink(event) is not True:
                                        accepted = False
                                except Exception:
                                    accepted = False
                            return accepted
                        trader.notification_sink = notify_order
                return trader
            if not trading_day:
                with runner_lock(config.output_dir):
                    trader = factory(config)
                    trader.stop_new_entries()
                    trader.poll(now, clock, allow_new_entries=False)
                    statistics = trader.statistics()
                    return save_results(config, trader, mode="alpaca_paper", extra={
                        "session_date": now.date().isoformat(), "exit_reason": "MARKET_CLOSED",
                        "account": _masked_account(account), "execute_paper": execute_paper,
                        "paper": True, "trading_day": False, "program_monitor_required": True,
                        "failed": statistics.get("network_ok") is not True,
                        "pending_remote_state": (statistics.get("network_ok") is not True or
                                                 _broker_has_pending(trader, statistics)),
                        "account_state_path": str(state_path),
                        "account_lock_scope": "same_host_same_installation",
                        "exclusive_executor_required": True,
                        **_broker_pool_metadata(trader),
                    })
            report = run_live(config, capture_path, trader_factory=factory, broker_client=broker)
            report.update({"account": _masked_account(account), "execute_paper": execute_paper,
                           "paper": True, "program_monitor_required": True,
                           "account_state_path": str(state_path),
                           "account_lock_scope": "same_host_same_installation",
                           "exclusive_executor_required": True})
            atomic_json(config.output_dir / "summary.json", report)
            return report
    finally:
        for notifier in created_notifiers:
            try:
                notifier.close(timeout=5)
            except Exception:
                LOGGER.warning("订单后台通知未完全结束，未发送内容保留供下次恢复")
        for trader in created_traders:
            trader.release_state_lock()
        broker.close()


def run_live(config: SystemConfig, capture_path: Path | None = None, *,
             trader_factory: Any = None, broker_client: Any = None) -> dict[str, Any]:
    if not config.api_key:
        raise ValueError("实时模式需要YHS_API_KEY环境变量；--replay不需要密钥")
    now_fn = lambda: datetime.now(eastern_timezone())
    now = now_fn()
    bounds = config.session_bounds(now.date())
    if bounds is None:
        raise ValueError("今天为休市/周末；请设置session_overrides或选择回放")
    start, close = bounds
    broker_mode = broker_client is not None
    report_mode = "alpaca_paper" if broker_mode else "live_paper"
    entry_cutoff = close - timedelta(minutes=15) if broker_mode else close
    with runner_lock(config.output_dir):
        trader = trader_factory(config) if trader_factory else PaperTrader(config)
        detector = SignalDetector(config, config.output_dir / "detector_state.json")
        pool_metadata = _broker_pool_metadata(trader) if broker_mode else {}
        if broker_mode:
            # 先读取远程真实状态；不可把远程持仓按本地Delta-Gamma价直接平掉。
            trader.poll(now, broker_client.get_clock(), allow_new_entries=False)
            if trader.statistics().get("network_ok") is False:
                trader.stop_new_entries()
                LOGGER.error("启动远程对账未健康，停止YHS新增信号并保留broker恢复对账")
                detail = _reconcile_broker_shutdown(trader, broker_client, now_fn, "BROKER_STARTUP_UNHEALTHY")
                return save_results(config, trader, mode=report_mode, extra={
                    "session_date": now.date().isoformat(), "failed": True,
                    "exit_reason": "BROKER_STARTUP_UNHEALTHY", "program_monitor_required": True,
                    **pool_metadata, **detail})
        elif any(position.entry_time.astimezone(eastern_timezone()).date() < now.date() for position in trader.open_positions):
            LOGGER.warning("发现跨日遗留纸面仓位，按最后观测模型价补记恢复平仓，不伪造隔夜行情")
            trader.close_all(now, "RECOVERY_AFTER_SESSION")
        if now >= close:
            if broker_mode:
                detail = _reconcile_broker_shutdown(trader, broker_client, now_fn, "RECOVERY_AFTER_SESSION")
            else:
                trader.close_all(now, "RECOVERY_AFTER_SESSION")
                detail = {}
            return save_results(config, trader, mode=report_mode, extra={
                "session_date": now.date().isoformat(), "after_close": True,
                "program_monitor_required": broker_mode, **pool_metadata, **detail})
        session_path = config.output_dir / "sessions" / f"{now.date()}.json"
        session = json.loads(session_path.read_text(encoding="utf-8")) if session_path.exists() else new_session(now.date())
        if session.get("version") != 1 or session.get("session_date") != now.date().isoformat():
            raise DataContractError("日内状态损坏")
        client = YHSClient(config)
        stop = threading.Event()
        broker_stop = threading.Event()
        broker_thread: threading.Thread | None = None
        background_errors: list[str] = []

        def stop_requests(*_: Any) -> None:
            stop.set()
            client.close()
            trader.stop_new_entries()

        def broker_monitor() -> None:
            next_poll = time.monotonic()
            while not broker_stop.wait(max(0.0, next_poll - time.monotonic())):
                next_poll = time.monotonic() + BROKER_POLL_SECONDS
                try:
                    observed_now = now_fn()
                    clock = broker_client.get_clock()
                    trader.poll(observed_now, clock, allow_new_entries=(
                        not stop.is_set() and _broker_entry_window(trader, observed_now, start, entry_cutoff)))
                    if trader.statistics().get("network_ok") is False:
                        if not background_errors:
                            background_errors.append("BROKER_MONITOR_UNHEALTHY")
                            LOGGER.error("Alpaca报价/成交状态不健康，停止YHS新增信号；后台及关停阶段继续尝试恢复对账")
                        trader.stop_new_entries()
                        stop_requests()
                except Exception:
                    if not background_errors:
                        background_errors.append("BROKER_MONITOR_ERROR")
                    LOGGER.exception("Alpaca Paper报价/成交后台失联，停止新开仓并取消YHS等待；保留远程订单对账状态")
                    trader.stop_new_entries()
                    stop_requests()
                if time.monotonic() > next_poll:
                    LOGGER.warning("Alpaca后台一轮超过5秒目标；网络/接口延迟期间不能保证程序止损及时执行")

        def session_closed() -> None:
            stop_requests()
            try:
                trader.stop_new_entries()
                trader.close_all(close, "SESSION_CLOSE")
                trader.export_trades_csv(config.output_dir / "trades_history.csv")
                if broker_mode:
                    LOGGER.info("收盘定时器已发起Paper退出；仅券商确认filled才计平仓，pending仓位保留")
                else:
                    LOGGER.info("收盘定时器已按最后有效观测模型价清平全部模拟仓位")
            except Exception:
                LOGGER.exception("收盘定时器账本操作失败；主循环finally将再次尝试")

        timer = threading.Timer(max(0.0, (close - now_fn()).total_seconds()), session_closed)
        timer.daemon = True
        old_sigint = old_sigterm = None
        reason = "SESSION_CLOSE"
        failed = False
        shutdown_detail: dict[str, Any] = {}
        try:
            old_sigint = os_signal.signal(os_signal.SIGINT, stop_requests)
            old_sigterm = os_signal.signal(os_signal.SIGTERM, stop_requests)
            timer.start()
            if broker_mode:
                broker_thread = threading.Thread(target=broker_monitor, name="alpaca-paper-monitor", daemon=True)
                broker_thread.start()
            while now_fn() < start:
                if stop.wait(max(0.0, min(30.0, (start - now_fn()).total_seconds()))):
                    raise RequestCancelled("盘前等待已取消")
            client.discover()  # 免费，读取服务端余量；不以本地估计覆盖服务器数据。
            LOGGER.info("实时YHS启动，美东收盘%s；执行模式=%s", close.isoformat(), report_mode)
            if not session["volume_claimed"]:
                # 请求前先持久化claim，崩溃后不重复消耗每日榜单成功请求。
                session["volume_claimed"] = True
                atomic_json(session_path, session)
                universe = daily_universe(config, client, now.date())
                pool = detector.filter_pool(universe)
                if broker_mode:
                    pool = _broker_filter_pool(trader, pool)
                pool = fit_pool_budget(pool, config, client, now.date())
                session["pool"] = [asdict(candidate) for candidate in pool]
                atomic_json(session_path, session)
                record_event(capture_path, {"kind": "universe", "session_date": now.date().isoformat(), "payload": universe})
            else:
                pool = [Candidate(**row) for row in session["pool"]]
                if broker_mode:
                    pool = _broker_filter_pool(trader, pool)
                LOGGER.info("使用每日缓存%d只标的，禁止再次请求榜单", len(pool))
            # 只允许本日原筛选/预算池入场；遗留持仓补入监控池不会获得新入场资格。
            entry_tickers = frozenset(candidate.ticker for candidate in pool)
            if broker_mode:
                pool_metadata = _broker_pool_metadata(trader, pool)
            if not pool and not trader.open_positions:
                LOGGER.warning("标的池为空，结束本日监控；查系统日志中的字段/筛选原因")
                reason = "EMPTY_UNIVERSE"
            else:
                held = {position.ticker for position in trader.open_positions}
                listed = {candidate.ticker for candidate in pool}
                for ticker in held - listed:
                    pos = next(position for position in trader.open_positions if position.ticker == ticker)
                    pool.append(Candidate(ticker, pos.last_spot, 0, 0, None, None, "NEUTRAL", "stock", 0))
                slots = config.polling_slots(now.date())
                # 启动时立即扫描一次，此后只处理未来固定时槽，不补跑错过的槽。
                pending = [now_fn()] + [slot for slot in slots if slot > now_fn()]
                for planned in pending:
                    wait = (planned - now_fn()).total_seconds()
                    if wait > 0 and stop.wait(wait):
                        break
                    if stop.is_set() or now_fn() >= close:
                        break
                    if now_fn() - planned > timedelta(minutes=config.polling_minutes(planned)):
                        LOGGER.warning("跳过过时轮询槽%s，不突发补拉", planned.isoformat())
                        continue
                    ordered = sorted(pool, key=lambda candidate: not any(position.ticker == candidate.ticker for position in trader.open_positions))
                    for candidate in ordered:
                        if stop.is_set() or now_fn() >= close:
                            break
                        essential = any(position.ticker == candidate.ticker for position in trader.open_positions)
                        if client.budget_status()["normal_available"] <= 0 and not essential:
                            continue
                        try:
                            heatmap = client.fetch_heatmap(candidate.ticker, category="essential" if essential else "normal")
                            frame = process_frame(client, detector, trader, candidate, heatmap, now_fn(), session,
                                                  offline=False, now_fn=now_fn, session_close=entry_cutoff,
                                                  can_fetch=(client.budget_status()["normal_available"] > 0 and
                                                      (not broker_mode or candidate.ticker in entry_tickers) and
                                                      not stop.is_set()),
                                                  persist_session=lambda: atomic_json(session_path, session),
                                                  entry_guard=(lambda moment: not stop.is_set() and
                                                      _broker_entry_window(trader, moment, start, entry_cutoff)) if broker_mode else None)
                            record_event(capture_path, frame)
                        except AuthenticationError:
                            raise
                        except QuotaExceeded as exc:
                            LOGGER.error("%s 配额熔断：%s", candidate.ticker, exc)
                            if essential:
                                raise
                        except RequestCancelled:
                            raise
                        except (APIError, DataContractError, ValueError) as exc:
                            LOGGER.warning("%s 本轮跳过：%s", candidate.ticker, exc)
                        finally:
                            atomic_json(session_path, session)
                    trader.export_trades_csv(config.output_dir / "trades_history.csv")
                if now_fn() < close and reason == "SESSION_CLOSE" and not stop.is_set():
                    stop.wait((close - now_fn()).total_seconds())
        except (KeyboardInterrupt, RequestCancelled):
            reason = "BROKER_MONITOR_ERROR" if background_errors else "SESSION_CLOSE" if now_fn() >= close else "USER_INTERRUPT"
            failed = bool(background_errors)
        except Exception:
            reason, failed = "MONITOR_ERROR", True
            if broker_mode:
                LOGGER.exception("监控异常，停止新开仓并请求Paper退出；只有远程成交确认才能结算")
            else:
                LOGGER.exception("监控异常，保存账本并按最后可用模型价清平模拟仓位")
        finally:
            timer.cancel()
            client.close()
            broker_stop.set()
            if broker_thread:
                broker_thread.join(timeout=15.0)
                if broker_thread.is_alive():
                    LOGGER.warning("后台对账尚未返回，将保留账本锁直到受限网络请求退出")
            if timer.is_alive():
                timer.join(timeout=15.0)
            trader.stop_new_entries()
            if old_sigint is not None:
                os_signal.signal(os_signal.SIGINT, old_sigint)
            if old_sigterm is not None:
                os_signal.signal(os_signal.SIGTERM, old_sigterm)
            if broker_mode:
                shutdown_detail = _reconcile_broker_shutdown(trader, broker_client, now_fn, reason)
                if ((broker_thread and broker_thread.is_alive()) or timer.is_alive()):
                    broker_client.close()
                    LOGGER.warning("等待后台broker/收盘线程结束，期间继续持有账户独占锁")
                    if broker_thread:
                        broker_thread.join()
                    if timer.is_alive():
                        timer.join()
            else:
                trader.close_all(close if now_fn() >= close else now_fn(), reason)
            atomic_json(session_path, session)
        if background_errors:
            failed = True
            reason = background_errors[0]
        report = save_results(config, trader, mode=report_mode, extra={"session_date": now.date().isoformat(),
                    "exit_reason": reason, "failed": failed, "quota": client.budget_status(),
                    "flow_calls": session["flow_calls"], "chain_calls": session["chain_calls"],
                    "program_monitor_required": broker_mode,
                    "broker_background_errors": background_errors,
                    **({"broker_poll_seconds_target": BROKER_POLL_SECONDS,
                        **pool_metadata,
                        "strategy_data_poll_minutes": [config.morning_interval_minutes, config.midday_interval_minutes,
                                                       config.afternoon_interval_minutes]} if broker_mode else {}),
                    **shutdown_detail})
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YHS异动期权监控与模拟复盘（仅Python标准库）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="实时YHS行情监控+模拟账本")
    mode.add_argument("--replay", action="store_true", help="78步模拟，或读取采集JSONL")
    mode.add_argument("--alpaca-paper", action="store_true", help="实时YHS信号+Alpaca Paper适配；默认禁止写订单")
    mode.add_argument("--alpaca-check", action="store_true", help="只读Alpaca Paper账户/持仓/订单/日历检查，不需要YHS Key")
    parser.add_argument("--execute-paper", action="store_true", help="仅在--alpaca-paper模式显式允许Paper订单写入")
    parser.add_argument("--alpaca-env-file", type=Path, help="读取已有Alpaca .env；不写入或修改环境文件")
    parser.add_argument("--alpaca-policy", type=Path, help="Alpaca Paper风险策略JSON")
    parser.add_argument("--config", type=Path, help="不包含密钥的JSON配置")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--date", default="2026-10-09", help="模拟日期YYYY-MM-DD")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--replay-file", type=Path, help="读取--capture生成的单日JSONL（离线）")
    parser.add_argument("--capture", type=Path, help="实时采集行情帧，便于离线复盘")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.execute_paper and not args.alpaca_paper:
        parser.error("--execute-paper只能与--alpaca-paper一起使用")
    if (args.alpaca_env_file or args.alpaca_policy) and not (args.alpaca_paper or args.alpaca_check):
        parser.error("Alpaca环境/策略参数只适用于--alpaca-paper或--alpaca-check")
    try:
        config = load_config(args.config, args.output_dir)
        if args.replay and args.output_dir is None:
            new_dir = config.output_dir / "replay" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
            config = replace(config, output_dir=new_dir, quota_state_path=new_dir / "unused_quota.json")
        config.output_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s：%(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(config.output_dir / "system.log", encoding="utf-8")], force=True)
        if not args.alpaca_check:
            LOGGER.info("YHS请求遵守50 RPM/至少1.2秒间隔；盘中5/12分钟轮询，不能把broker报价轮询等同于秒级策略信号")
        if args.alpaca_check:
            report = run_alpaca_check(config, args.alpaca_env_file)
        elif args.alpaca_paper:
            report = run_alpaca_paper(config, args.capture, env_file=args.alpaca_env_file,
                                      policy_path=args.alpaca_policy, execute_paper=args.execute_paper)
        elif args.replay:
            report = run_replay(config, date.fromisoformat(args.date), args.seed, args.replay_file)
        else:
            report = run_live(config, args.capture)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("failed") or report.get("shutdown_pending") else 0
    except KeyboardInterrupt:
        LOGGER.info("用户取消运行")
        return 130
    except (ValueError, OSError, APIError, PaperStateError, DataContractError, RuntimeError) as exc:
        LOGGER.error("运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
