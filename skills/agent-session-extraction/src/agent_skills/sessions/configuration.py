"""Explicit environment references in caller-owned configuration values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path


_VARIABLE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand_environment(value: str, environ: Mapping[str, str]) -> str:
    """Expand one value once, without shell evaluation or implicit defaults."""
    def replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name = match.group(1) or match.group(2)
        if name not in environ or not environ[name]:
            raise ValueError("configuration environment reference is missing")
        return environ[name]

    expanded = _VARIABLE.sub(replace, value)
    if expanded == "~" or expanded.startswith("~/"):
        home = environ.get("HOME", "")
        if not Path(home).is_absolute():
            raise ValueError("configuration HOME must be an absolute path")
        expanded = home + expanded[1:]
    return expanded


def require_external_config(path: Path, repository: Path) -> None:
    """Honor a caller's choice to keep a concrete configuration outside Git."""
    if path.is_symlink() or path.resolve().is_relative_to(repository.resolve()):
        raise ValueError("configuration must be an external non-symlink file")
