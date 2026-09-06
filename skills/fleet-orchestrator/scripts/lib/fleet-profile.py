#!/usr/bin/env python3
"""Create and resolve one fully isolated named fleet.

The default fleet intentionally has no profile: callers that do not request a
named fleet keep their existing environment and behavior.  A named profile is
an all-or-nothing boundary around tmux, ORC state, and Agent Bus state. Matrix
profiles retain their two-room boundary. Local profiles delete that dependency
and are valid only on the host that created them. Physical separation avoids a
fleet column in every database table and a fleet filter in every query.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_config as cfg

SCHEMA = 1
LOCAL_SCHEMA = 2
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TMUX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ROOM_RE = re.compile(r"^![^\s:]+:[^\s:]+$")
MATRIX_EXPECTED_FIELDS = {
    "schema",
    "name",
    "tmux_server",
    "primary_session",
    "matrix_homeserver",
    "matrix_room",
    "matrix_registry_room",
}
LOCAL_EXPECTED_FIELDS = {
    "schema",
    "name",
    "tmux_server",
    "primary_session",
    "agent_bus_transport",
    "local_host",
}
# Kept as a compatibility name for callers that imported the original schema.
EXPECTED_FIELDS = MATRIX_EXPECTED_FIELDS

PROFILE_ENV_KEYS = (
    "NW_FLEET",
    "NW_FLEET_PROFILE_APPLIED",
    "NW_FLEET_PROFILE_PATH",
    "NW_FLEET_PRIMARY_SESSION",
    "NW_TMUX_SERVER",
    "NOTES_RUNTIME_DIR",
    "DISPATCH_LEDGER_DB",
    "AGENT_BUS_TRANSPORT",
    "AGENT_BUS_CFG",
    "AGENT_BUS_DB",
    "MATRIX_BUS_CFG",
    "MATRIX_BUS_HS",
    "MATRIX_BUS_ROOM",
    "MATRIX_BUS_REGISTRY_ROOM",
)

class FleetProfileError(ValueError):
    """A profile is missing, partial, unsafe, or not isolated."""


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def profile_dir(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_PROFILE_DIR", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.profile_directory",
                          Path(env.get("XDG_CONFIG_HOME", str(_home(env) / ".config")))
                          / "fleet-orchestrator/fleets", env=env))


def runtime_root(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_RUNTIME_ROOT", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.runtime_directory",
                          Path(env.get("XDG_STATE_HOME", str(_home(env) / ".local/state")))
                          / "fleet-orchestrator/fleets", env=env))


def matrix_config_root(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_MATRIX_CFG_ROOT", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.matrix_config_directory",
                          Path(env.get("XDG_CONFIG_HOME", str(_home(env) / ".config")))
                          / "fleet-orchestrator/matrix-fleets", env=env))


def local_hostname() -> str:
    """Return the same short host identity used by Agent Bus onboarding."""
    return socket.gethostname().split(".", 1)[0]


def validate_name(name: str) -> str:
    if name == "default":
        return name
    if not NAME_RE.fullmatch(name):
        raise FleetProfileError(
            "fleet name must match [a-z0-9][a-z0-9-]{0,31}"
        )
    return name


def profile_path(name: str, env: Mapping[str, str] = os.environ) -> Path:
    validate_name(name)
    if name == "default":
        raise FleetProfileError("the default fleet deliberately has no profile")
    return profile_dir(env) / f"{name}.json"


def _read_json(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink():
            raise FleetProfileError(f"profile must not be a symlink: {path}")
        if not stat.S_ISREG(path.stat().st_mode):
            raise FleetProfileError(f"profile must be a regular file: {path}")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FleetProfileError(f"fleet profile does not exist: {path}") from exc
    except OSError as exc:
        raise FleetProfileError(f"cannot read fleet profile {path}: {exc}") from exc
    try:
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise FleetProfileError(f"duplicate field {key!r} in {path}")
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise FleetProfileError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetProfileError(f"fleet profile must be one JSON object: {path}")
    return value


def _nonempty_string(profile: Mapping[str, object], field: str, path: Path) -> str:
    value = profile.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FleetProfileError(f"{field} must be a non-empty string in {path}")
    if value != value.strip():
        raise FleetProfileError(f"{field} must not have surrounding whitespace in {path}")
    return value


def _validate_profile(
    name: str,
    value: dict[str, object],
    path: Path,
    *,
    require_local_host: bool = True,
) -> dict[str, object]:
    schema = value.get("schema")
    if type(schema) is not int or schema not in {SCHEMA, LOCAL_SCHEMA}:
        raise FleetProfileError(f"unsupported schema in {path}: {schema!r}")
    expected_fields = (
        MATRIX_EXPECTED_FIELDS if schema == SCHEMA else LOCAL_EXPECTED_FIELDS
    )
    fields = set(value)
    if fields != expected_fields:
        missing = sorted(expected_fields - fields)
        unknown = sorted(fields - expected_fields)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        raise FleetProfileError(f"profile fields are not exact in {path}: {' '.join(detail)}")
    if value["name"] != name:
        raise FleetProfileError(
            f"profile name {value['name']!r} does not match filename {name!r}"
        )

    tmux_server = _nonempty_string(value, "tmux_server", path)
    primary_session = _nonempty_string(value, "primary_session", path)
    if not TMUX_RE.fullmatch(tmux_server):
        raise FleetProfileError(f"invalid tmux_server in {path}: {tmux_server!r}")
    if not SESSION_RE.fullmatch(primary_session):
        raise FleetProfileError(
            f"invalid primary_session in {path}: {primary_session!r}"
        )
    if schema == LOCAL_SCHEMA:
        transport = _nonempty_string(value, "agent_bus_transport", path)
        local_host = _nonempty_string(value, "local_host", path)
        if transport != "local":
            raise FleetProfileError(
                f"agent_bus_transport must be 'local' in {path}"
            )
        current_host = local_hostname()
        if require_local_host and local_host != current_host:
            raise FleetProfileError(
                f"local fleet {name!r} belongs to host {local_host!r}, "
                f"not {current_host!r}"
            )
        return value

    homeserver = _nonempty_string(value, "matrix_homeserver", path)
    room = _nonempty_string(value, "matrix_room", path)
    registry_room = _nonempty_string(value, "matrix_registry_room", path)
    parsed_homeserver = urllib.parse.urlsplit(homeserver)
    if (
        parsed_homeserver.scheme != "https"
        or not parsed_homeserver.hostname
        or parsed_homeserver.username is not None
        or parsed_homeserver.password is not None
        or parsed_homeserver.path not in {"", "/"}
        or parsed_homeserver.query
        or parsed_homeserver.fragment
        or any(c.isspace() for c in homeserver)
    ):
        raise FleetProfileError(
            f"matrix_homeserver must be one https origin in {path}"
        )
    for field, room_id in (("matrix_room", room),
                           ("matrix_registry_room", registry_room)):
        if not ROOM_RE.fullmatch(room_id):
            raise FleetProfileError(f"invalid {field} in {path}: {room_id!r}")
    if room == registry_room:
        raise FleetProfileError(f"message and registry rooms must differ in {path}")
    return value


def _default_tmux_server(env: Mapping[str, str]) -> str | None:
    override = env.get("NW_DEFAULT_TMUX_SERVER", "").strip()
    if override:
        if not TMUX_RE.fullmatch(override):
            raise FleetProfileError("invalid NW_DEFAULT_TMUX_SERVER")
        return override
    inherited = env.get("NW_TMUX_SERVER", "").strip()
    if inherited and not env.get("NW_FLEET_PROFILE_APPLIED"):
        if not TMUX_RE.fullmatch(inherited):
            raise FleetProfileError("invalid inherited NW_TMUX_SERVER")
        return inherited
    state = cfg.path("paths.orchestrator_state",
                     cfg.path("runtime_dir",
                              Path(env.get("XDG_STATE_HOME", str(_home(env) / ".local/state")))
                              / "fleet-orchestrator", env=env)
                     / "state/fleet-orchestrator", env=env)
    path = cfg.path("tmux.server_file", state / "tmux-server", env=env)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FleetProfileError(f"cannot read default tmux selector {path}: {exc}") from exc
    if not TMUX_RE.fullmatch(value):
        raise FleetProfileError(f"invalid default tmux selector in {path}")
    return value


def _default_matrix_rooms(env: Mapping[str, str]) -> set[str]:
    """Rooms the default fleet can currently reach.

    The default transport configuration remains protected even while a named
    profile replaces the current process's Matrix environment.
    """
    rooms = {value for value in (cfg.get("matrix.room", "", env=env),
                                  cfg.get("matrix.registry_room", "", env=env))
             if value}
    if not env.get("NW_FLEET_PROFILE_APPLIED"):
        for key in ("MATRIX_BUS_ROOM", "MATRIX_BUS_REGISTRY_ROOM"):
            value = env.get(key, "").strip()
            if value:
                rooms.add(value)
    return rooms


def _validate_unique(selected_name: str, selected: Mapping[str, object],
                     env: Mapping[str, str]) -> None:
    selected_server = str(selected["tmux_server"])
    default_server = _default_tmux_server(env)
    if default_server and selected_server == default_server:
        raise FleetProfileError(
            f"fleet {selected_name!r} reuses the default tmux server {default_server!r}"
        )

    selected_rooms = (
        {
            str(selected["matrix_room"]),
            str(selected["matrix_registry_room"]),
        }
        if selected["schema"] == SCHEMA
        else set()
    )
    if selected_rooms and selected_rooms & _default_matrix_rooms(env):
        raise FleetProfileError(
            f"fleet {selected_name!r} reuses a default Matrix room"
        )
    root = profile_dir(env)
    try:
        candidates = sorted(root.glob("*.json"))
    except OSError as exc:
        raise FleetProfileError(f"cannot inspect fleet profile directory {root}: {exc}") from exc
    for other_path in candidates:
        if other_path.name == f"{selected_name}.json":
            continue
        other_name = other_path.stem
        validate_name(other_name)
        # Other profiles still participate in uniqueness checks even when they
        # are host-bound elsewhere. Host ownership applies only when selecting
        # that profile, not while inspecting the directory around it.
        other = _validate_profile(
            other_name,
            _read_json(other_path),
            other_path,
            require_local_host=False,
        )
        if selected_server == other["tmux_server"]:
            raise FleetProfileError(
                f"fleets {selected_name!r} and {other_name!r} reuse tmux server"
            )
        other_rooms = (
            {str(other["matrix_room"]), str(other["matrix_registry_room"])}
            if other["schema"] == SCHEMA
            else set()
        )
        if selected_rooms and selected_rooms & other_rooms:
            raise FleetProfileError(
                f"fleets {selected_name!r} and {other_name!r} reuse a Matrix room"
            )


def _ensure_profile_dir(env: Mapping[str, str]) -> Path:
    root = profile_dir(env)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not stat.S_ISDIR(root.stat().st_mode):
            raise FleetProfileError(
                f"fleet profile directory must be a real directory: {root}"
            )
        root.chmod(0o700)
    except OSError as exc:
        raise FleetProfileError(
            f"cannot prepare fleet profile directory {root}: {exc}"
        ) from exc
    return root


def _publish_new_profile(path: Path, value: Mapping[str, object]) -> None:
    """Publish complete bytes without replacing a concurrent creator."""
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link is an atomic no-replace publication on this filesystem.
        os.link(temp_path, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def create_local_profile(
    name: str,
    *,
    tmux_server: str | None = None,
    primary_session: str | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[Path, bool]:
    """Create one host-bound local profile, or validate the existing profile."""
    validate_name(name)
    if name == "default":
        raise FleetProfileError("the default fleet deliberately has no profile")
    root = _ensure_profile_dir(env)
    path = profile_path(name, env)

    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(root / ".create.lock", lock_flags, 0o600)
    except OSError as exc:
        raise FleetProfileError(f"cannot lock fleet profile directory {root}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.exists() or path.is_symlink():
            existing = _validate_profile(name, _read_json(path), path)
            _validate_unique(name, existing, env)
            if tmux_server is not None and existing["tmux_server"] != tmux_server:
                raise FleetProfileError(
                    f"fleet profile already uses tmux server "
                    f"{existing['tmux_server']!r}: {path}"
                )
            if (primary_session is not None
                    and existing["primary_session"] != primary_session):
                raise FleetProfileError(
                    f"fleet profile already uses primary session "
                    f"{existing['primary_session']!r}: {path}"
                )
            return path, False
        server = f"nw-{name}" if tmux_server is None else tmux_server
        session = name if primary_session is None else primary_session
        desired: dict[str, object] = {
            "schema": LOCAL_SCHEMA,
            "name": name,
            "tmux_server": server,
            "primary_session": session,
            "agent_bus_transport": "local",
            "local_host": local_hostname(),
        }
        _validate_profile(name, desired, path)
        _validate_unique(name, desired, env)
        try:
            _publish_new_profile(path, desired)
        except FileExistsError:
            # A writer that does not use this lock may still race us. Validate
            # its complete publication instead of overwriting it.
            existing = _validate_profile(name, _read_json(path), path)
            _validate_unique(name, existing, env)
            if tmux_server is not None and existing["tmux_server"] != tmux_server:
                raise FleetProfileError(
                    f"fleet profile was concurrently created with tmux server "
                    f"{existing['tmux_server']!r}: {path}"
                )
            if (primary_session is not None
                    and existing["primary_session"] != primary_session):
                raise FleetProfileError(
                    f"fleet profile was concurrently created with primary session "
                    f"{existing['primary_session']!r}: {path}"
                )
            return path, False
        return path, True
    finally:
        os.close(lock_fd)


def resolve(name: str, env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Return the complete environment selection for one named fleet."""
    validate_name(name)
    if name == "default":
        return {}
    path = profile_path(name, env)
    profile = _validate_profile(name, _read_json(path), path)
    _validate_unique(name, profile, env)

    runtime = runtime_root(env) / name
    common = {
        "NW_FLEET": name,
        "NW_FLEET_PROFILE_APPLIED": name,
        "NW_FLEET_PROFILE_PATH": str(path),
        "NW_FLEET_PRIMARY_SESSION": str(profile["primary_session"]),
        "NW_TMUX_SERVER": str(profile["tmux_server"]),
        "NOTES_RUNTIME_DIR": str(runtime),
    }
    if profile["schema"] == LOCAL_SCHEMA:
        agent_bus_cfg = runtime / "state" / "agent-bus"
        common.update({
            "DISPATCH_LEDGER_DB": str(
                runtime / "state" / "fleet-orchestrator" / "dispatch-ledger.sqlite3"
            ),
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": str(agent_bus_cfg),
            "AGENT_BUS_DB": str(agent_bus_cfg / "agent-bus-v3.sqlite3"),
        })
        return common

    matrix_cfg = matrix_config_root(env) / name
    common.update({
        "MATRIX_BUS_CFG": str(matrix_cfg),
        "DISPATCH_LEDGER_DB": str(matrix_cfg / "dispatch-ledger.sqlite3"),
        "AGENT_BUS_TRANSPORT": "matrix",
        "AGENT_BUS_DB": str(matrix_cfg / "agent-bus-v3.sqlite3"),
        "MATRIX_BUS_HS": str(profile["matrix_homeserver"]),
        "MATRIX_BUS_ROOM": str(profile["matrix_room"]),
        "MATRIX_BUS_REGISTRY_ROOM": str(profile["matrix_registry_room"]),
    })
    return common


def command_env(name: str, base: Mapping[str, str] = os.environ) -> dict[str, str]:
    result = dict(base)
    if name == "default":
        # When a named profile produced this environment, remove its complete
        # selection.  In an ordinary legacy environment there is no marker,
        # so explicit manual overrides retain exactly their old meaning.
        if result.get("NW_FLEET_PROFILE_APPLIED"):
            for key in PROFILE_ENV_KEYS:
                result.pop(key, None)
        else:
            for key in ("NW_FLEET", "NW_FLEET_PROFILE_PATH",
                        "NW_FLEET_PRIMARY_SESSION"):
                result.pop(key, None)
            if (result.get("AGENT_BUS_TRANSPORT", "").strip().lower() == "local"
                    or result.get("AGENT_BUS_CFG")):
                for key in ("AGENT_BUS_TRANSPORT", "AGENT_BUS_CFG", "AGENT_BUS_DB"):
                    result.pop(key, None)
        return result
    resolved = resolve(name, base)
    for key in PROFILE_ENV_KEYS:
        result.pop(key, None)
    result.update(resolved)
    return result


def _tmux_context(name: str, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    values = resolve(name, env)
    process_env = dict(os.environ)
    process_env.update(command_env(name, env))
    process_env.pop("TMUX", None)
    tmux_bin = process_env.get("TMUX_BIN", "tmux").strip()
    if not tmux_bin:
        raise FleetProfileError("TMUX_BIN must not be empty")
    return [tmux_bin, "-L", values["NW_TMUX_SERVER"]], process_env


def _run_tmux(base: list[str], args: list[str], process_env: Mapping[str, str]):
    try:
        return subprocess.run(
            [*base, *args],
            env=dict(process_env),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise FleetProfileError(f"could not run tmux: {exc}") from exc


def apply_tmux_environment(name: str, *, dry_run: bool,
                           env: Mapping[str, str] = os.environ) -> None:
    values = resolve(name, env)
    server = values["NW_TMUX_SERVER"]
    base, process_env = _tmux_context(name, env)
    probe = _run_tmux(base, ["list-sessions"], process_env)
    if probe.returncode:
        raise FleetProfileError(
            probe.stderr.strip() or f"tmux server {server!r} is unreachable"
        )
    if dry_run:
        clears = sum(1 for key in PROFILE_ENV_KEYS if key not in values)
        print(
            f"would set {len(values)} variables and clear {clears} variables "
            f"on tmux server {server}"
        )
        return

    # Remove the completion marker first and restore it last. If tmux dies
    # during the update, descendants see NW_FLEET without a matching marker;
    # wrappers re-resolve it and the direct adapter refuses it.
    clear = _run_tmux(
        base, ["set-environment", "-gu", "NW_FLEET_PROFILE_APPLIED"], process_env
    )
    if clear.returncode:
        raise FleetProfileError(
            clear.stderr.strip() or "could not clear tmux fleet completion marker"
        )
    for key in PROFILE_ENV_KEYS:
        if key in values or key == "NW_FLEET_PROFILE_APPLIED":
            continue
        result = _run_tmux(base, ["set-environment", "-gu", key], process_env)
        if result.returncode:
            raise FleetProfileError(
                result.stderr.strip() or f"could not clear tmux environment {key}"
            )
    ordered = ["NW_FLEET"] + [
        key for key in values
        if key not in {"NW_FLEET", "NW_FLEET_PROFILE_APPLIED"}
    ] + ["NW_FLEET_PROFILE_APPLIED"]
    for key in ordered:
        value = values[key]
        result = _run_tmux(
            base, ["set-environment", "-g", key, value], process_env
        )
        if result.returncode:
            raise FleetProfileError(
                result.stderr.strip() or f"could not set tmux environment {key}"
            )
    print(f"set named-fleet environment on tmux server {server}")


def ensure_primary_session(
    name: str,
    *,
    rollback_profile: Path | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[str, str, bool]:
    """Make a named fleet immediately attachable without exposing tmux setup."""
    root = _ensure_profile_dir(env)
    if rollback_profile is not None and rollback_profile != profile_path(name, env):
        raise FleetProfileError("rollback profile does not match the selected fleet")
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(root / ".create.lock", lock_flags, 0o600)
    except OSError as exc:
        raise FleetProfileError(f"cannot lock fleet profile directory {root}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            values = resolve(name, env)
            server = values["NW_TMUX_SERVER"]
            session = values["NW_FLEET_PRIMARY_SESSION"]
            base, process_env = _tmux_context(name, env)
            server_probe = _run_tmux(base, ["list-sessions"], process_env)
            created = False
            if server_probe.returncode == 0:
                owner = _run_tmux(
                    base,
                    ["show-environment", "-g", "NW_FLEET_PROFILE_APPLIED"],
                    process_env,
                )
                marker_key, separator, marker = owner.stdout.strip().partition("=")
                if (
                    owner.returncode != 0
                    or marker_key != "NW_FLEET_PROFILE_APPLIED"
                    or not separator
                    or not marker
                ):
                    raise FleetProfileError(
                        f"tmux server {server!r} already exists without a valid "
                        "fleet owner; refusing to adopt it"
                    )
                if marker != name:
                    raise FleetProfileError(
                        f"tmux server {server!r} belongs to fleet {marker!r}"
                    )
                apply_tmux_environment(name, dry_run=False, env=env)
                session_probe = _run_tmux(
                    base, ["has-session", "-t", f"={session}"], process_env
                )
                if session_probe.returncode:
                    started = _run_tmux(
                        base,
                        ["new-session", "-d", "-s", session, "-n", "main"],
                        process_env,
                    )
                    if started.returncode:
                        raise FleetProfileError(
                            started.stderr.strip()
                            or f"could not create primary tmux session {session!r}"
                        )
                    created = True
            else:
                started = _run_tmux(
                    base,
                    ["new-session", "-d", "-s", session, "-n", "main"],
                    process_env,
                )
                if started.returncode:
                    raise FleetProfileError(
                        started.stderr.strip()
                        or f"could not start tmux server {server!r}"
                    )
                created = True
                apply_tmux_environment(name, dry_run=False, env=env)

            verified = _run_tmux(
                base, ["has-session", "-t", f"={session}"], process_env
            )
            if verified.returncode:
                raise FleetProfileError(
                    verified.stderr.strip()
                    or f"primary tmux session {session!r} is not reachable"
                )
            return server, session, created
        except BaseException as exc:
            if rollback_profile is not None:
                try:
                    rollback_profile.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    raise FleetProfileError(
                        f"{exc}; could not remove incomplete fleet profile "
                        f"{rollback_profile}: {cleanup_exc}"
                    ) from exc
            raise
    finally:
        os.close(lock_fd)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    resolve_p = sub.add_parser("resolve", help="validate and print a profile")
    resolve_p.add_argument("name")
    resolve_p.add_argument("--field", choices=sorted({
        "name", "tmux_server", "primary_session", "profile_path",
        "runtime_dir", "agent_bus_transport", "agent_bus_config_dir",
        "dispatch_ledger_db", "agent_bus_db", "local_host",
        "matrix_config_dir", "matrix_homeserver", "matrix_room",
        "matrix_registry_room",
    }))

    create_p = sub.add_parser(
        "create", help="create a host-bound local fleet and its primary tmux session"
    )
    create_p.add_argument("name")
    create_p.add_argument("--tmux-server", help=argparse.SUPPRESS)
    create_p.add_argument("--primary-session", help=argparse.SUPPRESS)

    exec_p = sub.add_parser("exec", help="run one command in a named fleet")
    exec_p.add_argument("name")
    exec_p.add_argument("command", nargs=argparse.REMAINDER)

    tmux_p = sub.add_parser(
        "apply-tmux", help="make new processes on the fleet tmux server inherit its profile"
    )
    tmux_p.add_argument("name")
    tmux_p.add_argument("--dry-run", action="store_true")
    return p


def public_view(name: str, env: Mapping[str, str] = os.environ) -> dict[str, str]:
    if name == "default":
        return {"name": "default"}
    values = resolve(name, env)
    view = {
        "name": name,
        "tmux_server": values["NW_TMUX_SERVER"],
        "primary_session": values["NW_FLEET_PRIMARY_SESSION"],
        "profile_path": values["NW_FLEET_PROFILE_PATH"],
        "runtime_dir": values["NOTES_RUNTIME_DIR"],
        "agent_bus_transport": values["AGENT_BUS_TRANSPORT"],
        "agent_bus_config_dir": values.get(
            "AGENT_BUS_CFG", values.get("MATRIX_BUS_CFG", "")
        ),
        "dispatch_ledger_db": values["DISPATCH_LEDGER_DB"],
        "agent_bus_db": values["AGENT_BUS_DB"],
    }
    if values["AGENT_BUS_TRANSPORT"] == "local":
        profile = _read_json(Path(values["NW_FLEET_PROFILE_PATH"]))
        view["local_host"] = str(profile["local_host"])
    else:
        view.update({
            "matrix_config_dir": values["MATRIX_BUS_CFG"],
            "matrix_homeserver": values["MATRIX_BUS_HS"],
            "matrix_room": values["MATRIX_BUS_ROOM"],
            "matrix_registry_room": values["MATRIX_BUS_REGISTRY_ROOM"],
        })
    return view


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "create":
            path, created = create_local_profile(
                args.name,
                tmux_server=args.tmux_server,
                primary_session=args.primary_session,
            )
            server, session, session_created = ensure_primary_session(
                args.name,
                rollback_profile=path if created else None,
            )
            status = "created" if created else "validated existing"
            session_status = "created" if session_created else "using"
            print(f"{status} fleet profile: {path}")
            print(
                f"ready fleet {args.name!r}: {session_status} primary tmux session "
                f"{session!r} on server {server!r}"
            )
            print(f"attach with: tview --fleet {args.name}")
            return 0
        if args.action == "resolve":
            view = public_view(args.name)
            if args.field:
                if args.field not in view:
                    raise FleetProfileError(
                        f"field {args.field!r} does not exist for the default fleet"
                    )
                print(view[args.field])
            else:
                print(json.dumps(view, sort_keys=True, indent=2))
            return 0
        if args.action == "exec":
            command = list(args.command)
            if command and command[0] == "--":
                command.pop(0)
            if not command:
                raise FleetProfileError("exec needs a command after --")
            os.execvpe(command[0], command, command_env(args.name))
        if args.action == "apply-tmux":
            if args.name == "default":
                raise FleetProfileError("apply-tmux requires a named fleet")
            apply_tmux_environment(args.name, dry_run=args.dry_run)
            return 0
    except FleetProfileError as exc:
        print(f"fleet-profile: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
