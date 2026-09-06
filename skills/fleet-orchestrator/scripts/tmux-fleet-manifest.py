#!/usr/bin/env python3
"""Write or verify a recoverable tmux agent-window manifest. 0 LLM calls."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import tmux_runtime  # noqa: E402

FIELDS = ("window", "name", "command", "path", "pane_id", "pane_pid", "session_id")
AGENTS = {"claude", "codex", "opencode"}


def run(*args: str) -> str:
    result = subprocess.run([*tmux_runtime.base_cmd(), *args],
                            text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "tmux command failed")
    return result.stdout.rstrip("\n")


def process_args(pid: str) -> str:
    result = subprocess.run(["ps", "-o", "args=", "-p", pid], text=True,
                            capture_output=True)
    return result.stdout.strip()


def descendant_commands(root_pid: str) -> list[str]:
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], text=True,
                            capture_output=True)
    children: dict[str, list[tuple[str, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            children.setdefault(parts[1], []).append((parts[0], parts[2]))
    commands, queue = [], [root_pid]
    while queue:
        parent = queue.pop()
        for pid, command in children.get(parent, []):
            commands.append(command)
            queue.append(pid)
    return commands


def session_id(command: str, args: str, pane_pid: str) -> str:
    words = args.split()
    flag = "--resume" if command == "claude" else "resume" if command == "codex" else ""
    if flag and flag in words:
        index = words.index(flag)
        if index + 1 < len(words):
            return words[index + 1].strip("'\"")
    if command == "opencode":
        # OpenCode's bridge supervises a watcher keyed by its durable Agent Bus
        # identity; the TUI command line itself does not expose a session id.
        for child in descendant_commands(pane_pid):
            words = child.split()
            if "agent-bus-v3.py" in child and "watch" in words:
                return "agent-bus:" + words[words.index("watch") + 1]
    return ""


def live_rows(primary: str) -> list[dict[str, str]]:
    fmt = "|".join(("#{window_index}", "#{window_name}", "#{pane_current_command}",
                    "#{pane_current_path}", "#{pane_id}", "#{pane_pid}"))
    rows = []
    for line in run("list-windows", "-t", f"={primary}", "-F", fmt).splitlines():
        window, name, command, path, pane_id, pane_pid = line.split("|", 5)
        if command not in AGENTS:
            continue
        sid = session_id(command, process_args(pane_pid), pane_pid)
        rows.append(dict(zip(FIELDS, (window, name, command, path, pane_id,
                                     pane_pid, sid), strict=True)))
    rows.sort(key=lambda row: int(row["window"]))
    return rows


def write_manifest(path: Path, primary: str) -> None:
    rows = live_rows(primary)
    missing = [row["window"] for row in rows if not row["session_id"]]
    if missing:
        raise SystemExit(f"refusing incomplete manifest; missing session ids: {missing}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, FIELDS, delimiter="|")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    print(f"wrote {len(rows)} agent rows: {path}")


def verify_manifest(path: Path, primary: str) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        expected = list(csv.DictReader(handle, delimiter="|"))
    for row in expected:
        if "session_id" not in row and "session_or_thread_id" in row:
            row["session_id"] = row["session_or_thread_id"]
    live = {row["window"]: row for row in live_rows(primary)}
    errors = []
    for row in expected:
        actual = live.get(row["window"])
        if actual is None:
            errors.append(f"window {row['window']} missing")
            continue
        for field in ("name", "command", "path", "session_id"):
            if actual[field] != row[field]:
                errors.append(f"window {row['window']} {field}:"
                              f" {actual[field]!r} != {row[field]!r}")
    extra = sorted(set(live) - {row["window"] for row in expected}, key=int)
    if extra:
        errors.append(f"unexpected agent windows: {extra}")
    if errors:
        raise SystemExit("manifest verification failed:\n" + "\n".join(errors))
    print(f"verified {len(expected)} agent rows against session {primary}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("write", "verify"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--session", default="0")
    args = parser.parse_args()
    if args.action == "write":
        write_manifest(args.path, args.session)
    else:
        verify_manifest(args.path, args.session)


if __name__ == "__main__":
    main()
