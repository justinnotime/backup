"""Caller-configured fleet runtime paths."""
from __future__ import annotations

import os
from pathlib import Path
import runtime_config as cfg


def runtime_root() -> Path:
    # Named fleet profiles set this compatibility variable before imports.
    if os.environ.get("NOTES_RUNTIME_DIR"):
        return Path(os.environ["NOTES_RUNTIME_DIR"]).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return cfg.path("runtime_dir", state / "fleet-orchestrator")


def repository_root() -> Path:
    return cfg.path("canonical_source_root", Path(__file__).resolve().parents[2])


def orchestrator_state_dir() -> Path:
    if os.environ.get("NW_FLEET_PROFILE_APPLIED"):
        return runtime_root() / "state/fleet-orchestrator"
    return cfg.path("paths.orchestrator_state", runtime_root() / "state/fleet-orchestrator")


def codex_drive_state_dir() -> Path:
    return cfg.path("paths.legacy_drive_state", runtime_root() / "state/legacy-drive")


def lock_path(name: str) -> Path:
    if os.environ.get("NW_FLEET_PROFILE_APPLIED"):
        return runtime_root() / "cache/locks" / (name + ".lock")
    prefix = cfg.get("paths.lock_prefix", "")
    if not isinstance(prefix, str) or "/" in prefix or "\\" in prefix:
        raise ValueError("fleet configuration lock prefix must be a filename prefix")
    return cfg.path("paths.lock_directory", runtime_root() / "locks") / (prefix + name + ".lock")


def log_dir() -> Path:
    return runtime_root() / "logs"
