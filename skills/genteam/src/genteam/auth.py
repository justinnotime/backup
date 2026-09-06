"""Validate and store a cookie without putting its value in arguments or output."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

from .client import APIError, Client, fingerprint
from .config import ConfigurationError, Settings


def validate(client: Client, value: str) -> tuple[bool, str]:
    try:
        response = client.request("GET", "/servers", cookie=value)
    except APIError as exc:
        return False, str(exc)
    servers = response.get("servers")
    if servers is None:
        payload = response.get("data")
        if isinstance(payload, list):
            servers = payload
        elif isinstance(payload, dict):
            servers = next((item for item in payload.values() if isinstance(item, list)), None)
    if not isinstance(servers, list):
        return False, "API returned no workspace list; cookie was not verified"
    return True, f"cookie valid; {len(servers)} workspace(s) visible"


def store(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="validate the existing cookie")
    actions.add_argument("--stdin", action="store_true", help="read a cookie from stdin")
    args = parser.parse_args(argv)
    settings = Settings(args.config)
    client = Client(settings)
    if args.check:
        value = client.cookie()
    elif args.stdin:
        value = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            raise ConfigurationError(
                "hidden prompting requires a terminal; use --stdin for a password manager"
            )
        print("Copy the configured site's session cookie from your browser's developer tools.")
        value = getpass.getpass(f"{settings.cookie_name}: ").strip()
    value = value.removeprefix(settings.cookie_name + "=").strip().strip('"')
    if not value or any(character in value for character in "\r\n;"):
        raise ConfigurationError("empty or invalid cookie input")
    ok, message = validate(client, value)
    if not ok:
        print(f"FAIL {message}; nothing stored")
        return 1
    if not args.check:
        store(settings.cookie_file, value)
    print(f"OK {message} (fingerprint {fingerprint(value)})")
    return 0


def main(argv=None) -> int:
    try:
        return run(argv)
    except (APIError, ConfigurationError, OSError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
