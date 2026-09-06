"""Explicit caller settings, command execution and local atomic writes."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArchiveError(Exception):
    """A diagnosed failure whose message contains no upstream response body."""


@dataclass(frozen=True)
class Settings:
    root: Path
    output_directory: Path
    state_file: Path | None
    account: str | None
    command: tuple[str, ...]
    timeout: float
    rate_delay: float
    options: dict[str, Any]


def _path(value: Any, relative_to: Path | None = None) -> Path:
    if not isinstance(value, (str, Path)) or not str(value) or "\0" in str(value):
        raise ArchiveError("invalid configured path")
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if not path.is_absolute() and relative_to is not None:
        path = relative_to / path
    return path.resolve()


def _number(value: Any, name: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArchiveError(f"invalid {name}")
    if not math.isfinite(value) or value < minimum:
        raise ArchiveError(f"invalid {name}")
    return float(value)


def load_config(
    path: str | Path,
    kind: str,
    root: str | Path | None = None,
    state_file: str | Path | None = None,
) -> Settings:
    config_path = _path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError("configuration cannot be read") from exc
    if not isinstance(data, dict) or data.get("schema") != "genspark-archive/v1":
        raise ArchiveError("unsupported configuration schema")
    if kind not in {"emails", "calendar", "meetings"}:
        raise ArchiveError("unsupported archive kind")
    section = data.get(kind)
    if not isinstance(section, dict):
        raise ArchiveError("requested archive is not configured")
    repository = _path(root if root is not None else data.get("repository_root"))
    if not repository.is_dir():
        raise ArchiveError("repository root is not an existing directory")
    output = _path(section.get("output_directory"), repository)
    if not output.is_relative_to(repository) or output == repository:
        raise ArchiveError("archive output must be below the repository root")
    selected_state = state_file if state_file is not None else section.get("state_file")
    state = _path(selected_state, config_path.parent) if selected_state is not None else None
    if kind != "calendar" and state is None:
        raise ArchiveError("incremental archives require an external state file")
    if state is not None and (state.is_relative_to(repository) or state == config_path):
        raise ArchiveError("state must be outside the repository and separate from configuration")
    account = section.get("account")
    if kind in {"emails", "calendar"} and (
        not isinstance(account, str) or not account.strip() or "\0" in account
    ):
        raise ArchiveError("requested archive requires an explicit account")
    command = data.get("command", ["gsk"])
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item.strip() or "\0" in item for item in command)
    ):
        raise ArchiveError("command must be a nonempty argument array")
    expanded_command = tuple(os.path.expandvars(os.path.expanduser(item)) for item in command)
    options = {
        key: value
        for key, value in section.items()
        if key not in {"output_directory", "state_file", "account"}
    }
    for key in (
        "list_limit",
        "page_size",
        "days_back",
        "days_forward",
        "give_up_days",
        "lookback_days",
    ):
        if key in options:
            minimum = 1 if key in {"list_limit", "page_size"} else 0
            if (
                isinstance(options[key], bool)
                or not isinstance(options[key], int)
                or options[key] < minimum
            ):
                raise ArchiveError(f"invalid {key}")
    if options.get("page_size", 50) > (50 if kind == "meetings" else 100):
        raise ArchiveError("page_size exceeds the service maximum")
    if "folders" in options and (
        not isinstance(options["folders"], list)
        or not options["folders"]
        or any(not isinstance(folder, str) or not folder.strip() for folder in options["folders"])
    ):
        raise ArchiveError("folders must be a nonempty list of names")
    return Settings(
        root=repository,
        output_directory=output,
        state_file=state,
        account=account,
        command=expanded_command,
        timeout=_number(data.get("timeout", 120), "timeout", 0.001),
        rate_delay=_number(data.get("rate_delay", 1), "rate_delay", 0),
        options=options,
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    default_config = os.environ.get("GENSPARK_ARCHIVE_CONFIG")
    parser.add_argument("--config", default=default_config, required=not bool(default_config))
    parser.add_argument("--root", help="transaction repository for archive output")
    parser.add_argument("--state-file", help="external staged progress file")
    parser.add_argument(
        "--doctor", action="store_true", help="check local configuration without service calls"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show selected operation without service calls or writes",
    )


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings

    def call(self, argv: list[str], timeout: float | None = None) -> dict:
        try:
            result = subprocess.run(
                [*self.settings.command, *argv],
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.settings.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise ArchiveError("archive service command could not complete") from exc
        if result.returncode != 0:
            raise ArchiveError("archive service command returned failure")
        try:
            start = result.stdout.index("{")
            data = json.loads(result.stdout[start:])
        except (ValueError, TypeError) as exc:
            raise ArchiveError("archive service returned invalid JSON") from exc
        if (
            not isinstance(data, dict)
            or data.get("error")
            or data.get("success") is False
            or (
                isinstance(data.get("status"), str)
                and data["status"].lower() in {"error", "failed"}
            )
        ):
            raise ArchiveError("archive service reported failure")
        return data

    def pause(self) -> None:
        if self.settings.rate_delay:
            time.sleep(self.settings.rate_delay)


def output_path(settings: Settings, relative: str | Path) -> Path:
    path = settings.output_directory / relative
    target = path.resolve()
    if not target.is_relative_to(settings.output_directory) or target == settings.output_directory:
        raise ArchiveError("archive output escapes its selected directory")
    if path.is_symlink():
        raise ArchiveError("archive output is a symbolic link")
    return target


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ArchiveError("refusing to replace a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_state(path: Path) -> dict:
    try:
        if not path.exists():
            return {"synced_ids": []}
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError("archive state cannot be read") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("synced_ids"), list)
        or any(not isinstance(value, str) or not value for value in state["synced_ids"])
    ):
        raise ArchiveError("archive state has an invalid shape")
    return state


def write_state(path: Path, state: dict) -> None:
    updated = dict(state)
    updated["synced_ids"] = sorted(set(updated["synced_ids"]))
    write_text(path, json.dumps(updated, indent=2, sort_keys=True))
