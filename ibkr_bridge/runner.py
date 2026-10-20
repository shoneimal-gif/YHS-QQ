"""AlphaAgent tracking and local IBKR live-account READ-ONLY previews.

There is deliberately no order execution, quantity sizing, automatic position
adoption, or execution-enabling configuration. API login stays in local TWS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time

from .common import (MonitorError, PRIVATE_CONFIG, ROOT, RUNTIME, SHA, atomic_json,
                     load_config, monitor_lock, now, read_json, timestamp)
from .client import IBKRReadOnlyError, ReadonlyIBKRClient
from .source import SourceClient, SourceError


FLAGS = {"readonly": True, "orders_submitted": False, "preview_only": True,
         "user_confirmation_required": True, "quantity": None,
         "account_environment_verified": False, "tws_readonly_setting_verified": False}


def _code(exc: Exception) -> str:
    if isinstance(exc, (MonitorError, IBKRReadOnlyError, SourceError)):
        value = getattr(exc, "code", getattr(exc, "category", "read_only_unavailable"))
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value):
            return value
    return "read_only_unavailable"


def emit(data: dict) -> None:
    print(json.dumps(dict(data, **FLAGS), ensure_ascii=False, allow_nan=False), flush=True)


def _client(config: dict, *, preflight=False) -> ReadonlyIBKRClient:
    ib = config["ibkr"]
    bound = ib["account_sha256"]
    return ReadonlyIBKRClient(
        host=ib["host"], port=ib["port"], client_id=ib["client_id"],
        account_sha256_allowlist=() if preflight or bound is None else (bound,),
        request_timeout=ib["request_timeout_seconds"], quote_timeout=ib["quote_timeout_seconds"],
        max_quote_age_seconds=ib["max_quote_age_seconds"])


def preflight(config: dict, config_path: Path, bind_hash: str | None) -> None:
    if bind_hash is not None:
        if (not SHA.fullmatch(bind_hash) or config_path.resolve() != PRIVATE_CONFIG.resolve()
                or config_path.is_symlink()):
            raise MonitorError("private_account_binding_required")
    probe = _client(config, preflight=True)
    try:
        result = probe.connect()
        if bind_hash is not None:
            # Binding selects only an account already returned by this local
            # session. It never modifies the broker account or TWS settings.
            accounts = result.get("managed_accounts", [])
            if not any(item.get("account_sha256") == bind_hash for item in accounts):
                raise MonitorError("account_not_managed")
            latest = load_config(config_path)
            if latest != config:
                raise MonitorError("configuration_changed")
            config["ibkr"]["account_sha256"] = bind_hash
            atomic_json(config_path, config)
            emit({"mode": "ibkr_read_only_account_binding", "account_binding_saved": True,
                  "live_login_must_be_confirmed_in_tws": True})
        else:
            emit({"mode": "ibkr_read_only_preflight", "connection": result,
                  "next_step": "在TWS核对实盘登录、Read-Only API和脱敏账号，再绑定对应SHA256。"})
    finally:
        probe.close()


class Monitor:
    def __init__(self, config: dict, *, source_only: bool):
        self.config, self.source_only = config, source_only
        self.started = now()
        source = config["source"]
        self.source = SourceClient({"host": source["ssh_host"], "user": source["ssh_user"],
                                    "port": source["ssh_port"], "identity_file": source["identity_file"],
                                    "known_hosts_file": source["known_hosts_file"],
                                    "poll_seconds": config["display"]["poll_seconds"]})
        self.broker = None
        self.account = None
        self.next_account_snapshot = 0.0
        self.next_connection = 0.0
        self.broker_status = "source_only" if source_only else "not_connected"
        self.notifier = None
        self.notification_status = "disabled"
        self.state_path = RUNTIME / "monitor_state.json"
        self.state = {"schema_version": 1, "events": {}}
        if self.state_path.exists():
            state = read_json(self.state_path)
            if (set(state) != {"schema_version", "events"} or state["schema_version"] != 1
                    or not isinstance(state["events"], dict) or len(state["events"]) > 10000):
                raise MonitorError("monitor_state_invalid")
            for key, item in state["events"].items():
                if (not SHA.fullmatch(key) or not isinstance(item, dict)
                        or set(item) != {"invalidated", "notified", "preview_attempted", "last_seen_utc"}
                        or any(type(item[name]) is not bool for name in ("invalidated", "notified", "preview_attempted"))):
                    raise MonitorError("monitor_state_invalid")
                timestamp(item["last_seen_utc"])
            self.state = state
        self.last_report_signature = None
        if config["notifications"]["enabled"]:
            try:
                from discord_notifications import load_webhook
                from stock_quant.notifications import StockNotifier
                webhook = load_webhook(ROOT)
                if not webhook:
                    raise MonitorError("discord_configuration_missing")
                self.notifier = StockNotifier(webhook, RUNTIME / "discord_outbox.json",
                                              header="AlphaAgent | IBKR 实盘只读观察")
                self.notification_status = "configured_delivery_not_confirmed"
            except Exception:
                self.notification_status = "notification_unavailable"

    def close(self):
        if self.notifier is not None:
            self.notifier.close()
        if self.broker is not None:
            self.broker.close()
        self.source.close()

    def _connection(self) -> None:
        if self.source_only or time.monotonic() < self.next_connection:
            return
        if self.broker is not None:
            return
        if self.config["ibkr"]["account_sha256"] is None:
            self.broker_status = "account_binding_required"
            return
        candidate = _client(self.config)
        try:
            candidate.connect()
            candidate.bind_account(self.config["ibkr"]["account_sha256"])
            self.broker = candidate
            self.broker_status = "connected_read_only"
        except Exception as exc:
            candidate.close()
            self.broker_status = _code(exc)
            self.next_connection = time.monotonic() + 15

    def _age(self, event: dict) -> tuple[float, float, float]:
        created, received = timestamp(event["source_created_utc"]), timestamp(event["source_received_utc"])
        at = now()
        return ((at - created).total_seconds(), (at - received).total_seconds(),
                (received - created).total_seconds())

    def _account_snapshot(self) -> None:
        if self.broker is None or time.monotonic() < self.next_account_snapshot:
            return
        self.account = None
        try:
            self.account = self.broker.account_snapshot()
            # Account balances and positions remain in this ignored local file;
            # they are not copied into Discord messages or source exports.
            atomic_json(RUNTIME / "account_latest.json", self.account)
        except Exception as exc:
            self.broker.close()
            self.broker = None
            self.broker_status = _code(exc)
            self.next_connection = time.monotonic() + 15
            atomic_json(RUNTIME / "account_latest.json", dict(FLAGS, available=False, error=_code(exc),
                                                             observed_utc=now().isoformat()))
        self.next_account_snapshot = time.monotonic() + 30

    def _eligible(self, event: dict) -> tuple[bool, str]:
        age, delivery_age, _ = self._age(event)
        maximum = self.config["display"]["max_open_signal_age_seconds" if event["action"] == "open"
                                          else "max_close_signal_age_seconds"]
        if not 0 <= age <= maximum or not -5 <= delivery_age <= maximum:
            return False, "signal_expired_or_clock_unconfirmed"
        if timestamp(event["source_created_utc"]) < self.started:
            return False, "observed_before_monitor_start"
        return True, "fresh_source_read_only"

    def _recheck(self, event: dict) -> bool:
        snapshot = self.source.snapshot()
        if not snapshot["source_healthy"]:
            return False
        for current in snapshot["events"]:
            if current["source_id"] == event["source_id"]:
                # Compare the entire semantic trade, not only its identifier.
                fields = ("revision", "action", "tier", "contract", "reference_price_decimal",
                          "source_created_utc", "source_received_utc")
                return (current["verified"] and current["status"] == "verified"
                        and all(current[name] == event[name] for name in fields)
                        and self._eligible(current)[0])
        return False

    def _notify(self, event: dict, record: dict, report: dict) -> None:
        if self.notifier is None or record["notified"]:
            return
        fields = {"用途": "只读提示，未下单；需本人在IBKR确认", "动作": event["action"].upper(),
                  "合约": event["contract"]["occ_symbol"], "信号参考价": event["reference_price_decimal"],
                  "原信号时间UTC": event["source_created_utc"],
                  "观察时信号年龄秒": round(report["signal_age_seconds"], 3),
                  "源接收延迟秒": round(report["source_delivery_latency_seconds"], 3),
                  "观察时间UTC": now().isoformat(),
                  "IBKR": report["broker_status"], "报价状态": report["preview_status"]}
        preview = report.get("ibkr_preview")
        if preview is not None:
            quote = preview.get("quote", {})
            fields["买价/卖价"] = f"{quote.get('bid_usd')} / {quote.get('ask_usd')}（USD/份）"
            if event["action"] == "open":
                fields[f"上浮{self.config['display']['max_buy_premium_pct']}%内参考上限"] = str(preview.get("tick_aligned_price_cap_usd"))
            fields["报价说明"] = "观察快照，送达时可能过期；交易所时间未验证，请在IBKR重新核价"
        event_id = "ibkr-readonly:" + hashlib.sha256(event["source_id"].encode()).hexdigest()
        if self.notifier.enqueue(event_id, "AlphaAgent 新信号 · 只读观察", fields):
            record["notified"] = True

    def _event(self, event: dict) -> dict:
        key = hashlib.sha256(event["source_id"].encode()).hexdigest()
        record = self.state["events"].setdefault(key, {"invalidated": False, "notified": False, "preview_attempted": False,
                                                      "last_seen_utc": now().isoformat()})
        record["last_seen_utc"] = now().isoformat()
        if event["status"] == "invalidated":
            record["invalidated"] = True
        age, delivery_age, latency = self._age(event)
        report = dict(event, **FLAGS, signal_age_seconds=round(age, 3),
                      source_delivery_age_seconds=round(delivery_age, 3),
                      source_delivery_latency_seconds=round(latency, 3),
                      broker_status=self.broker_status, preview_status="source_unavailable", ibkr_preview=None)
        if record["invalidated"]:
            report["preview_status"] = "source_irreversibly_invalidated"
            if self.notifier is not None and record["notified"]:
                self.notifier.enqueue("ibkr-readonly-invalidated:" + key, "原信号已修改或撤回 · 只读预览失效",
                    {"用途": "原预览失效，请在IBKR自行核对；程序没有提交或撤销任何订单",
                     "源事件": event["source_id"], "观察时间UTC": now().isoformat()})
            return report
        if not event["verified"]:
            return report
        eligible, reason = self._eligible(event)
        report["preview_status"] = reason
        if not eligible:
            return report
        if record["preview_attempted"]:
            report["preview_status"] = "already_observed_preview_not_refreshed"
            if self.notifier is not None and not record["notified"] and self._recheck(event):
                self._notify(event, record, report)
            return report
        if self.broker is not None:
            record["preview_attempted"] = True
            try:
                if event["action"] == "close":
                    preview = self.broker.observe_option(event["contract"]["occ_symbol"])
                    preview["reference_role"] = "exit_reference_only_no_sell_price_rule"
                    preview["exit_reference_price_usd"] = event["reference_price_decimal"]
                else:
                    preview = self.broker.preview_option(event["contract"]["occ_symbol"],
                        event["reference_price_decimal"],
                        max_price_increase_pct=str(self.config["display"]["max_buy_premium_pct"]))
                    preview["reference_role"] = "opening_reference_cap_only_no_order"
                report["ibkr_preview"] = preview
                report["preview_status"] = "quote_snapshot_read_only"
            except Exception as exc:
                report["preview_status"] = _code(exc)
                self.broker.close()
                self.broker = None
                self.broker_status = _code(exc)
                self.next_connection = time.monotonic() + 15
        report["broker_status"] = self.broker_status
        # Network/qualification/quote latency never keeps a past verification
        # fresh. Re-read current edit/delete evidence before emitting a preview.
        if not self._recheck(event):
            report["ibkr_preview"] = None
            report["preview_status"] = "source_recheck_unconfirmed"
            return report
        preview = report["ibkr_preview"]
        if preview is not None:
            quote = preview["quote"]
            elapsed = (now() - timestamp(quote["observed_utc"])).total_seconds()
            receipt_age = quote.get("max_bid_ask_local_age_seconds")
            max_age = self.config["ibkr"]["max_quote_age_seconds"]
            if (receipt_age is None or elapsed < 0 or receipt_age + elapsed > max_age):
                quote.update(bid_usd=None, ask_usd=None, bid_size=None, ask_size=None,
                             realtime_verified=False, status="quote_expired_during_source_recheck")
                preview["price_within_tolerance"] = None
                report["preview_status"] = "quote_expired_during_source_recheck"
            # Position quantities are observations only and never imply adoption.
            if self.account is not None:
                preview["matching_position_quantity"] = sum(p["quantity"] for p in self.account["positions"]
                    if p["con_id"] == preview["contract"]["con_id"])
                preview["position_observed_utc"] = self.account["observed_utc"]
                preview["position_ownership_inferred"] = False
        age, delivery_age, latency = self._age(event)
        report.update(signal_age_seconds=round(age, 3), source_delivery_age_seconds=round(delivery_age, 3),
                      source_delivery_latency_seconds=round(latency, 3))
        self._notify(event, record, report)
        return report

    def step(self) -> dict:
        self._connection()
        result = {"mode": "ibkr_live_read_only_monitor", **FLAGS, "observed_utc": now().isoformat(),
                  "monitor_started_utc": self.started.isoformat(), "source_only": self.source_only,
                  "source_healthy": False, "source_error": None, "broker_status": self.broker_status,
                  "notification_status": self.notification_status, "events": [],
                  "poll_interval_is_not_latency_guarantee": True}
        try:
            snapshot = self.source.snapshot()
            result.update(source_healthy=snapshot["source_healthy"], source_error=snapshot["source_error"],
                          source_checked_utc=snapshot["checked_utc"])
            if snapshot["source_healthy"]:
                # Newest first; no bulk historical quote requests or notifications.
                result["events"] = [self._event(e) for e in
                    sorted(snapshot["events"], key=lambda item: item["source_created_utc"], reverse=True)
                    [:self.config["display"]["limit"]]]
        except Exception as exc:
            result.update(source_healthy=False, source_error=_code(exc), events=[])
        self._account_snapshot()
        result["broker_status"] = self.broker_status
        # No old preview is carried into a disconnected/pending/expired snapshot.
        if len(self.state["events"]) > 10000:
            raise MonitorError("monitor_state_capacity_reached")
        atomic_json(self.state_path, self.state)
        atomic_json(RUNTIME / "latest.json", result)
        return result

    def report(self, result: dict) -> None:
        # Suppress unchanged console heartbeat while still refreshing local state.
        signal = {"source_healthy": result["source_healthy"], "source_error": result["source_error"],
                  "broker_status": result["broker_status"],
                  "events": [(item["source_id"], item["revision"], item["status"], item["preview_status"])
                             for item in result["events"]]}
        signature = hashlib.sha256(json.dumps(signal, sort_keys=True).encode()).hexdigest()
        if signature != self.last_report_signature:
            emit(result)
            self.last_report_signature = signature

    def wait(self):
        interval = self.config["display"]["poll_seconds"]
        if self.broker is not None:
            try:
                self.broker.sleep(interval)
                return
            except Exception:
                self.broker.close()
                self.broker = None
                self.broker_status = "session_not_connected"
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="AlphaAgent → 本机IBKR实盘只读观察，无下单功能")
    parser.add_argument("--config", type=Path, default=PRIVATE_CONFIG)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--bind-account-sha256")
    action.add_argument("--source-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    # Third-party SDK logs can contain raw account identifiers or server text.
    for name in ("ib_async", "ib_insync"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
        logger.addHandler(logging.NullHandler())
    monitor = None
    try:
        config = load_config(args.config)
        with monitor_lock():
            if args.preflight or args.bind_account_sha256 is not None:
                preflight(config, args.config, args.bind_account_sha256)
                return 0
            if not args.source_only and config["ibkr"]["account_sha256"] is None:
                raise MonitorError("account_binding_required")
            monitor = Monitor(config, source_only=args.source_only)
            try:
                while True:
                    result = monitor.step()
                    monitor.report(result)
                    if args.once:
                        return 0 if result["source_healthy"] else 2
                    monitor.wait()
            finally:
                monitor.close()
                monitor = None
    except KeyboardInterrupt:
        emit({"mode": "ibkr_read_only_monitor", "status": "stopped_by_user"})
        return 0
    except Exception as exc:
        emit({"mode": "ibkr_read_only_monitor", "error": _code(exc),
              "next_step": "查看IBKR_READONLY_GUIDE.md；本程序不会请求密码或双重验证码。"})
        return 2
    finally:
        if monitor is not None:
            monitor.close()


if __name__ == "__main__":
    sys.exit(main())
