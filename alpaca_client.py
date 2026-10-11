"""Alpaca 单腿期权 Paper REST 适配器；仅使用 Python 标准库。

交易主机固定为 paper-api.alpaca.markets，行情主机固定为 data.alpaca.markets。
POST/PATCH/DELETE 不自动重试；请求结果未知时必须按 client_order_id/order_id
查询并对账。创建或替换订单的 HTTP 200 仅表示受理，绝不等同已经成交。

官方参考：
https://docs.alpaca.markets/us/docs/options-trading
https://docs.alpaca.markets/us/reference/postorder
https://docs.alpaca.markets/us/reference/getorderbyclientorderid
https://docs.alpaca.markets/us/reference/patchorderbyorderid-1
https://docs.alpaca.markets/us/reference/optionlatestquotes
https://docs.alpaca.markets/us/reference/optionsnapshots
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import error, parse, request

LOGGER = logging.getLogger(__name__)
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_ENV_KEYS = frozenset(("ALPACA_API_KEY", "ALPACA_SECRET_KEY"))


class AlpacaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


class SubmissionNotSent(AlpacaError):
    """提交前回调拒绝；本次请求尚未交给网络 opener，可释放本地意图。"""


class SubmissionUnknown(AlpacaError):
    """写请求可能已受理；只能先查询，不能盲目重新下单。"""

    def __init__(self, message: str, *, client_order_id: str = "", operation: str = "",
                 order_id: str = "", status: int | None = None):
        super().__init__(message, status)
        self.client_order_id = client_order_id
        self.operation, self.order_id = operation, order_id


def load_alpaca_credentials(env_file: str | Path | None = None) -> dict[str, str]:
    """仅读取两个凭据字段，进程环境优先；不修改 os.environ。

    支持 KEY=value、export KEY=value 和单/双引号。值按字面读取，不执行
    shell、不做变量插值、不支持多行或转义求值。错误消息仅含字段/行号。
    """
    values: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise AlpacaError("无法读取指定 Alpaca 凭据文件；请检查路径、权限及 UTF-8 编码") from None
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[6:].lstrip()
            name, separator, value = line.partition("=")
            name = name.strip()
            if name not in _ENV_KEYS:
                continue
            if not separator:
                raise AlpacaError(f"凭据文件第 {line_number} 行 {name} 缺少等号")
            value = value.strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0 or (value[end+1:].strip() and not value[end+1:].lstrip().startswith("#")):
                    raise AlpacaError(f"凭据文件第 {line_number} 行 {name} 引号格式错误")
                value = value[1:end]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            values[name] = value
    for name in _ENV_KEYS:
        if name in os.environ:
            values[name] = os.environ[name]
        value = values.get(name, "").strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise AlpacaError(f"{name} 缺失或含非法控制字符；请设置 Paper 凭据")
        values[name] = value
    return values


def normalize_option_symbol(symbol: str) -> str:
    """O:NVDA261016C00188000 -> NVDA261016C00188000，并校验 OCC。"""
    value = str(symbol).strip().upper()
    if value.startswith("O:"):
        value = value[2:]
    # 同时接受 OCC 21 字符 root 空格填充形式；不移除任意位置的空格。
    padded = re.fullmatch(r"([A-Z][A-Z0-9.]{0,5})( +)(\d{6}[CP]\d{8})", value)
    if padded and len(padded[1]) + len(padded[2]) == 6:
        value = padded[1] + padded[3]
    match = re.fullmatch(r"([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})", value)
    if match is None:
        raise AlpacaError("期权合约必须是有效 OCC 代码，不接受股票代码、URL 或任意路径")
    stamp = match[2]
    try:
        date(2000 + int(stamp[:2]), int(stamp[2:4]), int(stamp[4:]))
    except ValueError:
        raise AlpacaError("OCC 合约到期日无效") from None
    if int(match[4]) <= 0:
        raise AlpacaError("OCC 合约行权价必须为正数")
    return value


def _positive_decimal(value: Any, name: str, *, integer: bool = False) -> str:
    if isinstance(value, bool):
        raise AlpacaError(f"{name} 必须是正数")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise AlpacaError(f"{name} 必须是有效数字") from None
    if not number.is_finite() or number <= 0:
        raise AlpacaError(f"{name} 必须是有限正数")
    if integer:
        if number != number.to_integral_value():
            raise AlpacaError("期权 qty 必须是正整数，不能使用零碎合约")
        return str(int(number))
    return format(number, "f")


def normalize_quote(raw: Mapping[str, Any], symbol: str | None = None,
                    feed: str = "indicative") -> dict[str, Any]:
    """标准化 Alpaca bp/ap/bs/as/t；不会把 indicative 当作 OPRA 实时报价。"""
    if not isinstance(raw, Mapping):
        raise AlpacaError("期权 quote 必须是 JSON 对象")
    if feed not in ("indicative", "opra"):
        raise AlpacaError("行情 feed 只支持 indicative 或 opra")
    def number(short: str, long: str) -> float:
        try:
            value = float(raw.get(short, raw.get(long)))
        except (TypeError, ValueError):
            raise AlpacaError(f"期权报价缺少有效 {long}") from None
        if not math.isfinite(value):
            raise AlpacaError(f"期权 {long} 不是有限数字")
        return value
    bid, ask = number("bp", "bid_price"), number("ap", "ask_price")
    if bid < 0 or ask <= 0 or bid > ask:
        raise AlpacaError("期权报价为空、为负或买卖倒挂；禁止作为可成交报价")
    stamp = raw.get("t", raw.get("timestamp", raw.get("quoted_at")))
    if not isinstance(stamp, str) or not stamp:
        raise AlpacaError("期权报价缺少源时间，不能判断新鲜度")
    return {"symbol": normalize_option_symbol(symbol) if symbol else None,
            "bid": bid, "ask": ask, "bid_price": bid, "ask_price": ask,
            "midpoint": (bid+ask)/2, "bid_size": raw.get("bs", raw.get("bid_size")),
            "ask_size": raw.get("as", raw.get("ask_size")), "quoted_at": stamp, "timestamp": stamp,
            "feed": feed, "indicative": feed == "indicative", "estimated": feed == "indicative", "raw": dict(raw)}


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class _RateGate:
    def __init__(self, max_rpm: int, clock_fn: Callable[[], float]):
        self.max_rpm, self.clock = max_rpm, clock_fn
        self.lock = threading.Lock()
        self.starts: deque[float] = deque()
        self.last_start = -math.inf
        self.blocked_until = -math.inf


_GATES_LOCK = threading.Lock()
_GATES: dict[str, _RateGate] = {}


class AlpacaPaperClient:
    def __init__(self, api_key: str, secret_key: str, *, max_rpm: int = 120,
                 timeout_seconds: float = 10.0, max_get_retries: int = 3,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.monotonic,
                 opener: Callable[..., Any] | None = None):
        for name, value in (("ALPACA_API_KEY",api_key), ("ALPACA_SECRET_KEY",secret_key)):
            if not isinstance(value, str) or not value.strip() or any(ord(c)<32 or ord(c)==127 for c in value):
                raise AlpacaError(f"{name} 缺失或含非法控制字符")
        if isinstance(max_rpm, bool) or not 1 <= int(max_rpm) <= 120 or int(max_rpm) != max_rpm:
            raise AlpacaError("Alpaca 独立限频必须是 1–120 RPM 的整数")
        if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= 15:
            raise AlpacaError("Alpaca 单次网络超时必须大于零且不超过 15 秒")
        if isinstance(max_get_retries, bool) or not 0 <= int(max_get_retries) <= 5 or int(max_get_retries) != max_get_retries:
            raise AlpacaError("GET 额外重试次数必须为 0–5 的整数")
        self._api_key, self._secret_key = api_key.strip(), secret_key.strip()
        self.timeout_seconds, self.max_get_retries = float(timeout_seconds), int(max_get_retries)
        self._sleep_fn, self._cancel = sleep_fn, threading.Event()
        self._opener = opener or request.build_opener(_NoRedirect()).open
        # 同进程中同一 Key 的实例共用窗口；与 YHS 的限频及配额完全分离。
        import hashlib
        identity = hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()
        with _GATES_LOCK:
            gate = _GATES.get(identity)
            if gate is None:
                gate = _RateGate(int(max_rpm), clock_fn)
                _GATES[identity] = gate
            else:
                gate.max_rpm = min(gate.max_rpm, int(max_rpm))
        self._gate = gate
        self.last_status: int | None = None
        self.last_request_id: str | None = None
        self.last_headers: dict[str, str] = {}

    @classmethod
    def from_environment(cls, env_file: str | Path | None = None, **client_options: Any) -> "AlpacaPaperClient":
        values = load_alpaca_credentials(env_file)
        return cls(values["ALPACA_API_KEY"], values["ALPACA_SECRET_KEY"], **client_options)

    @property
    def base_url(self) -> str:
        return PAPER_BASE_URL

    @property
    def data_base_url(self) -> str:
        return DATA_BASE_URL

    @property
    def paper_only(self) -> bool:
        return True

    @property
    def paper(self) -> bool:
        return True

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise AlpacaError("Alpaca Paper 客户端已关闭；不再发送请求")

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_cancel()
            step = min(0.5, remaining)
            self._sleep_fn(step)
            remaining -= step
        self._check_cancel()

    def _rate_slot(self) -> None:
        while True:
            self._check_cancel()
            with self._gate.lock:
                now = self._gate.clock()
                while self._gate.starts and self._gate.starts[0] <= now-60:
                    self._gate.starts.popleft()
                wait = max(0.0, self._gate.blocked_until-now,
                           self._gate.last_start+60/self._gate.max_rpm-now)
                if len(self._gate.starts) >= self._gate.max_rpm:
                    wait = max(wait,self._gate.starts[0]+60.000001-now)
                if wait <= 0:
                    self._gate.starts.append(now)
                    self._gate.last_start = now
                    return
            self._sleep(wait)

    @staticmethod
    def _checked_url(path: str, params: Mapping[str, Any] | None, *, data: bool, method: str) -> str:
        parts = parse.urlsplit(path)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or not path.startswith("/") or ".." in path.split("/"):
            raise AlpacaError("禁止外部 URL、重定向、路径穿越或自行拼接查询串")
        if data:
            if method != "GET" or path not in ("/v1beta1/options/snapshots", "/v1beta1/options/quotes/latest"):
                raise AlpacaError("行情主机仅允许已声明的期权只读路由")
        elif method == "GET":
            allowed = path in ("/v2/account","/v2/clock","/v2/calendar","/v2/positions","/v2/orders","/v2/orders:by_client_order_id")
            allowed = allowed or bool(re.fullmatch(r"/v2/orders/[0-9a-f-]{36}",path)) or path.startswith("/v2/options/contracts/")
            if not allowed:
                raise AlpacaError("不支持该 Paper 只读路由")
        elif not ((method == "POST" and path == "/v2/orders") or
                  (method in ("PATCH","DELETE") and re.fullmatch(r"/v2/orders/[0-9a-f-]{36}",path))):
            raise AlpacaError("写请求只能访问 Paper 订单接口")
        base = DATA_BASE_URL if data else PAPER_BASE_URL
        url = base + path
        if params:
            url += "?" + parse.urlencode({k:v for k,v in params.items() if v is not None},doseq=True)
        result = parse.urlsplit(url)
        required_host = "data.alpaca.markets" if data else "paper-api.alpaca.markets"
        if result.scheme != "https" or result.hostname != required_host or result.port is not None or result.username or result.password:
            raise AlpacaError("Paper 主机安全校验失败；禁止发送请求")
        return url

    def _safe_message(self, body: bytes) -> tuple[str, str]:
        message, code = "", ""
        try:
            value = json.loads(body.decode("utf-8-sig"))
            if isinstance(value, dict):
                message = str(value.get("message", ""))
                candidate = str(value.get("code", ""))
                # 官方错误码是数字；不回显可能被错误服务放入 code 的凭据字符串。
                if re.fullmatch(r"[0-9]{1,12}",candidate):
                    code = candidate
        except (ValueError, UnicodeError):
            message = "服务端未返回可解析的错误 JSON"
        for secret in (self._api_key,self._secret_key):
            message = message.replace(secret,"[REDACTED]")
        message = re.sub(r"(?i)(APCA-API-(?:KEY-ID|SECRET-KEY)|ALPACA_(?:API_KEY|SECRET_KEY))\s*[:=]\s*[^\s,;]+",r"\1=[REDACTED]",message)
        message = " ".join(message.split())[:300]
        return message, code

    def _observe_headers(self, status: int, headers: Any) -> dict[str, str]:
        normalized = {str(k).lower():str(v) for k,v in headers.items()} if headers else {}
        self.last_status = status
        self.last_headers = normalized
        self.last_request_id = normalized.get("x-request-id")
        try:
            remaining = int(normalized.get("x-ratelimit-remaining","1"))
        except ValueError:
            remaining = 1
        if remaining <= 0 or status == 429:
            wait = self._retry_after(normalized)
            if remaining <= 0 and wait <= 0:
                wait = 60.0
            with self._gate.lock:
                self._gate.blocked_until = max(self._gate.blocked_until,self._gate.clock()+wait)
        return normalized

    @staticmethod
    def _retry_after(headers: Mapping[str,str]) -> float:
        wait = 0.0
        value = headers.get("retry-after")
        if value:
            try:
                parsed = float(value)
                wait = max(wait,parsed) if math.isfinite(parsed) else wait
            except ValueError:
                try:
                    wait = max(wait,parsedate_to_datetime(value).timestamp()-time.time())
                except (ValueError,TypeError,OverflowError):
                    pass
        try:
            reset = float(headers.get("x-ratelimit-reset","0"))
            if math.isfinite(reset):
                wait = max(wait,reset-time.time())
        except ValueError:
            pass
        return max(0.0,wait)

    def _request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                 payload: Mapping[str,Any] | None = None, data: bool = False,
                 none_on_404: bool = False, client_order_id: str = "", order_id: str = "",
                 before_send: Callable[[], None] | None = None) -> Any:
        url = self._checked_url(path,params,data=data,method=method)
        if before_send is not None and not callable(before_send):
            raise SubmissionNotSent("Paper 提交前检查必须是可调用函数；未发送请求")
        write = method != "GET"
        try:
            encoded = json.dumps(dict(payload),allow_nan=False,separators=(",",":")).encode("utf-8") if payload is not None else None
        except (TypeError,ValueError):
            raise AlpacaError("订单 payload 不是有效 JSON，未发送请求") from None
        attempts = 1 if write else self.max_get_retries+1
        backoff = 1.0
        for attempt in range(attempts):
            self._check_cancel()
            self._rate_slot()
            # 再次校验固定主机；不开启自动重定向。
            self._checked_url(path,params,data=data,method=method)
            headers = {"APCA-API-KEY-ID":self._api_key,"APCA-API-SECRET-KEY":self._secret_key,
                       "Accept":"application/json","User-Agent":"YHS-Alpaca-Paper/1.0"}
            if encoded is not None:
                headers["Content-Type"] = "application/json"
            response = None
            status: int | None = None
            own_headers: dict[str,str] = {}
            try:
                req = request.Request(url,data=encoded,headers=headers,method=method)
                self._check_cancel()
                if before_send is not None:
                    try:
                        # 限频/Retry-After 等待已结束；回调只作本地最终检查。
                        # 包装回调异常，避免凭据或其他敏感异常原文进入日志。
                        before_send()
                    except Exception:
                        raise SubmissionNotSent("Paper 提交前检查拒绝；未发送请求") from None
                try:
                    response = self._opener(req,timeout=self.timeout_seconds)
                except error.HTTPError as exc:
                    response = exc
                status = int(response.getcode())
                own_headers = self._observe_headers(status,response.headers)
                body = response.read(_MAX_BODY_BYTES+1)
                if len(body)>_MAX_BODY_BYTES:
                    if write and (200 <= status < 300 or status==408 or 500 <= status <= 599):
                        raise SubmissionUnknown("Paper 写请求响应过大，不能确认订单状态；请先对账",client_order_id=client_order_id,operation=method,order_id=order_id,status=status)
                    raise AlpacaError("Alpaca 响应超过安全上限，请缩小查询范围",status)
            except (error.URLError,TimeoutError,ConnectionError,OSError,HTTPException):
                if write:
                    raise SubmissionUnknown("Paper 写请求连接中断或超时，结果未知；禁止直接重复提交，请先查询订单",client_order_id=client_order_id,operation=method,order_id=order_id,status=status) from None
                if attempt+1 >= attempts:
                    raise AlpacaError("Alpaca GET 网络请求重试后仍失败；凭据不会写入日志",status) from None
                LOGGER.warning("Alpaca GET 连接失败，%.1f 秒后重试（%d/%d）",backoff,attempt+1,self.max_get_retries)
                self._sleep(backoff)
                backoff=min(8.0,backoff*2)
                continue
            finally:
                if response is not None:
                    try:
                        response.close()
                    except OSError:
                        pass
            if status == 404 and none_on_404:
                return None
            if status is not None and 200 <= status < 300:
                if status == 204 or not body:
                    if method == "DELETE":
                        return None
                    if write:
                        raise SubmissionUnknown("Paper 写请求已受理但没有订单响应；请先查询订单",client_order_id=client_order_id,operation=method,order_id=order_id,status=status)
                    raise AlpacaError("Alpaca GET 成功响应为空，无法验证数据",status)
                try:
                    result = json.loads(body.decode("utf-8-sig"))
                except (ValueError,UnicodeError):
                    if write:
                        raise SubmissionUnknown("Paper 写请求成功响应无法解析；请先查询订单，禁止重复提交",client_order_id=client_order_id,operation=method,order_id=order_id,status=status) from None
                    raise AlpacaError("Alpaca GET 返回无效 JSON",status) from None
                if not isinstance(result,(dict,list)):
                    if write:
                        raise SubmissionUnknown("Paper 写请求成功响应格式未知；请先对账",client_order_id=client_order_id,operation=method,order_id=order_id,status=status)
                    raise AlpacaError("Alpaca 响应必须是 JSON 对象或数组",status)
                return result
            message,code = self._safe_message(body)
            if status in (401,403):
                details = "凭据无效" if status==401 else "权限、行情订阅、期权级别或购买力不足"
                LOGGER.error("Alpaca Paper HTTP %s：%s；%s",status,details,message)
                raise AlpacaError(f"Alpaca Paper HTTP {status}：{details}；{message}",status,code)
            retryable = status in (408,429) or (status is not None and 500 <= status <= 599)
            if write and (status==408 or (status is not None and 500 <= status <= 599)):
                raise SubmissionUnknown(f"Paper 写请求 HTTP {status}，不能确认是否受理；请先查询订单",client_order_id=client_order_id,operation=method,order_id=order_id,status=status)
            if not write and retryable and attempt+1<attempts:
                wait=max(backoff,self._retry_after(own_headers) if status==429 else 0.0)
                LOGGER.warning("Alpaca GET HTTP %s，%.1f 秒后重试（%d/%d）",status,wait,attempt+1,self.max_get_retries)
                self._sleep(wait)
                backoff=min(8.0,backoff*2)
                continue
            raise AlpacaError(f"Alpaca Paper 请求失败：HTTP {status} {code}；{message}",status,code)
        raise AlpacaError("Alpaca 请求未完成")

    @staticmethod
    def _object(value: Any, name: str) -> dict[str,Any]:
        if not isinstance(value,dict):
            raise AlpacaError(f"{name} 响应必须是 JSON 对象")
        return value

    @staticmethod
    def _identifier(value: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError,TypeError,AttributeError):
            raise AlpacaError("order_id 必须是 Alpaca 返回的 UUID") from None

    @staticmethod
    def _client_id(value: Any) -> str:
        if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",value):
            raise AlpacaError("client_order_id 必须为 1–128 个字母、数字或 . _ : - 字符")
        return value

    def get_account(self) -> dict[str,Any]:
        return self._object(self._request("GET","/v2/account"),"account")

    def get_clock(self) -> dict[str,Any]:
        return self._object(self._request("GET","/v2/clock"),"clock")

    def get_calendar(self, start: str | date, end: str | date) -> list[dict[str,Any]]:
        first=date.fromisoformat(str(start))
        last=date.fromisoformat(str(end))
        if first>last:
            raise AlpacaError("calendar start 不得晚于 end")
        result=self._request("GET","/v2/calendar",params={"start":first.isoformat(),"end":last.isoformat()})
        if not isinstance(result,list) or any(not isinstance(row,dict) for row in result):
            raise AlpacaError("calendar 响应必须是对象数组")
        return result

    def get_positions(self) -> list[dict[str,Any]]:
        result=self._request("GET","/v2/positions")
        if not isinstance(result,list) or any(not isinstance(row,dict) for row in result):
            raise AlpacaError("positions 响应必须是对象数组")
        return result

    def get_orders(self, status: str = "open") -> list[dict[str,Any]]:
        if status not in ("open","closed","all"):
            raise AlpacaError("订单查询 status 仅支持 open、closed、all")
        result: list[dict[str,Any]]=[]
        params: dict[str,Any]={"status":status,"limit":500,"direction":"desc","nested":"false"}
        ids: set[str]=set()
        for _ in range(20):
            page=self._request("GET","/v2/orders",params=params)
            if not isinstance(page,list) or any(not isinstance(row,dict) for row in page):
                raise AlpacaError("orders 响应必须是对象数组")
            for row in page:
                identifier=self._identifier(row.get("id",""))
                if identifier not in ids:
                    ids.add(identifier)
                    result.append(row)
            if len(page)<500:
                return result
            cursor=self._identifier(page[-1].get("id",""))
            if cursor==params.get("before_order_id"):
                raise AlpacaError("订单分页未前进，安全停止，不能把不完整列表当作全部订单")
            params["before_order_id"]=cursor
        raise AlpacaError("订单查询达到 10000 条安全上限；不能把截断结果当作全部订单")

    def get_order(self, order_id: str) -> dict[str,Any] | None:
        result=self._request("GET","/v2/orders/"+self._identifier(order_id),none_on_404=True)
        return None if result is None else self._object(result,"order")

    def get_order_by_client_id(self, client_order_id: str) -> dict[str,Any] | None:
        result=self._request("GET","/v2/orders:by_client_order_id",
                             params={"client_order_id":self._client_id(client_order_id)},none_on_404=True)
        return None if result is None else self._object(result,"order")

    def get_contract(self, symbol: str) -> dict[str,Any] | None:
        result=self._request("GET","/v2/options/contracts/"+parse.quote(normalize_option_symbol(symbol),safe=""),none_on_404=True)
        return None if result is None else self._object(result,"option contract")

    @staticmethod
    def _feed(feed: str) -> str:
        if feed not in ("indicative","opra"):
            raise AlpacaError("行情 feed 只支持 indicative 或 opra")
        return feed

    def get_option_snapshot(self, symbol: str, feed: str = "indicative") -> dict[str,Any] | None:
        symbol=normalize_option_symbol(symbol)
        response=self._object(self._request("GET","/v1beta1/options/snapshots",data=True,
                                  params={"symbols":symbol,"feed":self._feed(feed),"limit":1}),"snapshots")
        snapshots=response.get("snapshots")
        if not isinstance(snapshots,dict):
            raise AlpacaError("snapshots 响应缺少按合约索引的 snapshots 字典")
        value=snapshots.get(symbol)
        return None if value is None else self._object(value,"single option snapshot")

    def get_option_quote(self, symbol: str, feed: str = "indicative") -> dict[str,Any] | None:
        symbol=normalize_option_symbol(symbol)
        return self.get_option_quotes([symbol], feed=feed).get(symbol)

    def get_option_quotes(self, symbols: list[str] | tuple[str, ...],
                         feed: str = "indicative") -> dict[str, dict[str, Any]]:
        """一次读取最多100个合约的最新报价；缺失合约不补造或逐个重拉。

        官方 multi-quotes 接口使用逗号分隔 symbols。返回每个合约的原始
        bp/ap/t 字典，调用者逐项检查源时间；一个合约缺失不代表其余合约失效。
        """
        if not isinstance(symbols, (list, tuple)):
            raise AlpacaError("批量期权 symbols 必须是合约列表，不接受字符串")
        normalized = sorted({normalize_option_symbol(symbol) for symbol in symbols})
        if not normalized:
            return {}
        if len(normalized) > 100:
            raise AlpacaError("一次批量期权报价最多100个不同合约")
        response=self._object(self._request("GET","/v1beta1/options/quotes/latest",data=True,
                                  params={"symbols":",".join(normalized),"feed":self._feed(feed)}),"quotes")
        quotes=response.get("quotes")
        if not isinstance(quotes,dict):
            raise AlpacaError("quotes 响应缺少按合约索引的 quotes 字典")
        result: dict[str, dict[str, Any]] = {}
        for symbol in normalized:
            value = quotes.get(symbol)
            if value is not None:
                result[symbol] = self._object(value, "single option quote")
        return result

    def submit_order(self, payload: Mapping[str,Any], *,
                     before_send: Callable[[], None] | None = None) -> dict[str,Any]:
        """创建 Paper 订单；before_send 在全部等待后、网络发送前调用。

        无参回调正常返回即通过；回调拒绝应抛异常，客户端会转为
        SubmissionNotSent 并隐藏异常原文。退出订单可不传回调。
        """
        if not isinstance(payload,Mapping):
            raise AlpacaError("订单 payload 必须是字典")
        allowed={"symbol","qty","side","type","time_in_force","limit_price","stop_price",
                 "client_order_id","position_intent","extended_hours","order_class"}
        if set(payload)-allowed:
            raise AlpacaError("单腿期权 payload 包含不支持字段；禁止 notional、legs、bracket 和 trailing_stop")
        order=dict(payload)
        order["symbol"]=normalize_option_symbol(order.get("symbol",""))
        order["qty"]=_positive_decimal(order.get("qty"),"qty",integer=True)
        order["client_order_id"]=self._client_id(order.get("client_order_id"))
        if order.get("side") not in ("buy","sell"):
            raise AlpacaError("订单 side 必须为 buy 或 sell")
        if order.get("type") not in ("market","limit","stop","stop_limit"):
            raise AlpacaError("期权 type 仅支持 market、limit、stop、stop_limit")
        if order.get("time_in_force") not in ("day","gtc"):
            raise AlpacaError("期权 time_in_force 仅支持 day 或 gtc")
        if "extended_hours" in order and order["extended_hours"] is not False:
            raise AlpacaError("期权不允许 extended_hours=True")
        if order.get("order_class","") not in ("","simple"):
            raise AlpacaError("该客户端只允许 simple 单腿期权订单")
        intent=order.get("position_intent")
        if intent is not None and intent not in ("buy_to_open","buy_to_close","sell_to_open","sell_to_close"):
            raise AlpacaError("position_intent 无效")
        if intent is not None and not intent.startswith(order["side"]+"_to_"):
            raise AlpacaError("position_intent 必须与 side 一致")
        for field,required in (("limit_price",order["type"] in ("limit","stop_limit")),
                               ("stop_price",order["type"] in ("stop","stop_limit"))):
            if required:
                order[field]=_positive_decimal(order.get(field),field)
            elif field in order:
                raise AlpacaError(f"当前 order type 不应携带 {field}")
        result=self._request("POST","/v2/orders",payload=order,client_order_id=order["client_order_id"],
                             before_send=before_send)
        if not isinstance(result,dict) or not result.get("id"):
            raise SubmissionUnknown("Paper 创建订单响应缺少 id；请按 client_order_id 对账",client_order_id=order["client_order_id"],operation="POST")
        return result

    def cancel_order(self, order_id: str) -> None:
        """204 仅为取消受理；调用者必须继续 GET 检查 canceled/filled 等终态。"""
        identifier=self._identifier(order_id)
        self._request("DELETE","/v2/orders/"+identifier,order_id=identifier)

    def replace_order(self, order_id: str, payload: Mapping[str,Any]) -> dict[str,Any]:
        identifier=self._identifier(order_id)
        if not isinstance(payload,Mapping) or not payload:
            raise AlpacaError("替换订单 payload 必须是非空字典")
        order=dict(payload)
        allowed={"qty","time_in_force","limit_price","stop_price","client_order_id"}
        if set(order)-allowed or not set(order)-{"client_order_id"}:
            raise AlpacaError("替换订单只允许 qty/TIF/limit_price/stop_price，不能只改 client_order_id")
        if "qty" in order:
            order["qty"]=_positive_decimal(order["qty"],"qty",integer=True)
        for field in ("limit_price","stop_price"):
            if field in order:
                order[field]=_positive_decimal(order[field],field)
        if "time_in_force" in order and order["time_in_force"] not in ("day","gtc"):
            raise AlpacaError("期权 time_in_force 仅支持 day 或 gtc")
        client_id=self._client_id(order["client_order_id"]) if "client_order_id" in order else ""
        result=self._request("PATCH","/v2/orders/"+identifier,payload=order,client_order_id=client_id,order_id=identifier)
        if not isinstance(result,dict) or not result.get("id"):
            raise SubmissionUnknown("Paper 替换订单响应缺少 id；请查询旧订单及 replaces/replaced_by 链后对账",client_order_id=client_id,operation="PATCH",order_id=identifier)
        return result

    def close(self) -> None:
        """取消限频/重试等待；已在途请求受 timeout_seconds 限制。"""
        self._cancel.set()

    def __enter__(self) -> "AlpacaPaperClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
