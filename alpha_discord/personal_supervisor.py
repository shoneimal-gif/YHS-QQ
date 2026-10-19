"""Supervise only the local signal reader, without accessing any credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .common import atomic_json

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "alpha_personal_local"


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        raise SystemExit("The local launcher requires Windows.")
    import msvcrt
    lock_path = RUNTIME / "supervisor.lock"
    if lock_path.is_symlink():
        raise SystemExit("Linked supervisor lock refused.")
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        print('{"supervisor_already_running":true}')
        return 0
    state = {"mode": "local_personal_signal_supervisor", "pid": os.getpid(),
             "observer_only": True, "orders_submitted": False, "restarts": 0}
    process = None
    output = None
    try:
        output = (RUNTIME / "reader.log").open("ab", buffering=0)
        while not (RUNTIME / "stop.request").exists():
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m",
                "alpha_discord.personal_local", "run"], cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
            state.update(running=True, child_pid=process.pid,
                         updated_utc=datetime.now(timezone.utc).isoformat())
            atomic_json(RUNTIME / "supervisor_status.json", state)
            while process.poll() is None and not (RUNTIME / "stop.request").exists():
                state["updated_utc"] = datetime.now(timezone.utc).isoformat()
                atomic_json(RUNTIME / "supervisor_status.json", state)
                time.sleep(1)
            if (RUNTIME / "stop.request").exists():
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
                break
            state.update(running=False, last_exit_code=process.returncode,
                         updated_utc=datetime.now(timezone.utc).isoformat())
            category = None
            for name in ("status.json", "launcher_error.json"):
                try:
                    current = json.loads((RUNTIME / name).read_text(encoding="utf-8"))
                    category = current.get("error_category") or category
                except (OSError, ValueError):
                    pass
            if category in {"authentication_failed", "personal_identity_mismatch",
                    "channel_permission_denied", "source_channel_binding_failed",
                    "unexpected_local_source_configuration", "destination_webhook_binding_failed",
                    "windows_credential_decryption_failed", "personal_account_required",
                    "source_permission_denied", "source_unavailable", "personal_account_mismatch"}:
                state["halted_category"] = category
                atomic_json(RUNTIME / "supervisor_status.json", state)
                return 1
            state["restarts"] += 1
            atomic_json(RUNTIME / "supervisor_status.json", state)
            delay = min(60, 5 * 2 ** min(state["restarts"] - 1, 4))
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not (RUNTIME / "stop.request").exists():
                time.sleep(1)
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        state.update(running=False, updated_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(RUNTIME / "supervisor_status.json", state)
        if output is not None:
            output.close()
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
