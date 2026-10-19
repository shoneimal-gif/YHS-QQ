"""Linux service entry point for the isolated personal signal observer.

Deployment layout is intentionally precise: /opt/yhs-qq/alpha-personal/runtime
is an ordinary directory; its alpha_personal_local child is a symlink pointing
to /var/lib/yhs-qq/alpha-personal. Pass the canonical /var/lib path to the
observer so its existing exact-runtime validation remains unchanged.

Credentials come only from systemd LoadCredential. No Windows/DPAPI module,
trading credentials, broker code, or other observer runtime is imported.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import errno
import json
import logging
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
from typing import Any

from .common import atomic_json, utc_now


APP = Path("/opt/yhs-qq/alpha-personal")
RUNTIME = Path("/var/lib/yhs-qq/alpha-personal")
SOURCE_GUILD = "1434960637561409689"
SOURCE_CHANNEL = "1492437553171660921"
SOURCE_AUTHOR = "1462345436852654295"
TARGET_GUILD = "1111111111111111111"
TARGET_CHANNEL = "2222222222222222222"
CLOUD_BINDING = "yhs_alpha_personal_cloud_v1"
CREDENTIAL_DIRECTORY = Path("/run/credentials/alpha-personal.service")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{32,512}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
LOG = logging.getLogger("alpha_discord.personal_cloud")


class CloudError(Exception):
    """Fixed diagnostic categories contain no credential or response text."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


def _acl(fd: int) -> list[tuple[int, int, int]] | None:
    """Decode only Linux's POSIX ACL access attribute; never credential data."""
    try:
        raw = os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        raise CloudError("credential_acl_unconfirmed") from None
    if len(raw) < 4 or len(raw) > 1024 or (len(raw)-4) % 8 or struct.unpack_from("<I", raw)[0] != 2:
        raise CloudError("credential_acl_invalid")
    return [struct.unpack_from("<HHI", raw, offset) for offset in range(4, len(raw), 8)]


def _credential_permissions(fd: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    valid_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid_kind:
        raise CloudError("credential_permissions_invalid")
    mode = stat.S_IMODE(info.st_mode)
    access = _acl(fd)
    uid = os.getuid()
    # Also support a service-owned private copy: no named ACL entries, no
    # group/other access, and only the documented private read/read-write modes.
    private_modes = (0o500, 0o700) if directory else (0o400, 0o600)
    undefined = 0xFFFFFFFF
    base_acl = [(1, (mode >> 6) & 7, undefined), (4, 0, undefined), (32, 0, undefined)]
    if info.st_uid == uid and uid != 0 and mode in private_modes and access in (None, base_acl):
        return
    # Observed systemd LoadCredential layout: root-owned 0550/0440 with an
    # ACL granting this service UID its read(/search) access. Group and other
    # entries grant zero; no other named users/groups are accepted. The mount
    # itself must be read-only. Original /etc root-0600 files stay unchanged.
    permission = 5 if directory else 4
    expected = [(1, permission, undefined), (2, permission, uid),
                (4, 0, undefined), (16, permission, undefined), (32, 0, undefined)]
    if (uid != 0 and info.st_uid == 0 and info.st_gid == 0
            and mode == (0o550 if directory else 0o440) and access == expected
            and os.fstatvfs(fd).f_flag & os.ST_RDONLY):
        return
    raise CloudError("credential_permissions_invalid")


def _credential(directory: Path, name: str, maximum: int) -> dict[str, Any]:
    directory_fd: int | None = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _credential_permissions(directory_fd, directory=True)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as stream:
            _credential_permissions(stream.fileno(), directory=False)
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise CloudError("credential_document_too_large")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise CloudError("credential_document_invalid")
        return value
    except CloudError:
        raise
    except Exception:
        raise CloudError("credential_load_failed") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _layout() -> None:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise CloudError("linux_service_required")
    if Path(__file__).resolve().parents[1] != APP or APP.is_symlink():
        raise CloudError("cloud_application_binding_failed")
    app_runtime = APP / "runtime"
    personal_link = app_runtime / "alpha_personal_local"
    if (not app_runtime.is_dir() or app_runtime.is_symlink()
            or not personal_link.is_symlink() or personal_link.resolve() != RUNTIME
            or RUNTIME.is_symlink() or RUNTIME.parent.is_symlink()):
        raise CloudError("cloud_runtime_layout_invalid")
    info = RUNTIME.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CloudError("cloud_runtime_permissions_invalid")


def load_cloud() -> tuple[dict[str, Any], str, str]:
    """Read only the two systemd credentials, with fixed source/destination."""
    _layout()
    selected = os.environ.get("CREDENTIALS_DIRECTORY")
    if not isinstance(selected, str) or not selected or len(selected) > 4096:
        raise CloudError("systemd_credentials_required")
    directory = Path(selected)
    if (directory != CREDENTIAL_DIRECTORY or directory.is_symlink() or not directory.is_dir()
            or any(parent.is_symlink() for parent in directory.parents)):
        raise CloudError("systemd_credential_directory_invalid")
    config = _credential(directory, "config", 16384)
    fixed = {
        "schema_version": 1, "mode": "personal_account_cloud_observer",
        "source_mode": "direct_personal_account", "observer_only": True,
        "source_guild_id": SOURCE_GUILD, "source_channel_id": SOURCE_CHANNEL,
        "source_author_id": SOURCE_AUTHOR, "target_guild_id": TARGET_GUILD,
        "target_channel_id": TARGET_CHANNEL, "max_signal_age_seconds": 120,
        "cloud_binding": CLOUD_BINDING,
    }
    if set(config) != set(fixed) | {"account_binding_sha256"}:
        raise CloudError("cloud_configuration_invalid")
    for key, value in fixed.items():
        if type(config.get(key)) is not type(value) or config[key] != value:
            raise CloudError("cloud_configuration_binding_failed")
    if not isinstance(config.get("account_binding_sha256"), str) or not _SHA.fullmatch(config["account_binding_sha256"]):
        raise CloudError("cloud_account_binding_invalid")
    credentials = _credential(directory, "discord-personal", 16384)
    if set(credentials) != {"DISCORD_USER_TOKEN", "FORWARD_WEBHOOK"}:
        raise CloudError("cloud_credential_schema_invalid")
    token, webhook = credentials["DISCORD_USER_TOKEN"], credentials["FORWARD_WEBHOOK"]
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise CloudError("personal_credential_format_invalid")
    if not isinstance(webhook, str) or not 64 <= len(webhook) <= 1024:
        raise CloudError("forward_webhook_format_invalid")
    # SignalForwarder independently validates the exact Discord webhook URL
    # and verifies its actual guild/channel before forwarding any signal.
    return config, token, webhook


def _runner_status(*, running: bool, category: str | None, check_only: bool) -> None:
    atomic_json(RUNTIME / "runner_status.json", {
        "version": 1, "mode": "alpha_personal_cloud_runtime",
        "cloud_binding": CLOUD_BINDING, "observer_only": True,
        "eligible_for_orders": False, "orders_submitted": False,
        "personal_account_messages_sent": False,
        "running": running, "check_only": check_only, "error_category": category,
        "updated_utc": utc_now().isoformat(),
    })


async def run_cloud(*, check_only: bool = False) -> int:
    # The SDK import is isolated from the existing bot, stock and option apps.
    from .personal_forwarder import SignalForwarder
    from .personal_observer import _ProcessLock, _silence_sdk_logs, run_observer

    _silence_sdk_logs()
    config, token, webhook = load_cloud()
    launch_lock = _ProcessLock(RUNTIME / "launcher.lock")
    forwarder: SignalForwarder | None = None
    tasks: list[asyncio.Task[Any]] = []
    stop = asyncio.Event()
    category: str | None = None
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        _runner_status(running=True, category=None, check_only=check_only)
        if not check_only:
            # Both databases and the forwarder are covered by launcher.lock.
            forwarder = SignalForwarder(webhook, RUNTIME / "forwarder",
                                        target_guild_id=TARGET_GUILD, target_channel_id=TARGET_CHANNEL)
        observer = asyncio.create_task(run_observer(config, token, RUNTIME,
            forwarder=forwarder, stop_event=stop, check_only=check_only))
        tasks.append(observer)
        if forwarder is not None:
            tasks.append(asyncio.create_task(forwarder.run(stop)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if task is observer:
                if type(result) is not int or result != 0:
                    raise CloudError("personal_observer_failed")
                if not stop.is_set() and not check_only:
                    raise CloudError("personal_observer_stopped_unexpectedly")
            elif not stop.is_set():
                raise CloudError("signal_forwarder_stopped_unexpectedly")
        return 0
    except asyncio.CancelledError:
        category = "cloud_runtime_cancelled"
        raise
    except CloudError as exc:
        category = exc.category
        return 1
    except Exception:
        category = "cloud_runtime_failed"
        return 1
    finally:
        stop.set()
        if tasks:
            # The forwarder HTTP timeout is 10 seconds. Allow an active POST
            # to finish and save its outcome before shutting down its database.
            _, pending = await asyncio.wait(tasks, timeout=30)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if forwarder is not None:
            with suppress(Exception):
                await forwarder.aclose()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        with suppress(Exception):
            _runner_status(running=False, category=category, check_only=check_only)
        launch_lock.close()
        if category:
            LOG.error("personal_cloud_error category=%s", category)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Isolated cloud AlphaAgent signal observer and informational forwarder")
    parser.add_argument("--check-only", action="store_true", help="Read source history once, without forwarding or orders")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return asyncio.run(run_cloud(check_only=args.check_only))
    except KeyboardInterrupt:
        return 0
    except CloudError as exc:
        LOG.error("personal_cloud_error category=%s", exc.category)
        return 1
    except Exception:
        LOG.error("personal_cloud_error category=cloud_startup_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
