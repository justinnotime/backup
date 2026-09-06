"""Install only an explicitly selected startup hook in a JSON settings file."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .brief import expand, load_config, path_at


def update(settings: dict, command: str, previous: list[str], timeout: int, action: str) -> dict:
    result = copy.deepcopy(settings)
    hooks = result.setdefault("hooks", {})
    existing = hooks.get("SessionStart", [])
    if not isinstance(existing, list):
        raise TypeError("invalid SessionStart configuration")
    selected = {command, *previous}
    entries = []
    installed = False
    for entry in existing:
        if not any(hook.get("command") in selected for hook in entry.get("hooks", [])):
            entries.append(entry)
            continue
        retained = []
        for hook in entry.get("hooks", []):
            if hook.get("command") not in selected:
                retained.append(hook)
            elif action == "install" and not installed:
                retained.append({**hook, "type": "command", "command": command, "timeout": timeout})
                installed = True
        if retained:
            entries.append({**entry, "hooks": retained})
    if action == "install" and not installed:
        entries.append(
            {"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        )
    if entries:
        hooks["SessionStart"] = entries
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        result.pop("hooks", None)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "action", choices=("install", "uninstall", "check"), nargs="?", default="install"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        hook = config["hook"]
        root = config["repository_root"]
        path = path_at(hook["settings_path"], root)
        if path.is_symlink() or not path.is_file():
            raise ValueError("settings must be an existing regular file")
        before = path.read_bytes()
        settings = json.loads(before)
        command_argv = hook["argv"]
        if (
            not isinstance(command_argv, list)
            or not command_argv
            or not all(isinstance(x, str) for x in command_argv)
        ):
            raise ValueError("hook argv must be explicit")
        command = shlex.join(expand(arg, root) for arg in command_argv)
        previous = hook.get("previous_commands", [])
        if not isinstance(previous, list) or not all(isinstance(x, str) for x in previous):
            raise ValueError("previous commands must be literal strings")
        previous = [value.replace("@root@", str(root)) for value in previous]
        if args.action == "check":
            found = any(
                h.get("command") in {command, *previous}
                for entry in settings.get("hooks", {}).get("SessionStart", [])
                for h in entry.get("hooks", [])
            )
            print("STATE: installed" if found else "STATE: not-installed")
            return 0
        updated = update(
            settings, command, previous, int(hook.get("timeout_seconds", 5)), args.action
        )
        if updated == settings:
            print("STATE: unchanged")
            return 0
        backup = path.with_name(
            path.name
            + ".workspace-brief-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + ".bak"
        )
        with backup.open("xb") as handle:
            os.chmod(backup, 0o600)
            handle.write(before)
        descriptor, temporary = tempfile.mkstemp(prefix=".workspace-brief-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(updated, handle, indent=2)
                handle.write("\n")
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            if path.is_symlink() or path.read_bytes() != before:
                raise ValueError("settings changed during installation")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(f"STATE: {args.action} complete; backup: {backup}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "FAIL workspace-brief: settings or selected hook configuration invalid", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
