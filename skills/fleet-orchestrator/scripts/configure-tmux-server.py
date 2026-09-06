#!/usr/bin/env python3
"""Configure the named tmux server used by ORC and peer-message tools.

This is the migration hook: validate the target server before publishing the
machine-local selector. A failed validation leaves the previous selector and
ORC drive counters untouched. A successful change resets only pane-absence
counters; task state and wake-ladder state remain unchanged.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
import tmux_runtime  # noqa: E402
import workplane as wp  # noqa: E402


def validate_reachable(server: str | None) -> tuple[int, int]:
    base = ["tmux", "-L", server] if server else ["tmux"]
    result = subprocess.run(
        [*base, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_command}"],
        text=True, capture_output=True, check=False, timeout=15,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "tmux server is unreachable")
    # Grouped sessions repeat each physical pane; count pane IDs, not rows.
    panes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        try:
            pane_id, command = line.split("\t", 1)
        except ValueError:
            continue
        panes.setdefault(pane_id, command)
    agents = sum(
        1 for command in panes.values()
        if command.rsplit("/", 1)[-1] in {"claude", "codex", "opencode"}
    )
    return len(panes), agents


def write_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        tmp.replace(path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def remove_durable(path: Path) -> None:
    """Remove a selector durably; success means the directory entry is synced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server", nargs="?", help="tmux -L socket name")
    parser.add_argument("--default", action="store_true",
                        help="select tmux's default server and remove the selector")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.default == bool(args.server):
        parser.error("give exactly one server name or --default")
    server = None if args.default else tmux_runtime.validate_server(
        args.server, "command line")
    panes, agents = validate_reachable(server)
    if panes == 0:
        raise SystemExit("refusing a reachable tmux server with zero panes")
    path = tmux_runtime.config_path()
    if args.dry_run:
        print(f"OK would select {'default' if server is None else server}:"
              f" panes={panes} agent_panes={agents} config={path}")
        return 0
    lock_path = tmux_runtime.nw_paths.lock_path("fleet-orchestrator")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Reset counters BEFORE publishing. A crash in between is harmless:
        # the old selector remains active with conservative zero counters. Once
        # the new selector is durably visible, the counters are already safe.
        try:
            conn = wp.connect_writable()
            with conn:
                reset = conn.execute(
                    "UPDATE drive SET absent_ticks=0 WHERE absent_ticks!=0"
                ).rowcount
            if server is None:
                remove_durable(path)
            else:
                write_atomic(path, server)
        except (OSError, sqlite3.Error) as exc:
            raise SystemExit(
                f"tmux selector not published; pane-absence counters may have"
                f" been conservatively reset: {exc}"
            )
    print(f"OK selected {'default' if server is None else server}: panes={panes}"
          f" agent_panes={agents}; reset {reset} pane-absence counter(s); config={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
