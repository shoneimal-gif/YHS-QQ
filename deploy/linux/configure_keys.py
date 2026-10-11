#!/usr/bin/env python3
"""Interactively save API keys without placing them in argv or shell history."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings


DIRECTORY = Path("/etc/yhs-qq")
DESTINATION = DIRECTORY / "credentials.json"
KEY_NAMES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "YHS_API_KEY")


def configure() -> int:
    if os.geteuid() != 0:
        print("Run this command with sudo on the server", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print("An interactive terminal is required; connect using ssh -t", file=sys.stderr)
        return 1
    if not DIRECTORY.is_dir() or DIRECTORY.is_symlink() or DIRECTORY.resolve() != DIRECTORY:
        print("Install the service first; /etc/yhs-qq must be a real directory", file=sys.stderr)
        return 1
    directory_stat = DIRECTORY.stat()
    if directory_stat.st_uid != 0:
        print("/etc/yhs-qq must be owned by root", file=sys.stderr)
        return 1
    if DESTINATION.is_symlink() or (DESTINATION.exists() and not stat.S_ISREG(DESTINATION.stat().st_mode)):
        print("Refusing to replace a credential path that is not a regular file", file=sys.stderr)
        return 1
    os.chmod(DIRECTORY, 0o700)
    values = {}
    warnings.simplefilter("error", getpass.GetPassWarning)
    for name in KEY_NAMES:
        value = getpass.getpass(f"{name} (hidden): ").strip()
        if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
            print("All three keys must be nonempty and contain no control characters; no changes saved", file=sys.stderr)
            return 1
        values[name] = value
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DIRECTORY,
                                         prefix=".credentials-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(values, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, DESTINATION)
        temporary = None
        directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Saved /etc/yhs-qq/credentials.json with root-only permissions (0600)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(configure())
    except (OSError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Credential configuration did not complete", file=sys.stderr)
        raise SystemExit(1)
