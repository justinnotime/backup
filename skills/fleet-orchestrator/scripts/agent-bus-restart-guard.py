#!/usr/bin/env python3
"""Check whether a proposed slot or tmux pane has an active registration.

Exit 3 for pane conflicts and 4 for a slot active in another pane.
No identities are changed.
"""

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: agent-bus-restart-guard.py <slot> <pane> <host>", file=sys.stderr)
        return 2
    slot, pane, host = sys.argv[1:4]
    bus_config = Path(os.environ.get("AGENT_BUS_CFG") or os.environ.get("MATRIX_BUS_CFG")
                      or cfg.path("bus.config_directory", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fleet-orchestrator" / "bus"))
    db = Path(os.environ.get("AGENT_BUS_DB") or cfg.path("bus.database", bus_config / "agent-bus-v3.sqlite3"))
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    my_pane = os.environ.get("TMUX_PANE", "").strip()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
    has_pane_id = "pane_id" in columns

    # Slot liveness: adopting a slot that is registered to a different live
    # pane is seat theft, not a resume. Compare stable pane ids when both
    # sides have one; else the stored location string.
    row = conn.execute(
        "SELECT handle, slot, agent_id, tmux"
        + (", pane_id" if has_pane_id else "")
        + " FROM identities WHERE status='active' AND slot=? AND host=?",
        (slot, host),
    ).fetchone()
    if row:
        row_pane_id = row[4] if has_pane_id else None
        if row_pane_id and my_pane:
            elsewhere = row_pane_id != my_pane
        else:
            elsewhere = row[3].startswith("tmux=") and row[3] != pane
        if elsewhere:
            print("\t".join(row[:4]))
            return 4

    # Pane occupancy: a second slug booted from an already-seated pane.
    if not pane.startswith("tmux="):
        return 0
    clauses, params = ["tmux=?"], [pane]
    if has_pane_id and my_pane:
        clauses.append("pane_id=?")
        params.append(my_pane)
    row = conn.execute(
        "SELECT handle, slot, agent_id, tmux FROM identities "
        f"WHERE status='active' AND ({' OR '.join(clauses)})"
        " AND host=? AND slot!=?",
        (*params, host, slot),
    ).fetchone()
    if row:
        print("\t".join(row))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
