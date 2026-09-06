#!/usr/bin/env python3
"""Compatibility shim for the retired implicit turn-hook installer.

Rollout is now selected one artifact x harness x seat at a time through
scripts/rollout-control.py. This command is intentionally read-only: it prints
status and never edits hooks, trust state, DSH profiles, or OpenCode plugins.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "rollout-control.py"


def main() -> int:
    print("install-turn-hooks: deprecated; no files changed")
    print("Use rollout-control.py stage/install for one explicit target; activation and trust remain separate.")
    return subprocess.run([
        sys.executable, str(CLI), "status", "--artifact", "claude-turn-hooks"
    ]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
