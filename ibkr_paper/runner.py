"""Local entry point for explicitly authorized IBKR simulated trading only."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time

from ibkr_bridge.common import MonitorError
from ibkr_bridge.source import SourceClient, SourceError
from .common import (CONFIG, ROOT, RUNTIME, SHA, PaperError, atomic_json,
                     executor_lock, load_config, now)


def code(exc: BaseException) -> str:
    value = getattr(exc, "code", getattr(exc, "category", None))
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value):
        return value
    return "paper_operation_unavailable"


def emit(data: dict) -> None:
    print(json.dumps(dict(data, mode="ibkr_paper", paper_only=True), ensure_ascii=False,
                     allow_nan=False), flush=True)


def client_for(config: dict, *, execute=False, preflight=False):
    from .client import PaperIBKRClient
    ib = config["ibkr"]
    bound = ib["account_sha256"]
    return PaperIBKRClient(host=ib["host"], port=ib["port"], client_id=ib["client_id"],
                          account_sha256_allowlist=() if preflight or bound is None else (bound,),
                          execute_paper=execute, request_timeout=ib["request_timeout_seconds"],
                          quote_timeout=ib["quote_timeout_seconds"],
                          max_quote_age_seconds=ib["max_quote_age_seconds"])


def source_for(config: dict) -> SourceClient:
    source = config["source"]
    return SourceClient({"host": source["ssh_host"], "user": source["ssh_user"],
                         "port": source["ssh_port"], "identity_file": source["identity_file"],
                         "known_hosts_file": source["known_hosts_file"],
                         "poll_seconds": config["strategy"]["poll_seconds"]})


def preflight(config: dict, path: Path, *, bind_hash: str | None = None) -> None:
    if bind_hash is not None and (not SHA.fullmatch(bind_hash) or path.resolve() != CONFIG.resolve()
                                 or path.is_symlink()):
        raise PaperError("private_paper_account_binding_required")
    broker = client_for(config, preflight=True)
    try:
        result = broker.connect()
        if result.get("paper_verified") is not True:
            raise PaperError("paper_account_unconfirmed")
        if bind_hash is not None:
            accounts = result.get("managed_accounts", [])
            if len(accounts) != 1 or accounts[0].get("account_sha256") != bind_hash:
                raise PaperError("paper_account_binding_mismatch")
            latest = load_config(path)
            if latest != config:
                raise PaperError("paper_configuration_changed")
            if RUNTIME.joinpath("ledger.sqlite3").exists() and config["ibkr"]["account_sha256"] != bind_hash:
                raise PaperError("paper_ledger_account_rebinding_refused")
            config["ibkr"]["account_sha256"] = bind_hash
            atomic_json(path, config)
            report = {"action": "paper_account_bound", "account_binding_saved": True,
                      "paper_verified": True, "orders_submitted": False,
                      "service_started": False, "activation_starts_with_execute_paper": True}
        else:
            report = {"action": "preflight", "connection": result,
                      "paper_verified": True, "orders_submitted": False,
                      "service_started": False,
                      "next_step": "核对Gateway仍是Paper Trading，绑定DU账户摘要后再启用模拟执行。"}
        atomic_json(RUNTIME / "connection_status.json", report)
        emit(report)
    finally:
        broker.close()


def observe_once(config: dict, broker, source) -> dict:
    account = broker.snapshot()
    if account.get("complete") is not True:
        raise PaperError("paper_snapshot_incomplete")
    feed = source.snapshot()
    return {"action": "observe_only", "observed_utc": now().isoformat(),
            "paper_verified": True, "readonly": True, "orders_submitted": False,
            "source_healthy": feed["source_healthy"], "source_error": feed["source_error"],
            "source_checked_utc": feed["checked_utc"], "source_event_count": len(feed["events"]),
            "position_count": len(account["positions"]), "broker_order_count": len(account["orders"]),
            "execution_enabled": False, "ledger_activated": False,
            "poll_interval_is_not_latency_guarantee": True}


def make_notifier(config: dict):
    if not config["notifications"]["enabled"]:
        return None
    from discord_notifications import load_webhook
    from stock_quant.notifications import StockNotifier
    webhook = load_webhook(ROOT)
    if not webhook:
        raise PaperError("paper_discord_configuration_missing")
    return StockNotifier(webhook, RUNTIME / "discord_outbox.json",
                         header="Dennis AlphaAgent | IBKR Paper 模拟跟单")


def watch(config: dict, *, execute: bool, once: bool) -> int:
    if config["ibkr"]["account_sha256"] is None:
        raise PaperError("paper_account_binding_required")
    if execute and (not config["execute_paper"] or not config["paper_login_confirmed"]):
        raise PaperError("paper_execution_configuration_required")
    source = source_for(config)
    broker = engine = notifier = None
    last_signature = None
    outage = None
    next_health_print = 0.0
    try:
        if execute:
            notifier = make_notifier(config)
        while True:
            try:
                if broker is None:
                    broker = client_for(config, execute=execute)
                    connection = broker.connect()
                    if connection.get("paper_verified") is not True:
                        raise PaperError("paper_account_unconfirmed")
                    broker.bind_account(config["ibkr"]["account_sha256"])
                    if execute:
                        from .engine import PaperEngine
                        engine = PaperEngine(config, broker, source, runtime=RUNTIME, notifier=notifier)
                report = engine.step() if execute else observe_once(config, broker, source)
                report = dict(report, action="execute_paper" if execute else "observe_only",
                              observed_utc=now().isoformat(), paper_verified=True,
                              execution_enabled=execute, local_gateway_required=True,
                              poll_interval_is_not_latency_guarantee=True)
                atomic_json(RUNTIME / "latest.json", report)
                if outage is not None:
                    if notifier is not None:
                        notifier.enqueue("ibp-recovered:" + outage, "IBKR 模拟盘连接已恢复",
                            {"状态": "已重新核对绑定的模拟账户；恢复对账及有效新信号处理",
                             "说明": "不会追买历史消息，也不会重发结果未知的委托", "观察时间UTC": now().isoformat()})
                    outage = None
                # Engine reports carry timestamps; print at most once per 30s
                # unless a signal/order/fill or health summary has changed.
                stable = {k: v for k, v in report.items() if k not in {"observed_utc", "checked_utc", "snapshot_utc"}}
                signature = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
                if signature != last_signature or time.monotonic() >= next_health_print or once:
                    emit(report)
                    last_signature = signature
                    next_health_print = time.monotonic() + 30
                if once:
                    return 0
                broker.sleep(config["strategy"]["poll_seconds"])
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                category = code(exc)
                report = {"action": "paused_reconnecting" if execute else "observe_unavailable",
                          "observed_utc": now().isoformat(), "error": category,
                          "new_order_submission_paused": True, "execution_enabled": execute,
                          "broker_state_confirmed": False, "unknown_orders_will_not_be_resent": True}
                atomic_json(RUNTIME / "latest.json", report)
                emit(report)
                if outage is None:
                    outage = hashlib.sha256(now().isoformat().encode()).hexdigest()[:24]
                    if notifier is not None:
                        notifier.enqueue("ibp-disconnected:" + outage, "IBKR 模拟跟单暂不可用",
                            {"分类": category, "动作": "暂停新增提交，继续尝试连接和核对原委托",
                             "说明": "已有委托和持仓以券商记录为准，未知委托不会自动重发",
                             "观察时间UTC": now().isoformat()})
                if engine is not None:
                    engine.close()
                    engine = None
                if broker is not None:
                    broker.close()
                    broker = None
                if once:
                    return 2
                time.sleep(5)
    finally:
        if engine is not None:
            engine.close()
        if broker is not None:
            broker.close()
        if notifier is not None:
            notifier.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Dennis AlphaAgent → IBKR Paper-only 自动跟单")
    parser.add_argument("--config", type=Path, default=CONFIG)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--preflight", action="store_true")
    actions.add_argument("--bind-account-sha256")
    actions.add_argument("--observe-only", action="store_true")
    actions.add_argument("--execute-paper", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    execution_started = False
    for name in ("ib_async", "ib_insync"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
        logger.addHandler(logging.NullHandler())
    try:
        config = load_config(args.config)
        with executor_lock():
            if args.preflight or args.bind_account_sha256 is not None:
                preflight(config, args.config, bind_hash=args.bind_account_sha256)
                return 0
            execution_started = args.execute_paper
            return watch(config, execute=args.execute_paper, once=args.once)
    except KeyboardInterrupt:
        emit({"action": "stopped_by_user", "broker_orders_automatically_cancelled": False})
        return 0
    except Exception as exc:
        emit({"action": "state_unconfirmed" if execution_started else "not_started", "error": code(exc),
              "orders_submitted": None if execution_started else False,
              "next_step": "查看IBKR_PAPER_GUIDE.md，保留已有账本与未知委托身份。"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
