"""Caller-owned configuration for the standalone fleet runtime.

No configuration is required for an isolated local installation. An explicitly
selected file must exist and be valid; an absent default file means defaults.
Credentials are referenced by path and are never included in diagnostics.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping


def home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return Path(values.get("HOME", str(Path.home())))


def expand(value: str, env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    if value == "~" or value.startswith("~/"):
        value = str(home(values)) + value[1:]
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                  lambda match: values.get(match[1] or match[2], match[0]), value)


def config_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    selected = values.get("FLEET_ORCHESTRATOR_CONFIG")
    if selected:
        return Path(expand(selected, values))
    root = Path(values.get("XDG_CONFIG_HOME", str(home(values) / ".config")))
    return root / "fleet-orchestrator" / "config.json"


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("fleet configuration contains duplicate fields")
        result[key] = value
    return result


def read(env: Mapping[str, str] | None = None) -> dict:
    values = os.environ if env is None else env
    selected = config_path(values)
    try:
        raw = selected.read_text(encoding="utf-8")
    except FileNotFoundError:
        if values.get("FLEET_ORCHESTRATOR_CONFIG"):
            raise ValueError("explicit fleet configuration file is missing") from None
        return {}
    except (OSError, UnicodeError):
        raise ValueError("fleet configuration file cannot be read") from None
    try:
        result = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise ValueError("fleet configuration is invalid JSON") from None
    if not isinstance(result, dict):
        raise ValueError("fleet configuration must be a JSON object")
    if result.get("schema", "fleet-runtime/v1") != "fleet-runtime/v1":
        raise ValueError("unsupported fleet configuration schema")
    return result


def get(key: str, default=None, *, env: Mapping[str, str] | None = None):
    value = read(env)
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def path(key: str, default=None, *, env: Mapping[str, str] | None = None) -> Path | None:
    value = get(key, default, env=env)
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"fleet configuration field {key} must be a path")
    return Path(expand(str(value), env))


def command(key: str, *, env: Mapping[str, str] | None = None) -> list[str]:
    value = get(key, [], env=env)
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"fleet configuration field {key} must be an argument array")
    return [expand(x, env) for x in value]
