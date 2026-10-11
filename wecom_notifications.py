"""Enterprise WeChat notifications, independent of the trading ledger.

The persistent outbox survives restarts and retries delivery. A response lost
after acceptance, or a crash before saving success, can cause a duplicate.
No webhook or broker credentials are persisted in this module's state file.
"""

from __future__ import annotations

import json
from datetime import datetime
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOG = logging.getLogger(__name__)
_KINDS = {"submitted", "filled", "cancelled", "rejected", "unknown"}


def validate_webhook(url: str) -> str:
    """Accept only the official HTTPS robot endpoint, with one opaque key."""
    try:
        if not isinstance(url, str) or any(ord(char) < 32 for char in url):
            raise ValueError
        parts = urlsplit(url.strip())
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if (parts.scheme != "https" or parts.netloc != "qyapi.weixin.qq.com"
                or parts.path != "/cgi-bin/webhook/send" or parts.fragment
                or len(query) != 1 or query[0][0] != "key"
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", query[0][1])):
            raise ValueError
        return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + query[0][1]
    except Exception:
        raise ValueError("企业微信机器人地址无效，请检查官方机器人 Webhook。") from None


def load_webhook(root: Path) -> str | None:
    """Environment overrides the local, Git-ignored private configuration."""
    if "YHS_WECOM_WEBHOOK" in os.environ:
        return validate_webhook(os.environ["YHS_WECOM_WEBHOOK"])
    path = Path(root) / ".private_config" / "wecom.json"
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 16384:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return validate_webhook(data["webhook_url"])
    except Exception:
        raise ValueError("无法读取企业微信通知配置，请检查本地私有配置文件。") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _DeliveryError(Exception):
    def __init__(self, status: int | None = None):
        self.status = status
        super().__init__("企业微信通知未确认送达。")


def _post(webhook: str, content: str) -> None:
    clipped = content.encode("utf-8")
    if len(clipped) > 2048:
        content = clipped[:2030].decode("utf-8", errors="ignore") + "\n（已截断）"
    body = json.dumps({"msgtype": "text", "text": {"content": content}},
                      ensure_ascii=False).encode("utf-8")
    request = Request(webhook, data=body,
                      headers={"Content-Type": "application/json; charset=utf-8"},
                      method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=4) as response:
            if response.status != 200:
                raise _DeliveryError(response.status)
            result = json.loads(response.read(8192).decode("utf-8"))
            if not isinstance(result, dict) or type(result.get("errcode")) is not int:
                raise _DeliveryError()
            if result["errcode"] != 0:
                raise _DeliveryError()
    except HTTPError as exc:
        raise _DeliveryError(exc.code) from None
    except _DeliveryError:
        raise
    except Exception:
        raise _DeliveryError() from None


def _normalise_event(event: dict) -> dict:
    """Copy only notification fields; never mutate a caller's ledger entry."""
    result = {}
    for key in ("event_id", "kind", "side", "ticker", "contract_symbol", "timestamp"):
        value = event[key]
        if not isinstance(value, str) or not value or len(value) > 240:
            raise ValueError
        result[key] = value.replace("\r", " ").replace("\n", " ")
    if result["kind"] not in _KINDS or result["side"] not in {"buy", "sell"}:
        raise ValueError
    timestamp = datetime.fromisoformat(result["timestamp"].replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError
    for key in ("requested_quantity", "filled_quantity", "new_fill_quantity", "remaining_quantity", "price", "realized_pnl", "strike"):
        if event.get(key) is not None:
            value = float(event[key])
            if not math.isfinite(value) or (key != "realized_pnl" and value < 0):
                raise ValueError
            result[key] = value
    if "requested_quantity" not in result or "filled_quantity" not in result:
        raise ValueError
    for key in ("order_id", "client_order_id", "exit_reason", "right", "expiration", "action", "order_type"):
        if event.get(key):
            result[key] = str(event[key])[:240].replace("\r", " ").replace("\n", " ")
    return result


def _message(event: dict) -> str:
    kind = event["kind"]
    action = "买入" if event["side"] == "buy" else "卖出"
    statuses = {"submitted": "已提交", "cancelled": "已取消或到期", "rejected": "下单失败/被拒绝",
                "unknown": "状态未知"}
    status = statuses.get(kind)
    if kind == "filled":
        status = ("全部成交" if event["filled_quantity"] >= event["requested_quantity"]
                  else "部分成交")
    lines = ["YHS QQ｜Alpaca Paper", f"{action}订单{status}",
             f"标的：{event['ticker']}  合约：{event['contract_symbol']}"]
    right = {"C": "Call（看涨期权）", "P": "Put（看跌期权）"}.get(event.get("right"))
    if right:
        operation = "买入开仓" if event["side"] == "buy" else "卖出平仓"
        lines.append(f"操作：{operation} {right}")
    order_type = {"limit": "限价单", "market": "市价单"}.get(event.get("order_type"))
    if order_type:
        lines.append("订单类型：" + order_type)
    if event.get("expiration"):
        lines.append("到期日：" + event["expiration"])
    if "strike" in event:
        lines.append(f"行权价：${event['strike']:g}")
    lines.append(f"数量：本次订单 {event['requested_quantity']:g} 张；累计成交 {event['filled_quantity']:g} 张")
    if kind == "filled" and "new_fill_quantity" in event:
        lines.append(f"本次新增确认成交：{event['new_fill_quantity']:g} 张")
    if "remaining_quantity" in event:
        lines.append(f"该笔策略仓位剩余：{event['remaining_quantity']:g} 张（截至观察时间）")
    if "price" in event:
        label = "成交均价" if kind == "filled" else "限价"
        price = event["price"]
        lines.append(f"{label}：${price:.4f}/股（每张 = 100 股，约 ${price * 100:.2f}/张）")
        if kind == "filled":
            flow = "累计买入金额" if event["side"] == "buy" else "本单累计卖出金额"
            lines.append(f"{flow}：${price * event['filled_quantity'] * 100:.2f}（未含费用）")
        elif kind == "submitted":
            lines.append(f"按限价计算的委托金额：${price * event['requested_quantity'] * 100:.2f}（未含费用，尚待成交确认）")
    identity = event.get("order_id") or event.get("client_order_id")
    if identity:
        lines.append("订单尾号：" + identity[-8:])
    if event.get("exit_reason"):
        lines.append("退出原因：" + event["exit_reason"])
    if "realized_pnl" in event:
        lines.append(f"已实现收益：${event['realized_pnl']:+.2f}（未含费用）")
    if kind == "submitted":
        lines.append("提交不等于成交，成交另行确认通知。")
    elif kind == "unknown":
        lines.append("提交结果待券商对账；程序保留订单意图，不盲目重复下单。")
    lines.append("程序观察时间：" + event["timestamp"])
    return "\n".join(lines)


class WeComNotifier:
    """A serial daemon sender; enqueue performs only a short local disk write."""

    def __init__(self, webhook: str, state_path: Path):
        self._webhook = validate_webhook(webhook)
        self._path = Path(state_path)
        self._condition = threading.Condition()
        self._stopping = False
        self._pending = []
        self._sent = set()
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if (type(data.get("version")) is not int or data["version"] != 1
                        or not isinstance(data.get("sent_ids"), list)
                        or not isinstance(data.get("pending"), list)):
                    raise ValueError
                self._sent = set(data["sent_ids"])
                if any(not isinstance(value, str) for value in self._sent):
                    raise ValueError
                seen = set(self._sent)
                for item in data["pending"]:
                    event = _normalise_event(item["event"])
                    attempts = max(0, int(item.get("attempts", 0)))
                    due = float(item.get("next_attempt", 0))
                    if not math.isfinite(due):
                        raise ValueError
                    if event["event_id"] not in seen:
                        seen.add(event["event_id"])
                        self._pending.append({"event": event, "attempts": attempts,
                                              "next_attempt": due})
        except Exception:
            raise ValueError("企业微信通知队列无法读取，通知已停用。") from None
        self._worker = threading.Thread(target=self._run, name="wecom-notifications", daemon=True)
        self._worker.start()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        payload = {"version": 1, "pending": self._pending, "sent_ids": sorted(self._sent)}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    def enqueue(self, event: dict) -> bool:
        try:
            event = _normalise_event(event)
            with self._condition:
                identity = event["event_id"]
                if identity in self._sent or any(
                        item["event"]["event_id"] == identity for item in self._pending):
                    return True
                if self._stopping:
                    return False
                item = {"event": event, "attempts": 0, "next_attempt": 0}
                self._pending.append(item)
                try:
                    self._save()
                except Exception:
                    self._pending.remove(item)
                    raise
                self._condition.notify()
            return True
        except Exception as exc:
            LOG.warning("企业微信通知入队失败，类型=%s", type(exc).__name__)
            return False

    def _run(self) -> None:
        last_send = 0.0
        while True:
            with self._condition:
                while not self._stopping:
                    now = time.time()
                    item = next((item for item in self._pending if item["next_attempt"] <= now), None)
                    delay = max(0.0, 3.2 - (time.monotonic() - last_send))
                    if item is not None and delay == 0:
                        break
                    if item is None:
                        delay = min((item["next_attempt"] - now for item in self._pending), default=60)
                    self._condition.wait(timeout=max(0.01, min(delay, 60)))
                if self._stopping:
                    return
            failure = None
            try:
                _post(self._webhook, _message(item["event"]))
            except Exception as exc:
                failure = exc
                LOG.warning("企业微信通知发送失败，类型=%s，HTTP状态=%s",
                            type(exc).__name__, getattr(exc, "status", None))
            last_send = time.monotonic()
            with self._condition:
                # close() may return while this HTTP request is still finishing.
                # Keep the durable pending item and avoid writes after its owner
                # releases the account lease; a restarted process owns the queue.
                if self._stopping:
                    return
                if failure is None:
                    self._pending.remove(item)
                    self._sent.add(item["event"]["event_id"])
                else:
                    item["attempts"] += 1
                    item["next_attempt"] = time.time() + min(600, 30 * 2 ** min(item["attempts"] - 1, 5))
                try:
                    self._save()
                except Exception as exc:
                    LOG.warning("企业微信通知队列保存失败，类型=%s", type(exc).__name__)
                self._condition.notify_all()

    def close(self, timeout: float = 5) -> None:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            # Give normal final-fill messages a bounded opportunity to send.
            # Failed/backlogged messages remain on disk for the next run.
            while self._pending and self._worker.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            self._stopping = True
            self._condition.notify_all()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=max(0, deadline - time.monotonic()))


def send_test_notification(webhook: str) -> None:
    """Send only when the user explicitly invokes the notification test command."""
    _post(validate_webhook(webhook),
          "YHS QQ｜Alpaca Paper\n企业微信通知测试：连接正常。此次仅发送测试通知，未提交订单。")
