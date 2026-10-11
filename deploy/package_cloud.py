"""Build a private Linux deployment archive; never include API credentials."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "alpaca_client.py", "alpaca_trader.py", "config.py", "detector.py",
    "main.py", "mock_replay.py", "paper_trader.py", "yhs_client.py", "wecom_notifications.py",
    "config.alpaca_paper_100k.json", "alpaca_policy_100k.json",
)
LINUX_FILES = (
    "install.sh", "runner.py", "configure_keys.py", "yhs-qq.service", "yhs-qq.timer",
)


@contextlib.contextmanager
def hold_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("本目录仍有运行进程，请先停止再打包迁移。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def runtime_files():
    runtime = ROOT / "runtime"
    if not runtime.exists():
        return []
    result = []
    for path in sorted(runtime.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("本地 runtime 含符号链接，需先检查后迁移。")
        if not path.is_file():
            continue
        relative = path.relative_to(runtime)
        if any(part.startswith("replay") or part == "__pycache__" for part in relative.parts):
            continue
        if path.suffix.lower() in (".lock", ".tmp", ".log", ".pyc"):
            continue
        if path.name.startswith(".env") or path.name in ("credentials.json", "auth.json"):
            raise RuntimeError("runtime 中发现密钥文件，拒绝打包。")
        result.append(path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = [ROOT / name for name in SOURCE_FILES]
    paths += [ROOT / "deploy" / "linux" / name for name in LINUX_FILES]
    if (ROOT / "CLOUD_DEPLOY.md").exists():
        paths.append(ROOT / "CLOUD_DEPLOY.md")
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"部署文件缺失或含符号链接：{path.relative_to(ROOT)}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (args.output or ROOT / ".private_deploy" / f"yhs-qq-{stamp}.tar.gz").resolve()
    if destination.exists():
        raise RuntimeError("目标部署包已存在，请使用新的文件名。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    locks = set((ROOT / "runtime").rglob("*.lock"))
    locks.add(ROOT / "runtime" / "alpaca_paper_100k" / "runner.lock")
    accounts = ROOT / "runtime" / "alpaca_accounts"
    if accounts.exists():
        for account in accounts.iterdir():
            if account.is_dir() and not account.is_symlink():
                locks.add(account / "runner.lock")
                locks.add(account / "alpaca_paper_state.json.lock")
    with contextlib.ExitStack() as stack:
        for path in sorted(locks):
            stack.enter_context(hold_lock(path))
        paths += runtime_files()
        manifest = {"created_at_utc": stamp, "contains_credentials": False, "files": {}}
        with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path in paths:
                relative = path.relative_to(ROOT).as_posix()
                data = path.read_bytes()
                if relative.startswith("deploy/linux/"):
                    data = data.replace(b"\r\n", b"\n")
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mtime = int(path.stat().st_mtime)
                info.mode = 0o600 if relative.startswith("runtime/") else 0o644
                archive.addfile(info, io.BytesIO(data))
                manifest["files"][relative] = hashlib.sha256(data).hexdigest()
            data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo("CLOUD_MANIFEST.json")
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    print(json.dumps({"archive": str(destination), "file_count": len(paths),
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                      "contains_credentials": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
