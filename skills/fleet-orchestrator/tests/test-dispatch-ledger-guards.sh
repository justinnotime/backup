#!/usr/bin/env bash

set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
LEDGER="$REPO/scripts/dispatch-ledger.py"
SANDBOX=$(mktemp -d "${TMPDIR:-/tmp}/dispatch-ledger-test.XXXXXX")
trap 'rm -rf "$SANDBOX"' EXIT
export DISPATCH_LEDGER_DB="$SANDBOX/ledger.sqlite3"
export DISPATCH_LEDGER_ACTOR="test@harness"
export ORC_SEAT_ID="tmux9"

FAILURES=0
L() { python3 "$LEDGER" "$@"; }
new_id() { L open --to tmux9 --subject "$1" --check "true" --after 1h | grep -oE '[0-9a-f]{8}' | head -1; }

expect() {  # expect <wanted-exit> <label> <command...>
  local want="$1" label="$2"; shift 2
  local out; out=$("$@" 2>&1); local got=$?
  if [ "$got" = "$want" ]; then
    printf 'OK    %s\n' "$label"
  else
    FAILURES=$((FAILURES + 1))
    printf 'FAIL  %s (wanted exit %s, got %s)\n%s\n' "$label" "$want" "$got" "$out"
  fi
}

sql() { python3 - "$@" <<'PY'
import os, sqlite3, sys
conn = sqlite3.connect(os.environ["DISPATCH_LEDGER_DB"])
conn.execute(sys.argv[1], sys.argv[2:])
conn.commit()
PY
}

repair_store() {
  PYTHONPATH="$REPO/scripts/lib" python3 - <<'PY'
import workplane
workplane.connect_writable().close()
PY
}

A=$(new_id "fixture A"); B=$(new_id "fixture B"); C=$(new_id "fixture C"); D=$(new_id "fixture D")
L ack "$A" --note "acked" --after 1h >/dev/null
L close "$B" --resolution done >/dev/null

expect 0 "baseline: a clean ledger replays to its stored state" L verify

expect 1 "ack after close is refused at the command" L ack "$B" --after 1h
expect 1 "chase after close is refused at the command" L chase "$B" --after 1h
expect 0 "and the ledger is still consistent afterwards" L verify

expect 1 "the database refuses an illegal state change from raw SQL" \
  sql "UPDATE dispatch SET state='open' WHERE id=?" "$A"
expect 0 "and the row is untouched afterwards" L verify

sql "UPDATE dispatch SET state='closed', resolution='done' WHERE id=?" "$A"
expect 1 "a legal pair the log does not explain is caught by replay" L verify
sql "DROP TRIGGER dispatch_state_legal"
expect 1 "with the trigger dropped, verify says so instead of quietly recreating it" L verify
sql "UPDATE dispatch SET state='acked', resolution='' WHERE id=?" "$A"
repair_store
expect 0 "restoring the true state and the trigger clears it" L verify

sql "DELETE FROM event WHERE dispatch_id=? AND kind='ack'" "$A"
expect 1 "a row ahead of its own history is caught by replay" L verify
sql "INSERT INTO event (dispatch_id, at_ms, actor, kind, note)
     SELECT ?, MAX(at_ms)+1, 'test', 'ack', 'restored' FROM event WHERE dispatch_id=?" "$A" "$A"
expect 0 "restoring the event clears it" L verify

sql "INSERT INTO state_pair (workflow, from_state, to_state) VALUES ('dispatch','closed','open')"
expect 1 "a state_pair row TRANSITIONS does not contain is caught" L verify
sql "DELETE FROM state_pair WHERE from_state='closed'"
expect 0 "removing the invented pair clears it" L verify

L link "$C" supersedes "$A" --note "A is still open here" >/dev/null
expect 1 "supersedes pointing at a live dispatch is caught" L verify
L close "$A" --resolution superseded --by "$C" >/dev/null
expect 0 "the same edge is legal once the superseded node is closed" L verify

L link "$C" blocks "$D" >/dev/null
L link "$D" blocks "$C" >/dev/null
expect 1 "a cycle in blocks is caught" L verify
sql "DELETE FROM edge WHERE kind='blocks' AND src=?" "$D"
expect 0 "breaking the cycle clears it" L verify

sql "INSERT OR REPLACE INTO edge (src,kind,dst,at_ms,actor,note) VALUES ('deadbeef','blocks',?,0,'test','')" "$C"
expect 1 "an edge naming a missing dispatch is caught" L verify

expect 1 "a self-edge is refused" L link "$C" blocks "$C"
expect 2 "an unknown edge kind is refused by the parser" L link "$C" nonsense "$D"

says() {  # says <wanted:yes|no> <label> <needle> <command...>
  local want="$1" label="$2" needle="$3"; shift 3
  local out; out=$("$@" 2>&1)
  local got=no; case "$out" in *"$needle"*) got=yes;; esac
  if [ "$got" = "$want" ]; then
    printf 'OK    %s\n' "$label"
  else
    FAILURES=$((FAILURES + 1))
    printf 'FAIL  %s (wanted %s for %q)\n%s\n' "$label" "$want" "$needle" "$out"
  fi
}

E=$(new_id "fixture E")
L chase "$E" --note "silence 1" --after 1h >/dev/null
says yes "two unanswered chases do advise reassigning" "reassign rather than asking again" \
  L chase "$E" --note "silence 2" --after 1h
says yes "and doctor calls the same node out" "$E chased 2x" L doctor

F=$(new_id "fixture F")
L chase "$F" --note "chase 1" --after 1h >/dev/null
L ack "$F" --note "answered with a fix" --after 1h >/dev/null
says no "an answered chase does not count toward the silence run" "reassign rather than asking again" \
  L chase "$F" --note "chase 2, first since the answer" --after 1h
says no "and doctor does not demand reassigning an answering seat" "$F chased" L doctor
says yes "but the lifetime total stays visible" "2 total" L show "$F"

G=$(new_id "fixture G")
L chase "$G" --note "genuine silence" --after 1h >/dev/null
says no "a note does not advise reassigning" "reassign rather than asking again" \
  L note "$G" --note "relayed the reviewer verdict" --after 1h
says yes "and the earlier real chase is still counted" "chases     1" L show "$G"
sql "DELETE FROM edge WHERE src='deadbeef'"
expect 0 "a note leaves the ledger consistent" L verify
L ack "$G" --note "seat answered" --after 1h >/dev/null
says no "a note on an acked node does not reopen it" "state      open" L show "$G"
expect 1 "note after close is refused like every other verb" L note "$B" --after 1h

if [ "$FAILURES" = 0 ]; then
  echo "---"
  echo "OK    every guard fired on a constructed violation"
  exit 0
fi
echo "---"
echo "FAIL  $FAILURES guard(s) did not fire"
exit 1
