"""Explicit repository layout and issue workflow settings."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


class ConfigurationError(ValueError):
    pass


def local_path(root: Path, value: str, *, glob: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ConfigurationError("invalid local path")
    path = Path(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
        raise ConfigurationError("local paths must remain inside the configured repository")
    if not glob and any(character in value for character in "*?["):
        raise ConfigurationError("wildcards are not allowed here")
    current = root.resolve()
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ConfigurationError("symlink paths are not supported")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ConfigurationError("local path escapes repository")
    return current


def load(path: str | Path, root: str | Path | None = None) -> dict:
    cfg = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("schema") != "markdown-issues/v1":
        raise ConfigurationError("unsupported configuration schema")
    repository = Path(root or os.path.expandvars(cfg["repository_root"])).expanduser().resolve()
    if not repository.is_dir():
        raise ConfigurationError("repository directory is missing")
    cfg["repository_root"] = str(repository)
    for field in ("open_directory", "closed_directory"):
        local_path(repository, cfg[field])
    if cfg["open_directory"] == cfg["closed_directory"]:
        raise ConfigurationError("open and closed directories must differ")
    for field in ("actors", "priorities", "kinds", "sub_states"):
        values = cfg[field]
        if (
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
                for value in values
            )
        ):
            raise ConfigurationError("invalid workflow vocabulary")
    for field in ("default_actor", "default_assignee", "unassigned_actor"):
        if cfg[field] not in cfg["actors"]:
            raise ConfigurationError("default actor is not in the configured vocabulary")
    if any(not re.fullmatch(r"[a-z0-9-]+", actor) for actor in cfg["actors"]):
        raise ConfigurationError("actors must match the note author format")
    if cfg["default_actor"] == cfg["unassigned_actor"]:
        raise ConfigurationError("opening actor must be attributable")
    if not re.fullmatch(r"[a-z0-9-]+", cfg["default_actor"]):
        raise ConfigurationError("invalid note actor")
    for field in ("stale_days", "idle_days"):
        if isinstance(cfg[field], bool) or not isinstance(cfg[field], int) or cfg[field] < 0:
            raise ConfigurationError("invalid age threshold")
    for value in cfg["related_path_prefixes"]:
        local_path(repository, value.rstrip("/"))
    headings = cfg["headings"]
    if set(headings) != {"context", "acceptance", "notes"} or len(set(headings.values())) != 3:
        raise ConfigurationError("three distinct section headings are required")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"## [^\r\n]+", value)
        for value in headings.values()
    ):
        raise ConfigurationError("invalid section heading")
    for ref in cfg.get("base_refs", ["origin/main", "HEAD"]):
        validate_ref(ref)
    if "body_template" in cfg and not isinstance(cfg["body_template"], str):
        raise ConfigurationError("invalid creation template")
    return cfg


def validate_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or any(character in value for character in "\0\r\n")
    ):
        raise ConfigurationError("invalid Git reference")
    return value
