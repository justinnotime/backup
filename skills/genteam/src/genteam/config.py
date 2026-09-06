"""Explicit private settings shared by all GenTeam commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value)).replace("{home}", str(Path.home()))


class Settings:
    def __init__(self, source: str | Path | None = None):
        selected = source or os.environ.get("GENTEAM_CONFIG")
        self.source = (
            Path(expand(str(selected)))
            if selected
            else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "genteam/config.json"
        )
        self.source = self.source.resolve()
        try:
            self.data = json.loads(self.source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ConfigurationError("cannot read GenTeam configuration") from None
        if not isinstance(self.data, dict) or self.data.get("schema") != "genteam/v1":
            raise ConfigurationError("configuration schema must be genteam/v1")
        self.base_url = str(self.data.get("base_url", "")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ConfigurationError(
                "base_url must be an explicit HTTP(S) origin without credentials"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigurationError("remote GenTeam credentials require HTTPS")
        self.cookie_file = self.path("cookie_file", required=True)
        self.cookie_name = self.get("cookie_name", "session_id")
        if not isinstance(self.cookie_name, str) or not self.cookie_name.replace("_", "").isalnum():
            raise ConfigurationError("cookie_name must be a cookie field name")

    def get(self, dotted: str, default=None):
        value = self.data
        for key in dotted.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def path(self, dotted: str, default=None, *, required=False) -> Path | None:
        value = self.get(dotted, default)
        if value is None:
            if required:
                raise ConfigurationError(f"missing configuration field {dotted}")
            return None
        if not isinstance(value, (str, Path)) or not str(value):
            raise ConfigurationError(f"{dotted} must be a path")
        result = Path(expand(str(value)))
        return result if result.is_absolute() else self.source.parent / result

    def command(self, dotted: str) -> list[str]:
        value = self.get(dotted, [])
        if not isinstance(value, list) or any(not isinstance(arg, str) or not arg for arg in value):
            raise ConfigurationError(f"{dotted} must be an argument array")
        return [expand(arg) for arg in value]
