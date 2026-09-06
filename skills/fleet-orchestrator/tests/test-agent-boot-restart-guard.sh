#!/usr/bin/env bash
# Regression tests for scripts/agent-bus-restart-guard.py (one pane = one seat).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$ROOT/scripts/agent-bus-restart-guard.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export AGENT_BUS_DB="$TMP/bus.sqlite3"

python3 - "$AGENT_BUS_DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("""CREATE TABLE identities (
  agent_id TEXT PRIMARY KEY, slot TEXT UNIQUE NOT NULL, handle TEXT UNIQUE NOT NULL,
  generation INTEGER NOT NULL, status TEXT NOT NULL, harness TEXT NOT NULL,
  mode TEXT NOT NULL, host TEXT NOT NULL, tmux TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]', created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL, lease_until_ms INTEGER)""")
conn.execute("INSERT INTO identities VALUES ('id-old','codex/old-task','host-b/old-task-tmux8',"
             "1,'active','codex','pull','HOST','tmux=0:8.0 win=codex','[]',0,0,NULL)")
conn.execute("INSERT INTO identities VALUES ('id-dead','codex/dead-task','host-b/dead-task-tmux9',"
             "1,'retired','codex','pull','HOST','tmux=0:9.0 win=codex','[]',0,0,NULL)")
conn.execute("INSERT INTO identities VALUES ('id-float','host-b/floating','host-b/floating',"
             "1,'active','claude','watch','HOST','','[]',0,0,NULL)")
conn.commit()
EOF

fail() { echo "FAIL: $1"; exit 1; }

run() { # run <slot> <pane> <host>; sets rc + out
  set +e
  out=$(python3 "$GUARD" "$1" "$2" "$3")
  rc=$?
  set -e
}

run host-b/new-task 'tmux=0:8.0 win=codex' HOST
[ "$rc" = 3 ] || fail "occupied pane: expected rc 3, got $rc"
printf '%s' "$out" | grep -q 'host-b/old-task-tmux8' || fail "conflicting handle not printed"
printf '%s' "$out" | grep -q 'codex/old-task' || fail "conflicting slot not printed"

run codex/old-task 'tmux=0:8.0 win=codex' HOST
[ "$rc" = 0 ] || fail "same-slot resume: expected rc 0, got $rc"

run host-b/new-task 'tmux=0:7.0 win=codex' HOST
[ "$rc" = 0 ] || fail "free pane: expected rc 0, got $rc"

run host-b/new-task 'tmux=0:9.0 win=codex' HOST
[ "$rc" = 0 ] || fail "retired seat must not block: expected rc 0, got $rc"

run host-b/new-task 'no-tmux' HOST
[ "$rc" = 0 ] || fail "non-tmux session: expected rc 0, got $rc"

run host-b/new-task 'tmux=0:8.0 win=codex' OTHERHOST
[ "$rc" = 0 ] || fail "other host: expected rc 0, got $rc"

run codex/old-task 'tmux=0:6.0 win=claude' HOST
[ "$rc" = 4 ] || fail "live slot from another pane: expected rc 4, got $rc"
printf '%s' "$out" | grep -q 'host-b/old-task-tmux8' || fail "stolen seat handle not printed"
printf '%s' "$out" | grep -q 'tmux=0:8.0 win=codex' || fail "owning pane not printed"

run codex/old-task 'no-tmux' HOST
[ "$rc" = 4 ] || fail "live tmux slot from non-tmux session: expected rc 4, got $rc"

run codex/old-task 'tmux=0:6.0 win=claude' OTHERHOST
[ "$rc" = 0 ] || fail "same slot other host: expected rc 0, got $rc"

run codex/dead-task 'tmux=0:6.0 win=claude' HOST
[ "$rc" = 0 ] || fail "retired slot adoptable: expected rc 0, got $rc"

run host-b/floating 'tmux=0:6.0 win=claude' HOST
[ "$rc" = 0 ] || fail "slot with non-tmux registration undecidable: expected rc 0, got $rc"

export AGENT_BUS_DB="$TMP/absent.sqlite3"
run host-b/new-task 'tmux=0:8.0 win=codex' HOST
[ "$rc" = 0 ] || fail "missing DB fail-open: expected rc 0, got $rc"

echo "restart-guard: all checks passed"
