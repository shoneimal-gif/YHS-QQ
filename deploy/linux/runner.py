#!/usr/bin/env python3
"""Run the existing Paper program with systemd-managed API credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


KEY_NAMES = frozenset({"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "YHS_API_KEY"})


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate credential field")
        result[name] = value
    return result


def read_credentials() -> dict[str, str]:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise ValueError("Start through systemd with LoadCredential=api configured")
    credential_file = Path(directory) / "api"
    with credential_file.open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("Credential file exceeds the supported size")
    values = json.loads(payload, object_pairs_hook=unique_object)
    if not isinstance(values, dict) or set(values) != KEY_NAMES:
        raise ValueError("Credential file must contain exactly the three required API fields")
    result = {}
    for name in KEY_NAMES:
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("An API credential is missing")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("An API credential contains an invalid control character")
        result[name] = value.strip()
    return result


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
    app_directory = Path(__file__).resolve().parents[2]
    os.chdir(app_directory)
    sys.path.insert(0, str(app_directory))
    os.environ.update(credentials)
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
        for name in KEY_NAMES:
            os.environ.pop(name, None)


if __name__ == "__main__":
    raise SystemExit(run())
