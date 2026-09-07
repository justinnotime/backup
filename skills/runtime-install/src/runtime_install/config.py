"""Read native installation settings without executing configuration code."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path


class ConfigError(ValueError):
    pass


def resolve(value, *, config_directory=None, environment=None):
    """Expand explicit environment references and optional environment defaults.

    Only braced references are expanded; shell expressions are never evaluated.
    CONFIG_DIR is the resolved source file directory, including through links.
    """
    env = dict(os.environ if environment is None else environment)
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("HOSTNAME", socket.gethostname().split(".")[0])
    for name, suffix in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        env[name] = env.get(name) or str(Path(env["HOME"]) / suffix)
    if config_directory is not None:
        env["CONFIG_DIR"] = str(config_directory)

    def expand(item):
        if isinstance(item, str):
            if item == "~" or item.startswith("~/"):
                item = env["HOME"] + item[1:]

            def replace(match):
                if not env.get(match[1]):
                    raise ConfigError("required configuration environment is missing")
                return env[match[1]]

            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, item)
        if isinstance(item, list):
            return [expand(child) for child in item]
        if isinstance(item, dict):
            if "env" in item and set(item) <= {"env", "default", "suffix"}:
                name = item["env"]
                if not isinstance(name, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", name
                ):
                    raise ConfigError("invalid configuration environment name")
                selected = env.get(name) or expand(item.get("default"))
                if not isinstance(selected, str):
                    raise ConfigError("required configuration environment is missing")
                if "suffix" in item:
                    suffix = expand(item["suffix"])
                    if not isinstance(suffix, str) or not suffix.startswith("/"):
                        raise ConfigError(
                            "configuration path suffix must begin with slash"
                        )
                    selected = os.path.normpath(selected + suffix)
                return expand(selected)
            return {key: expand(child) for key, child in item.items()}
        return item

    return expand(value)


def load_config(filename):
    if filename == "-":
        return resolve(json.load(sys.stdin))
    selected = Path(filename).expanduser().resolve()
    return resolve(json.loads(selected.read_text()), config_directory=selected.parent)
