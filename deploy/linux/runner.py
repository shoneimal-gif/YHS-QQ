#!/usr/bin/env python3
"""Run the existing Paper program with systemd-managed API credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


KEY_NAMES = frozenset({"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "YHS_API_KEY"})
OPTIONAL_KEY_NAMES = frozenset({"YHS_DISCORD_WEBHOOK"})
NOTIFICATION_KEY_NAMES = frozenset({"YHS_DISCORD_WEBHOOK", "YHS_WECOM_WEBHOOK", "YHS_PUSHPLUS_TOKEN"})
APP_DIRECTORY = Path(__file__).resolve().parents[2]


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate credential field")
        result[name] = value
    return result


def read_credentials_file(credential_file: Path) -> dict[str, str]:
    """Read the bounded regular credential file without logging its contents."""
    descriptor = os.open(credential_file, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Credentials must be a regular file")
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("Credential file exceeds the supported size")
    values = json.loads(payload, object_pairs_hook=unique_object)
    if (not isinstance(values, dict) or not KEY_NAMES.issubset(values)
            or set(values) - KEY_NAMES - OPTIONAL_KEY_NAMES):
        raise ValueError("Credential file must contain the three API fields and only supported optional fields")
    result = {}
    for name in values:
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("An API credential is missing")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("An API credential contains an invalid control character")
        result[name] = value.strip()
    if "YHS_DISCORD_WEBHOOK" in result:
        # The official Discord validator performs no network requests.
        sys.path.insert(0, str(APP_DIRECTORY))
        try:
            from discord_notifications import validate_webhook
            result["YHS_DISCORD_WEBHOOK"] = validate_webhook(result["YHS_DISCORD_WEBHOOK"])
        finally:
            sys.path.pop(0)
    return result


def read_credentials() -> dict[str, str]:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise ValueError("Start through systemd with LoadCredential=api configured")
    return read_credentials_file(Path(directory) / "api")


def run() -> int:
    parser = argparse.ArgumentParser(description="YHS QQ cloud runner, Alpaca Paper only")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Read-only Alpaca account check")
    modes.add_argument("--preview", action="store_true", help="Monitor signals without writing orders")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required", file=sys.stderr)
        return 1
    try:
        credentials = read_credentials()
    except (OSError, ValueError, TypeError):
        # Never include JSON decoder text or secret values in journal output.
        print("API credential loading failed; configure all three keys and use systemd LoadCredential", file=sys.stderr)
        return 1
    app_directory = APP_DIRECTORY
    os.chdir(app_directory)
    sys.path.insert(0, str(app_directory))
    # Cloud deployment supplies Discord explicitly. Inherited legacy channel
    # credentials are never enabled by this runner, nor passed to read-only modes.
    managed_names = KEY_NAMES | NOTIFICATION_KEY_NAMES
    previous = {name: os.environ.get(name) for name in managed_names}
    for name in managed_names:
        os.environ.pop(name, None)
    os.environ.update({name: value for name, value in credentials.items()
                       if name in KEY_NAMES or not (args.check or args.preview)})
    try:
        from main import main

        arguments = ["--config", "config.alpaca_paper_100k.json"]
        if args.check:
            arguments.append("--alpaca-check")
        else:
            arguments += ["--alpaca-paper", "--alpaca-policy", "alpaca_policy_100k.json"]
            if not args.preview:
                arguments.append("--execute-paper")
        return main(arguments)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(run())
