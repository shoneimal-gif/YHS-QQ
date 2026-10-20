"""Windows-local personal reader setup and launcher; never import a broker.

Credentials stay DPAPI-encrypted for the current Windows user. Network setup
can only read the bound source or manage our own destination webhook. A
write with an uncertain result is recovered by its unique name, never retried.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .common import atomic_json

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".private_config" / "discord_personal_local"
RUNTIME = ROOT / "runtime" / "alpha_personal_local"
SOURCE_GUILD = "1434960637561409689"
SOURCE_CHANNEL = "1492437553171660921"
SOURCE_AUTHOR = "1462345436852654295"
TARGET_GUILD = "1111111111111111111"
TARGET_CHANNEL = "2222222222222222222"
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{32,512}\Z")
_WEBHOOK = re.compile(r"/api/v10/webhooks/([1-9][0-9]{0,19})/([A-Za-z0-9_-]{20,256})\Z")


class LocalError(Exception):
    def __init__(self, category: str, status: int | None = None):
        super().__init__(category)
        self.category, self.status = category, status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ordinary(path: Path, *, directory: bool = False) -> None:
    info = path.stat(follow_symlinks=False)
    if (path.is_symlink() or getattr(info, "st_file_attributes", 0) &
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
        raise LocalError("linked_private_path_refused")
    correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_kind:
        raise LocalError("unexpected_private_path")


def _load_json(path: Path, maximum: int = 32768):
    _ordinary(path)
    if path.stat().st_size > maximum:
        raise LocalError("configuration_too_large")
    return json.loads(path.read_text(encoding="utf-8-sig"))


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise LocalError("windows_current_user_required")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.restype = wintypes.BOOL
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    buffer = ctypes.create_string_buffer(value)
    incoming = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    try:
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise LocalError("windows_credential_decryption_failed" if not protect else "windows_credential_encryption_failed")
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(ctypes.cast(outgoing.data, wintypes.HLOCAL))


def _read_secret(name: str) -> str:
    path = PRIVATE / name
    _ordinary(PRIVATE, directory=True)
    _ordinary(path)
    if path.stat().st_size > 16384:
        raise LocalError("encrypted_credential_too_large")
    encoded = path.read_text(encoding="utf-8-sig").strip()
    try:
        return _dpapi(bytes.fromhex(encoded), protect=False).decode("utf-16-le")
    except LocalError:
        raise
    except Exception:
        raise LocalError("invalid_encrypted_credential") from None


def _save_secret(name: str, value: str) -> None:
    _ordinary(PRIVATE, directory=True)
    path = PRIVATE / name
    if path.exists():
        _ordinary(path)
    temporary = PRIVATE / (".dpapi-" + uuid.uuid4().hex + ".tmp")
    encoded = _dpapi(value.encode("utf-16-le"), protect=True).hex()
    try:
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_local():
    _ordinary(PRIVATE, directory=True)
    cfg = _load_json(PRIVATE / "observer_config.json")
    if (not isinstance(cfg, dict) or cfg.get("schema_version") != 1 or
            cfg.get("mode") != "personal_account_local_observer" or cfg.get("observer_only") is not True or
            cfg.get("credential_storage") != "windows_dpapi_current_user" or
            cfg.get("credential_filename") != "user_token.dpapi" or
            cfg.get("source_guild_id") != SOURCE_GUILD or cfg.get("source_channel_id") != SOURCE_CHANNEL or
            cfg.get("expected_source_bot_id") != SOURCE_AUTHOR or
            not isinstance(cfg.get("account_binding_sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", cfg["account_binding_sha256"]) or
            cfg.get("max_signal_age_seconds") != 120):
        raise LocalError("unexpected_local_source_configuration")
    token = _read_secret("user_token.dpapi")
    if not _TOKEN.fullmatch(token):
        raise LocalError("invalid_personal_credential_format")
    cfg.update(source_author_id=SOURCE_AUTHOR,
               target_guild_id=TARGET_GUILD, target_channel_id=TARGET_CHANNEL)
    return cfg, token


def _request(token: str, path: str, *, payload=None):
    reads = {"/users/@me", "/channels/" + SOURCE_CHANNEL,
             "/channels/" + SOURCE_CHANNEL + "/messages?limit=1",
             "/channels/" + TARGET_CHANNEL,
             "/channels/" + TARGET_CHANNEL + "/webhooks"}
    if path not in reads or (payload is not None and path != "/channels/" + TARGET_CHANNEL + "/webhooks"):
        raise LocalError("api_scope_refused")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request("https://discord.com/api/v10" + path, data=body,
                      headers={"Authorization": token, "Content-Type": "application/json",
                               "User-Agent": "YHSAlphaLocalObserver/1.0"},
                      method="GET" if body is None else "POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=20) as reply:
            data = reply.read(1048577)
            if len(data) > 1048576:
                raise LocalError("oversize_api_response")
            return json.loads(data)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise LocalError("discord_http_failed", code) from None
    except LocalError:
        raise
    except Exception:
        raise LocalError("discord_result_unknown" if body is not None else "discord_read_failed") from None


def _webhook_url(data: dict) -> str:
    hook_id, token = data.get("id"), data.get("token")
    if (data.get("channel_id") != TARGET_CHANNEL or data.get("guild_id") != TARGET_GUILD or
            data.get("type") != 1 or not isinstance(hook_id, str) or not _ID.fullmatch(hook_id) or
            not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token)):
        raise LocalError("destination_webhook_binding_failed")
    return f"https://discord.com/api/v10/webhooks/{hook_id}/{token}"


def _safe_webhook() -> str:
    url = _read_secret("forward_webhook.dpapi")
    split = urlsplit(url)
    if split.scheme != "https" or split.netloc != "discord.com" or split.query or split.fragment or not _WEBHOOK.fullmatch(split.path):
        raise LocalError("invalid_forward_webhook")
    cfg = _load_json(PRIVATE / "forwarder_config.json")
    if (cfg.get("target_guild_id") != TARGET_GUILD or cfg.get("target_channel_id") != TARGET_CHANNEL or
            cfg.get("observer_only") is not True):
        raise LocalError("destination_configuration_mismatch")
    return url


def configure_forwarder():
    cfg, token = load_local()
    identity = _request(token, "/users/@me")
    account_id = identity.get("id")
    if (not isinstance(account_id, str) or identity.get("bot", False) or
            hashlib.sha256(account_id.encode()).hexdigest() != cfg["account_binding_sha256"]):
        raise LocalError("personal_identity_mismatch")
    channel = _request(token, "/channels/" + TARGET_CHANNEL)
    if channel.get("id") != TARGET_CHANNEL or channel.get("guild_id") != TARGET_GUILD or channel.get("type") not in (0, 5):
        raise LocalError("destination_channel_mismatch")
    journal_path = PRIVATE / "webhook_setup.json"
    if journal_path.exists():
        journal = _load_json(journal_path)
        if (journal.get("target_channel_id") != TARGET_CHANNEL or
                journal.get("account_binding_sha256") != cfg["account_binding_sha256"]):
            raise LocalError("webhook_setup_binding_mismatch")
    else:
        journal = {"name": "AlphaAgent Sync " + uuid.uuid4().hex[:12],
                   "account_binding_sha256": cfg["account_binding_sha256"],
                   "target_channel_id": TARGET_CHANNEL, "attempted": False,
                   "outcome": "prepared"}
        atomic_json(journal_path, journal)
    if journal.get("attempted"):
        existing = _request(token, "/channels/" + TARGET_CHANNEL + "/webhooks")
        if not isinstance(existing, list):
            raise LocalError("invalid_webhook_recovery_response")
        matching = [x for x in existing if isinstance(x, dict) and
                    x.get("name") == journal["name"] and
                    isinstance(x.get("user"), dict) and x["user"].get("id") == account_id]
        if len(matching) != 1:
            raise LocalError("webhook_write_needs_recovery")
        url = _webhook_url(matching[0])
    else:
        journal.update(attempted=True, outcome="unknown",
                       attempted_at_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(journal_path, journal)
        try:
            data = _request(token, "/channels/" + TARGET_CHANNEL + "/webhooks",
                            payload={"name": journal["name"]})
            url = _webhook_url(data)
        except LocalError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                journal.update(attempted=False, outcome="rejected", http_status=exc.status)
                atomic_json(journal_path, journal)
            raise
    _save_secret("forward_webhook.dpapi", url)
    atomic_json(PRIVATE / "forwarder_config.json", {
        "version": 1, "observer_only": True,
        "target_guild_id": TARGET_GUILD, "target_channel_id": TARGET_CHANNEL,
        "secret_filename": "forward_webhook.dpapi"})
    journal.update(outcome="confirmed")
    atomic_json(journal_path, journal)
    return {"forwarder_configured": True, "target_channel_id": TARGET_CHANNEL,
            "credential_storage": "windows_dpapi_current_user", "messages_sent": 0,
            "cloud_uploaded": False, "orders_submitted": False}


async def run_local(check_only=False):
    from .personal_observer import _ProcessLock, run_observer
    cfg, token = load_local()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    launch_lock = _ProcessLock(RUNTIME / "launcher.lock")
    forwarder = None
    try:
        if not check_only:
            from .personal_forwarder import SignalForwarder
            forwarder = SignalForwarder(_safe_webhook(), RUNTIME / "forwarder",
                                       target_guild_id=TARGET_GUILD, target_channel_id=TARGET_CHANNEL)
    except BaseException:
        launch_lock.close()
        raise
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
            except (ValueError, OSError):
                pass
    tasks = [asyncio.create_task(run_observer(cfg, token, RUNTIME,
                    forwarder=forwarder, stop_event=stop, check_only=check_only))]
    async def watch_stop():
        while not stop.is_set() and not (RUNTIME / "stop.request").exists():
            await asyncio.sleep(1)
        stop.set()
    tasks.append(asyncio.create_task(watch_stop()))
    if forwarder is not None:
        tasks.append(asyncio.create_task(forwarder.run(stop)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if type(result) is int and result != 0:
                category = "personal_observer_failed"
                try:
                    candidate = _load_json(RUNTIME / "status.json").get("error_category")
                    if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,80}", candidate):
                        category = candidate
                except (OSError, ValueError, LocalError):
                    pass
                raise LocalError(category)
    finally:
        stop.set()
        _, pending = await asyncio.wait(tasks, timeout=15)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if forwarder is not None:
            result = forwarder.close()
            if asyncio.iscoroutine(result):
                await result
        launch_lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("configure-forwarder", "run", "check", "status"))
    args = parser.parse_args(argv)
    # Third-party diagnostic logs can contain account, channel or HTTP details.
    logging.disable(logging.CRITICAL)
    try:
        if args.action == "configure-forwarder":
            result = configure_forwarder()
        elif args.action == "status":
            result = _load_json(RUNTIME / "status.json")
        else:
            asyncio.run(run_local(check_only=args.action == "check"))
            result = {"reader_stopped": True, "observer_only": True, "orders_submitted": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        category = exc.category if isinstance(exc, LocalError) else "personal_local_failed"
        result = {"mode": "discord_personal_local_setup", "success": False,
                  "error_category": category,
                  "http_status": exc.status if isinstance(exc, LocalError) else None,
                  "cloud_uploaded": False, "orders_submitted": False}
        RUNTIME.mkdir(parents=True, exist_ok=True)
        atomic_json(RUNTIME / "launcher_error.json", result)
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
