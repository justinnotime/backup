"""Caller-owned locations and private collection settings."""

import json
import os
import re
import tempfile
from pathlib import Path


def atomic_write(path, text, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    path = Path(os.path.expandvars(str(path))).expanduser().resolve()
    value = json.loads(path.read_text())
    allowed = {
        "schema",
        "state_directory",
        "lock_file",
        "read_token_file",
        "client_id",
        "authority",
        "login_hint",
        "own_user_id",
        "collection_enabled",
        "sender_hourly_limit",
        "list_page_limit",
        "message_page_limit",
        "first_run_lookback_minutes",
        "overlap_minutes",
        "draft_expiry_hours",
    }
    if (
        not isinstance(value, dict)
        or value.get("schema") != "chat-mentions/v1"
        or set(value) - allowed
    ):
        raise ValueError("config-schema-invalid")

    def location(text):
        if not isinstance(text, str) or not text:
            raise ValueError("config-path-required")
        text = os.path.expandvars(text)
        if re.search(r"\$(?:\w+|\{[^}]+\})", text):
            raise ValueError("config-path-variable-unresolved")
        selected = Path(text).expanduser()
        return (
            (path.parent / selected) if not selected.is_absolute() else selected
        ).resolve()

    value["state_directory"] = location(value.get("state_directory"))
    value["lock_file"] = location(
        value.get("lock_file", str(value["state_directory"] / "collector.lock"))
    )
    if value.get("read_token_file"):
        value["read_token_file"] = location(value["read_token_file"])
    enabled = value.setdefault("collection_enabled", False)
    if type(enabled) is not bool:
        raise ValueError("config-collection-enabled-invalid")
    for name in ("client_id", "authority", "login_hint", "own_user_id"):
        if name in value and (
            not isinstance(value[name], str) or not value[name].strip()
        ):
            raise ValueError("config-auth-field-invalid: " + name)
    if enabled and (not value.get("client_id") or not value.get("read_token_file")):
        raise ValueError("config-collection-credentials-required")
    for key, default in [
        ("sender_hourly_limit", 4),
        ("list_page_limit", 10),
        ("message_page_limit", 10),
        ("first_run_lookback_minutes", 30),
        ("overlap_minutes", 10),
        ("draft_expiry_hours", 48),
    ]:
        number = value.setdefault(key, default)
        if type(number) is not int or number <= 0:
            raise ValueError("config-positive-number-required: " + key)
    artifacts = [
        path,
        value["lock_file"],
        value["state_directory"] / "state.json",
        value["state_directory"] / "queue.jsonl",
    ]
    if value.get("read_token_file"):
        artifacts.append(value["read_token_file"])
    if len(set(artifacts)) != len(artifacts):
        raise ValueError("config-files-must-be-distinct")
    return value
