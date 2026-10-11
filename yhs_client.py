"""YHS REST 只读客户端：标准库、可取消重试与持久化额度保护。

官方契约以 https://api.yehangshe.com/v1/openapi.json 和 /v1/discover 为准。
只有数据 HTTP 200 确认扣额；网络断开且未知状态时额外保留一份安全预算，
不把它记成已确认的服务端消耗。所有实例应共用同一 quota_state_path。
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib import error, parse, request

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 16 * 1024 * 1024


class APIError(RuntimeError):
    """请求失败；错误不包含 API Key 或原始响应正文。"""

    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class QuotaExceeded(APIError):
    """本地预算或服务端额度熔断。"""


class AuthenticationError(APIError):
    """Key、订阅或权限错误，不能靠自动重试修复。"""


class RequestCancelled(APIError):
    """客户端被关闭或请求等待被取消。"""


class SchemaError(APIError):
    """成功响应无法按 JSON 对象/数组解析。"""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # 不将鉴权 header 自动转发到重定向主机。
        return None


_REGISTRY_LOCK = threading.Lock()
_GATES: dict[str, tuple[threading.RLock, threading.BoundedSemaphore, int]] = {}


def _shared_gate(path: Path, concurrency: int) -> tuple[threading.RLock, threading.BoundedSemaphore]:
    key = str(path.resolve()).casefold() if os.name == "nt" else str(path.resolve())
    with _REGISTRY_LOCK:
        item = _GATES.get(key)
        if item is None:
            item = (threading.RLock(), threading.BoundedSemaphore(concurrency), concurrency)
            _GATES[key] = item
        elif item[2] != concurrency:
            raise ValueError("共用额度文件的客户端必须采用相同并发限制")
        return item[0], item[1]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except ValueError:
        return None


def _process_alive(pid: int) -> bool | None:
    """只在明确进程已退出时回收槽位，访问被拒绝视为未知。

    Windows 的 os.kill(pid, 0) 行为不等同 POSIX，因此使用只读进程查询。
    """
    if pid == os.getpid():
        return True
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return False if ctypes.get_last_error() == 87 else None
            try:
                code = wintypes.DWORD()
                return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
            finally:
                kernel.CloseHandle(handle)
        except (ImportError, AttributeError, OSError):
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


class YHSClient:
    def __init__(self, config: Any, sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.monotonic,
                 opener: Callable[..., Any] | None = None):
        self.config = config
        self._sleep_fn, self._clock_fn = sleep_fn, clock_fn
        self._wall_offset = time.time() - clock_fn()
        self._cancel = threading.Event()
        self.base_url = str(config.api_base_url).rstrip("/")
        base = parse.urlsplit(self.base_url)
        if base.scheme != "https" or not base.hostname or base.username or base.password or base.query or base.fragment:
            raise ValueError("API base_url 必须是无用户名、查询参数及片段的 HTTPS 地址")
        if not 1 <= int(config.max_rpm) <= 50 or float(config.min_request_interval_seconds) < 1.2:
            raise ValueError("安全限制要求不超过 50 RPM 且请求起始间隔至少 1.2 秒")
        if not 1 <= int(config.max_concurrency) <= 3:
            raise ValueError("安全并发限制必须为 1 到 3")
        if not 0 < int(config.monthly_quota) <= 100_000 or not 0 <= int(config.quota_reserve) < int(config.monthly_quota):
            raise ValueError("月配额不得超过 100000，保留额度必须小于月配额")
        if not re.fullmatch(r"[A-Za-z0-9-]+", str(config.auth_header)) or any(c in str(config.auth_scheme) for c in "\r\n"):
            raise ValueError("鉴权 header 名称或 scheme 格式错误")
        if float(config.request_timeout_seconds) <= 0:
            raise ValueError("网络超时必须为正数")
        if not 0 < float(config.initial_backoff_seconds) <= float(config.max_backoff_seconds):
            raise ValueError("指数退避初始值及上限必须为正数且初始值不大于上限")
        self.state_path = Path(config.quota_state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_mutex, self._semaphore = _shared_gate(self.state_path, int(config.max_concurrency))
        self._opener = opener or request.build_opener(_NoRedirect()).open
        self.last_headers: dict[str, str] = {}
        self.last_status: int | None = None
        with self._state_transaction() as state:
            self._month(state)

    def _now(self) -> float:
        return self._wall_offset + self._clock_fn()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise RequestCancelled("请求已取消")

    def _sleep(self, seconds: float) -> None:
        # 分段等待，使 close()/Ctrl+C 能在一秒内中断长退避。
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_cancel()
            step = min(1.0, remaining)
            self._sleep_fn(step)
            remaining -= step
        self._check_cancel()

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    self._check_cancel()
                    if time.monotonic() >= deadline:
                        raise APIError("额度状态文件被其他进程占用超过 10 秒；安全停止")
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _state_transaction(self) -> Iterator[dict[str, Any]]:
        with self._state_mutex, self._file_lock():
            if self.state_path.exists():
                try:
                    state = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise APIError("额度状态文件无法读取；为避免超额禁止自动重置，请从备份恢复") from exc
                if not isinstance(state, dict) or state.get("version") != 1:
                    raise APIError("额度状态文件格式不兼容；为避免超额禁止自动重置")
                if not isinstance(state.get("months"), dict) or not isinstance(state.get("pending"), dict):
                    raise APIError("额度状态文件缺少账本字段；安全停止")
                if not isinstance(state.get("request_starts"), list) or any(_number(v) is None for v in state["request_starts"]):
                    raise APIError("额度状态文件的限频记录损坏；安全停止")
                if any(not isinstance(v, dict) or not isinstance(v.get("pid"), int) or not isinstance(v.get("month"), str)
                       for v in state["pending"].values()):
                    raise APIError("额度状态文件的在途记录损坏；安全停止")
            else:
                state = {"version": 1, "months": {}, "pending": {}, "request_starts": [],
                         "last_start": 0.0, "rate_blocked_until": 0.0,
                         "server_quota_remaining": None, "quota_reset_at": None}
            self._month(state)
            # 进程崩溃后，账本额度保持保守，明确已退出的进程才释放并发槽位。
            for token, pending in list(state["pending"].items()):
                if _process_alive(pending["pid"]) is False:
                    if pending.get("billable", True):
                        ledger = state["months"].setdefault(pending["month"], {"charged_200": 0, "uncertain": 0})
                        ledger["uncertain"] += 1
                    state["pending"].pop(token)
                    LOGGER.warning("回收已退出进程的未结请求槽位；数据请求额度转入未知状态安全预算")
            yield state
            temp = self.state_path.with_name(self.state_path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.state_path)
            except OSError as exc:
                raise APIError("额度状态无法持久化；为避免未记账请求，客户端安全停止") from exc
            finally:
                if temp.exists():
                    temp.unlink()

    def _month(self, state: dict[str, Any]) -> tuple[str, dict[str, int]]:
        now = self._now()
        month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        ledger = state["months"].setdefault(month, {"charged_200": 0, "uncertain": 0})
        if any(not isinstance(ledger.get(k), int) or ledger[k] < 0 for k in ("charged_200", "uncertain")):
            raise APIError("额度账本计数损坏；安全停止")
        reset_at = _number(state.get("quota_reset_at"))
        if reset_at is not None and now >= reset_at:
            state["server_quota_remaining"] = None
            state["quota_reset_at"] = None
        return month, ledger

    def _available(self, state: dict[str, Any]) -> tuple[int, int]:
        month, ledger = self._month(state)
        pending = sum(1 for item in state["pending"].values()
                      if item.get("month") == month and item.get("billable", True))
        remaining = int(self.config.monthly_quota) - ledger["charged_200"] - ledger["uncertain"] - pending
        remote = state.get("server_quota_remaining")
        if remote is not None:
            remaining = min(remaining, int(remote) - pending)
        return max(0, remaining), pending

    def budget_status(self) -> dict[str, Any]:
        """账本包含成功计费、未知状态安全预留及在途预留，均不含密钥。"""
        with self._state_transaction() as state:
            month, ledger = self._month(state)
            available, pending = self._available(state)
            return {"month": month, **ledger, "pending": pending, "available": available,
                    "in_flight": len(state["pending"]),
                    "normal_available": max(0, available - int(self.config.quota_reserve)),
                    "server_quota_remaining": state.get("server_quota_remaining"),
                    "quota_reset_at": state.get("quota_reset_at")}

    @property
    def quota_remaining(self) -> int:
        return int(self.budget_status()["available"])

    def _is_free(self, path: str) -> bool:
        # 由实际 URL 路径决定豁免，不能通过 category 假冒免费请求。
        full = parse.urlsplit(self._url(path, None)).path.rstrip("/")
        return full in ("/v1/discover", "/v1/health", "/v1/openapi.json")

    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        part = parse.urlsplit(path)
        if part.scheme or part.netloc or part.fragment or not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("端点必须是当前 API 的绝对路径，不接受外部 URL、片段或路径穿越")
        if part.query:
            raise ValueError("查询参数请通过 params 传入，不要拼入路径")
        # 同时支持官方 spec 的 /v1/... 与配置中相对 /v1 的路径。
        if self.base_url.endswith("/v1") and path.startswith("/v1/"):
            path = path[3:]
        url = self.base_url + path
        if params:
            clean = {key: value for key, value in params.items() if value is not None}
            if clean:
                url += "?" + parse.urlencode(clean, doseq=True)
        return url

    def _begin_attempt(self, path: str, free: bool, category: str) -> str | None:
        while True:
            self._check_cancel()
            wait = 0.0
            token = None
            with self._state_transaction() as state:
                now = self._now()
                month, _ = self._month(state)
                if not free:
                    available, _ = self._available(state)
                    reserve = 0 if category in ("essential", "priority") else int(self.config.quota_reserve)
                    if available <= reserve:
                        raise QuotaExceeded(f"额度熔断：可用 {available}，保留额度 {reserve}；停止新增数据请求")
                starts = [float(v) for v in state["request_starts"] if float(v) > now - 60.0]
                state["request_starts"] = starts
                wait = max(0.0, float(state.get("rate_blocked_until", 0)) - now,
                           float(state.get("last_start", 0)) + float(self.config.min_request_interval_seconds) - now)
                if len(starts) >= int(self.config.max_rpm):
                    wait = max(wait, min(starts) + 60.000001 - now)
                if len(state["pending"]) >= int(self.config.max_concurrency):
                    wait = max(wait, 0.2)
                if wait <= 0:
                    state["request_starts"].append(now)
                    state["last_start"] = now
                    token = uuid.uuid4().hex
                    state["pending"][token] = {"month": month, "started_at": now,
                                               "pid": os.getpid(), "path": path, "billable": not free}
            if wait <= 0:
                return token
            self._sleep(wait)

    def _observe_response(self, status: int, headers: Any, token: str | None) -> None:
        normalized = {str(k).lower(): str(v) for k, v in headers.items()} if headers is not None else {}
        self.last_headers, self.last_status = normalized, status
        with self._state_transaction() as state:
            _, ledger = self._month(state)
            if token is not None:
                pending = state["pending"].get(token)
                if status == 200 and pending is not None and pending.get("billable", True):
                    ledger["charged_200"] += 1
                if pending is not None:
                    # 费用在响应头到达时已确认；在途槽必须保留到 body 读完并 close。
                    pending["billable"] = False
                    pending["status_observed"] = status
            quota = _number(normalized.get("x-quota-remaining"))
            quota_reset = _epoch(normalized.get("x-quota-reset-at"))
            if quota_reset is not None and quota_reset != state.get("quota_reset_at"):
                old_reset = _number(state.get("quota_reset_at"))
                if old_reset is not None and old_reset <= self._now():
                    state["server_quota_remaining"] = None
                state["quota_reset_at"] = quota_reset
            if quota is not None:
                previous = state.get("server_quota_remaining")
                # 并发响应可乱序；同一计费周期采用保守的最小余量。
                state["server_quota_remaining"] = max(0, int(quota)) if previous is None else min(int(previous), max(0, int(quota)))
            rate_remaining = _number(normalized.get("x-ratelimit-remaining"))
            rate_reset = _epoch(normalized.get("x-ratelimit-reset"))
            if rate_remaining is not None and rate_remaining <= 0:
                state["rate_blocked_until"] = max(float(state.get("rate_blocked_until", 0)),
                                                  rate_reset if rate_reset is not None and rate_reset > self._now() else self._now() + 60.0)
        if quota is not None and quota <= 0:
            LOGGER.warning("服务端 X-Quota-Remaining=0：后续数据请求已熔断，免费 Discover 仍可使用")
        if rate_remaining is not None and rate_remaining <= 0:
            LOGGER.warning("服务端当前分钟余量为零：暂停请求直至窗口重置")

    def _uncertain(self, token: str | None) -> None:
        if token is None:
            return
        with self._state_transaction() as state:
            pending = state["pending"].pop(token, None)
            if pending is not None and pending.get("billable", True):
                ledger = state["months"].setdefault(pending["month"], {"charged_200": 0, "uncertain": 0})
                ledger["uncertain"] += 1
        if pending is not None and pending.get("billable", True):
            LOGGER.warning("连接中断且未收到 HTTP 状态：预留 1 单位安全预算，未记为已确认计费")

    def _release_attempt(self, token: str | None) -> None:
        if token is not None:
            with self._state_transaction() as state:
                state["pending"].pop(token, None)

    def _retry_after(self, headers: Any) -> float:
        values = {str(k).lower(): str(v) for k,v in headers.items()} if headers is not None else {}
        value = values.get("retry-after")
        numeric = _number(value)
        if numeric is not None:
            return max(0.0, numeric)
        if value:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - self._now())
            except (ValueError, TypeError, OverflowError):
                pass
        return 0.0

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any] | list[Any]:
        try:
            result = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SchemaError("响应不是有效 UTF-8 JSON；若 HTTP 200 则该次仍然计费") from exc
        if not isinstance(result, (dict, list)):
            raise SchemaError("响应 JSON 必须是对象或数组")
        return result

    @staticmethod
    def _error_code(body: bytes) -> str:
        try:
            value = json.loads(body.decode("utf-8-sig"))
            item = value.get("error", value) if isinstance(value, dict) else {}
            code = item.get("code", "") if isinstance(item, dict) else ""
            return str(code) if re.fullmatch(r"[A-Z0-9_]{1,100}", str(code)) else ""
        except (ValueError, UnicodeDecodeError):
            return ""

    def get(self, path: str, params: Mapping[str, Any] | None = None,
            category: str = "normal") -> dict[str, Any] | list[Any]:
        """GET；202 无上限指数退避，close() / Ctrl+C 可中断。

        数据请求即使收到 202 也必须预留可能的下一次 200；月额为零时停止
        202 轮询，防止下一次成功越额。元数据请求始终免月额，仍遵守限频。
        """
        url = self._url(path, params)
        free = self._is_free(path)
        auth_needed = parse.urlsplit(url).path not in ("/v1/health", "/v1/openapi.json")
        if auth_needed and not self.config.api_key:
            raise AuthenticationError("未设置 YHS_API_KEY 环境变量，无法访问需要鉴权的端点")
        headers = {"Accept": "application/json", "User-Agent": self.config.user_agent}
        if self.config.api_key:
            if "\r" in self.config.api_key or "\n" in self.config.api_key:
                raise AuthenticationError("API Key 含非法换行")
            headers[str(self.config.auth_header)] = (str(self.config.auth_scheme) + " " + self.config.api_key).strip()
        network_errors = 0
        backoff = float(self.config.initial_backoff_seconds)
        while True:
            self._check_cancel()
            while not self._semaphore.acquire(timeout=0.2):
                self._check_cancel()
            token: str | None = None
            observed = False
            response = None
            status = 0
            response_headers: Any = {}
            body = b""
            transport_error = False
            try:
                token = self._begin_attempt(path, free, category)
                req = request.Request(url, headers=headers, method="GET")
                try:
                    response = self._opener(req, timeout=float(self.config.request_timeout_seconds))
                except error.HTTPError as exc:
                    response = exc
                status = int(response.getcode())
                response_headers = response.headers
                self._observe_response(status, response_headers, token)
                observed = True
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise SchemaError("响应超过 16 MiB 安全上限；请缩小查询范围")
            except (error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if not observed:
                    self._uncertain(token)
                network_errors += 1
                if network_errors > int(self.config.max_error_retries):
                    raise APIError(f"网络连接连续失败 {network_errors} 次；停止该请求") from exc
                transport_error = True
                LOGGER.warning("网络连接失败，%s 秒后重试（%s/%s）", backoff, network_errors, self.config.max_error_retries)
            except BaseException:
                # 已收到状态时账本已结算；否则保留未知请求预算。
                if token is not None and not observed:
                    self._uncertain(token)
                raise
            finally:
                try:
                    if response is not None:
                        response.close()
                finally:
                    try:
                        self._release_attempt(token)
                    finally:
                        self._semaphore.release()
            if transport_error:
                self._sleep(backoff)
                backoff = min(float(self.config.max_backoff_seconds), backoff * 2)
                continue
            if status == 200:
                LOGGER.debug("GET %s 成功，月额消耗=%s", path, 0 if free else 1)
                return self._decode(body)
            code = self._error_code(body)
            if status in (401, 402, 403):
                raise AuthenticationError(f"鉴权/订阅/权限失败：HTTP {status} {code}；请检查账户和 Key", status, code)
            if status == 429 and code == "QUOTA_EXHAUSTED":
                with self._state_transaction() as state:
                    state["server_quota_remaining"] = 0
                raise QuotaExceeded("服务端月配额耗尽，数据请求已熔断", status, code)
            if status == 202:
                wait = max(backoff, self._retry_after(response_headers))
                LOGGER.info("GET %s 数据准备中（202，免费），%.1f 秒后重试", path, wait)
                self._sleep(wait)
                backoff = min(float(self.config.max_backoff_seconds), backoff * 2)
                continue
            if status == 429 or 500 <= status <= 599 or status == 408:
                network_errors += 1
                if network_errors > int(self.config.max_error_retries):
                    raise APIError(f"GET {path} 重试后仍失败：HTTP {status} {code}", status, code)
                wait = max(backoff, self._retry_after(response_headers))
                if status == 429:
                    own_headers = {str(k).lower(): str(v) for k,v in response_headers.items()}
                    reset = _epoch(own_headers.get("x-ratelimit-reset"))
                    if reset is not None:
                        wait = max(wait, reset - self._now())
                LOGGER.warning("GET %s HTTP %s %s（免费），%.1f 秒后重试", path, status, code, wait)
                self._sleep(wait)
                backoff = min(float(self.config.max_backoff_seconds), backoff * 2)
                continue
            raise APIError(f"GET {path} 请求失败：HTTP {status} {code}；不会重复请求参数/端点错误", status, code)

    def discover(self) -> dict[str, Any]:
        result = self.get(self.config.discover_path, category="metadata")
        if not isinstance(result, dict):
            raise SchemaError("Discover 响应必须是 JSON 对象")
        # 官方推荐的 monthly_remaining：当中间代理剥离响应头时仍可熔断。
        containers = [result]
        while containers:
            node = containers.pop()
            value = _number(node.get("monthly_remaining"))
            if value is not None:
                with self._state_transaction() as state:
                    current = state.get("server_quota_remaining")
                    state["server_quota_remaining"] = max(0, int(value)) if current is None else min(int(current), max(0, int(value)))
                break
            containers.extend(item for item in node.values() if isinstance(item, dict))
        return result

    def fetch_volume_top(self, limit: int = 100) -> dict[str, Any] | list[Any]:
        if not 1 <= limit <= 100:
            raise ValueError("初筛榜单截取数量必须为 1–100")
        # 官方 Top150 路由无查询参数；调用一次后 detector 在本地取 Top100。
        return self.get(self.config.volume_path)

    @staticmethod
    def _ticker(ticker: str) -> str:
        symbol = str(ticker).upper().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9]*(\.[A-Z0-9]+)*", symbol):
            raise ValueError("标的代码格式错误")
        return symbol

    def fetch_heatmap(self, ticker: str, category: str = "normal") -> dict[str, Any] | list[Any]:
        symbol = self._ticker(ticker)
        path = self.config.heatmap_path
        if "{ticker}" not in path:
            raise APIError("heatmap_path 必须使用官方 /derived/heatmap/{ticker}/snapshot 模板")
        return self.get(path.replace("{ticker}", parse.quote(symbol, safe="")), category=category)

    def fetch_flow(self, ticker: str, min_premium: float = 50_000) -> dict[str, Any] | list[Any]:
        if not math.isfinite(float(min_premium)) or float(min_premium) < 0:
            raise ValueError("最低权利金必须是非负有限值")
        return self.get(self.config.flow_path,
                        {"tickers": self._ticker(ticker), "kind": "order", "min_premium_usd": min_premium,
                         "sort_by": "occurredAt", "direction": "desc", "limit": 100})

    def fetch_option_chain(self, ticker: str, expiration: str | None = None) -> dict[str, Any] | list[Any]:
        if not expiration:
            raise APIError("期权链必须指定 expiration=YYYY-MM-DD；请使用信号近周到期日")
        try:
            date.fromisoformat(expiration)
        except (TypeError, ValueError) as exc:
            raise ValueError("期权链到期日格式必须为 YYYY-MM-DD") from exc
        path = self.config.chain_path
        if not path or "{ticker}" not in path:
            raise APIError("未配置官方 chain_path；请通过 Discover 确认 /options/chain-snapshot/{ticker} 权限")
        result = self.get(path.replace("{ticker}", parse.quote(self._ticker(ticker), safe="")),
                          {"expiration": expiration, "limit": 400})
        if isinstance(result, dict) and isinstance(result.get("_meta"), dict) and result["_meta"].get("next_cursor"):
            LOGGER.warning("期权链第一页未覆盖全部合约；为遵守开仓单次拉链预算不自动翻页，缺少目标合约时放弃开仓")
        return result

    def close(self) -> None:
        """取消等待和后续重试；正在进行的 urllib 请求由配置超时约束。"""
        self._cancel.set()

    def __enter__(self) -> "YHSClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
