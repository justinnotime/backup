#!/usr/bin/env python3
"""Record configured harness turn boundaries without blocking the harness."""


from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

import runtime_config as cfg
import workplane as wp

EVENT_KINDS = {"UserPromptSubmit": "start", "Stop": "end"}
CANARY_FILE = cfg.path("turn_report.seats_file")

def _canary(seat: str) -> bool:


    value = os.environ.get("NW_TURN_CANARY_FILE") or CANARY_FILE
    if value is None:
        return False
    path = Path(value)
    try:
        seats = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(seats, list) and seat in seats


def main() -> int:
    if os.environ.get("NW_TURN_REPORT_OFF", "").strip() == "1":
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("start", "end"), default="")
    ap.add_argument("--harness", default="")
    args, _ = ap.parse_known_args()
    kind = args.kind
    if not kind and not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
            kind = EVENT_KINDS.get(payload.get("hook_event_name", ""), "")
            if payload.get("stop_hook_active") is True:
                return 0
        except (json.JSONDecodeError, OSError, AttributeError):
            return 0
    if not kind:
        return 0
    pane = os.environ.get("TMUX_PANE", "").strip().lstrip("%")
    seat = wp.caller_seat_id()
    if not seat or not _canary(seat):
        return 0


    conn = wp.connect_writable(timeout=2)
    try:
        with conn:
            wp.turn_record(conn, seat, kind, pane=pane, harness=args.harness)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
