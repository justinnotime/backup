#!/usr/bin/env bash
# tview isolation test: a PRIVATE -L server is the isolation
# boundary (sessions are not), every assertion SAYS what failed, and the
# environment is scrubbed - the pre-rework version died silently at `wait`
# whenever it ran inside a tmux session, because script(1) inherited TMUX
# and tview asked a private server for a client it never had.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TVIEW="$ROOT/scripts/tview"
server="tview-test-$RANDOM-$$"
fleet_name="view-$RANDOM"
fleet_server="tview-fleet-$RANDOM-$$"
stage=$(mktemp -d /tmp/tview-test.XXXXXX)
# tmux leaves a -L socket pathname behind after kill-server. Keep it inside
# this test's existing temporary directory so the EXIT cleanup owns it too.
export TMUX_TMPDIR="$stage"
socket_dir="$TMUX_TMPDIR/tmux-$(id -u)"
cleanup() {
  local cleanup_failed=0 private_server
  for private_server in "$server" "$fleet_server"; do
    if [[ -S "$socket_dir/$private_server" ]] \
       || tmux -L "$private_server" has-session 2>/dev/null; then
      if ! tmux -L "$private_server" kill-server 2>/dev/null; then
        echo "FAIL: could not stop $private_server; preserved $stage" >&2
        cleanup_failed=1
      fi
    fi
  done
  (( cleanup_failed == 0 )) || return 1
  rm -rf "$stage"
}
trap 'cleanup || exit 1' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

cat >"$stage/tmux" <<EOF
#!/usr/bin/env bash
exec tmux -L "$server" "\$@"
EOF
chmod +x "$stage/tmux"

tmux -L "$server" new-session -d -s 0 -n user-shell 'sleep 120' \
  || fail "could not start the private server"
tmux -L "$server" set-option -t 0 destroy-unattached off
tmux -L "$server" new-window -d -t 0:1 -n one 'sleep 120'
tmux -L "$server" new-window -d -t 0:2 -n two 'sleep 120'
tmux -L "$server" new-session -d -s alternate -n alt-zero 'sleep 120'
tmux -L "$server" new-window -d -t '=alternate:5' -n alt-five 'sleep 120'

mkdir -p "$stage/fleets"
cat >"$stage/fleets/$fleet_name.json" <<EOF
{
  "schema": 1,
  "name": "$fleet_name",
  "tmux_server": "$fleet_server",
  "primary_session": "main",
  "matrix_homeserver": "https://matrix.example.test",
  "matrix_room": "!message-$RANDOM:example.test",
  "matrix_registry_room": "!registry-$RANDOM:example.test"
}
EOF
tmux -L "$fleet_server" new-session -d -s main -n fleet-zero 'sleep 120'
tmux -L "$fleet_server" new-window -d -t '=main:7' -n fleet-seven 'sleep 120'

for private_server in "$server" "$fleet_server"; do
  actual_socket=$(tmux -L "$private_server" display-message -p '#{socket_path}')
  [[ "$actual_socket" == "$socket_dir/$private_server" ]] \
    || fail "private server $private_server used unexpected socket $actual_socket"
  [[ ! -e "/tmp/tmux-$(id -u)/$private_server" ]] \
    || fail "private server $private_server leaked a socket into global /tmp"
done

view_flow() {  # $1 = log name, $2 = window keys
  # TMUX= scrubs the leaked outer-tmux variable: tview must key the view
  # off the pty, not ask the PRIVATE server about an outer client
  ( sleep 1; printf "$2"; sleep 1; printf '\002d' ) \
    | TERM=xterm-256color timeout 10 script -qec \
      "TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN=$stage/tmux $TVIEW" /dev/null \
      >"$stage/$1.log" 2>&1
}

view_flow a '\0022' & a=$!
view_flow b '\0021' & b=$!
wait "$a" || fail "view flow A exited nonzero: $(tail -3 "$stage/a.log" | tr '\n' ' ')"
wait "$b" || fail "view flow B exited nonzero: $(tail -3 "$stage/b.log" | tr '\n' ' ')"

fmt='#{session_name}|#{session_group}|#{session_attached}|#{destroy_unattached}'
sessions=$(tmux -L "$server" list-sessions -F "$fmt")
grep -q '^0|' <<<"$sessions" || fail "primary session missing: $sessions"
[[ $(grep -c '^tview-' <<<"$sessions") -eq 2 ]] \
  || fail "expected 2 tview views, got: $sessions"
grep -q '|on$' <<<"$sessions" \
  && fail "destroy-unattached must never be on: $sessions"
[[ $(tmux -L "$server" list-windows -t 0 | wc -l) -eq 3 ]] \
  || fail "primary window count changed"
[[ $(tmux -L "$server" list-panes -a -F '#{pane_id}' | sort -u | wc -l) -eq 5 ]] \
  || fail "grouped views must share panes, not copy them"

# The no-argument path above must still mean session 0. One positional
# argument now always selects a window in that internal primary session,
# first by index and then by exact name.
( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET=default TMUX_BIN=$stage/tmux $TVIEW 2" \
    /dev/null >"$stage/positional-index.log" 2>&1 \
  || fail "default-fleet positional window-index flow failed: $(tail -3 "$stage/positional-index.log" | tr '\n' ' ')"

index_rows=$(tmux -L "$server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|0\|2$' <<<"$index_rows" \
  || fail "positional window index 2 was not selected in session 0: $index_rows"

( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN=$stage/tmux $TVIEW one" \
    /dev/null >"$stage/positional-name.log" 2>&1 \
  || fail "positional window-name flow failed: $(tail -3 "$stage/positional-name.log" | tr '\n' ' ')"

group_rows=$(tmux -L "$server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|0\|1$' <<<"$group_rows" \
  || fail "exact positional window name 'one' was not selected: $group_rows"
awk -F'|' '$1 ~ /^tview-/ && $2 != "0" {exit 1}' <<<"$group_rows" \
  || fail "a positional window unexpectedly selected another session: $group_rows"

# Merely having a named-fleet profile must not redirect the no-argument path.
# The flows above used only the original private server; the fleet server is
# still untouched until --fleet is explicit.
if tmux -L "$fleet_server" list-sessions -F '#{session_name}' | grep -q '^tview-'; then
  fail "no-argument tview leaked into the named fleet server"
fi

# Legacy inside-tmux behavior: even when called from another session group,
# bare tview keeps the calling window index while switching to session 0's
# grouped view.
cat >"$stage/same-server-command" <<EOF
#!/usr/bin/env bash
exec env TERM=xterm-256color NW_FLEET= TMUX_BIN=$(command -v tmux) $TVIEW
EOF
chmod +x "$stage/same-server-command"
tmux -L "$server" new-window -d -t '=alternate:1' -n alt-one \
  'exec bash --noprofile --norc -i'
same_input="$stage/same-server-input"
mkfifo "$same_input"
exec {same_input_fd}<>"$same_input"
TERM=xterm-256color timeout 15 script -qec \
  "TERM=xterm-256color TMUX= $(command -v tmux) -L $server attach-session -t 'alternate:1'" \
  /dev/null <"$same_input" >"$stage/same-server.log" 2>&1 &
same_pid=$!

same_source_row=
for _ in {1..50}; do
  same_source_row=$(tmux -L "$server" list-clients \
    -F '#{client_tty}|#{session_name}|#{window_index}' 2>/dev/null \
    | head -n 1 || true)
  [[ "$same_source_row" == *'|alternate|1' ]] && break
  sleep 0.1
done
[[ "$same_source_row" == *'|alternate|1' ]] \
  || fail "client never reached alternate window 1: $same_source_row"
same_client_tty=${same_source_row%%|*}
tmux -L "$server" send-keys -t 'alternate:1' "$stage/same-server-command" Enter

same_target_row=
for _ in {1..50}; do
  same_target_row=$(tmux -L "$server" list-clients \
    -F '#{client_tty}|#{session_name}|#{session_group}|#{window_index}' \
    2>/dev/null | head -n 1 || true)
  [[ "$same_target_row" == *'|0|1' ]] && break
  sleep 0.1
done
IFS='|' read -r same_target_tty same_target_session same_target_group \
  same_target_window <<<"$same_target_row"
[[ "$same_target_tty" == "$same_client_tty" \
   && "$same_target_session" == tview-* \
   && "$same_target_group" == 0 \
   && "$same_target_window" == 1 ]] \
  || fail "bare tview did not preserve window 1 across session groups: $same_target_row"
tmux -L "$server" detach-client -t "$same_target_tty"
wait "$same_pid" \
  || fail "same-server client flow exited nonzero: $(tail -3 "$stage/same-server.log" | tr '\n' ' ')"
exec {same_input_fd}>&-

( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN=$(command -v tmux) TVIEW_FLEET_PROFILE=$ROOT/scripts/lib/fleet-profile.py NW_FLEET_PROFILE_DIR=$stage/fleets $TVIEW --fleet $fleet_name 7" \
    /dev/null >"$stage/named-fleet.log" 2>&1 \
  || fail "named-fleet flow failed: $(tail -3 "$stage/named-fleet.log" | tr '\n' ' ')"

fleet_rows=$(tmux -L "$fleet_server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|main\|7$' <<<"$fleet_rows" \
  || fail "--fleet did not attach its configured server/window: $fleet_rows"
grep -q '^main|' <<<"$fleet_rows" \
  || fail "named fleet primary session disappeared: $fleet_rows"
tmux -L "$server" has-session -t '=main' 2>/dev/null \
  && fail "named fleet session leaked into the original server"

# Inside a named fleet, the inherited NW_FLEET is enough: callers only name
# the destination window and tview resolves both server and primary session.
( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET=$fleet_name TMUX_BIN=$(command -v tmux) TVIEW_FLEET_PROFILE=$ROOT/scripts/lib/fleet-profile.py NW_FLEET_PROFILE_DIR=$stage/fleets $TVIEW fleet-seven" \
    /dev/null >"$stage/inherited-fleet.log" 2>&1 \
  || fail "inherited-fleet flow failed: $(tail -3 "$stage/inherited-fleet.log" | tr '\n' ' ')"

inherited_rows=$(tmux -L "$fleet_server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|main\|7$' <<<"$inherited_rows" \
  || fail "NW_FLEET did not select the named fleet and exact window: $inherited_rows"

# Exercise the cross-server handoff from a real attached tmux client. The
# command runs inside server A, so tview must use detach-client -E to replace
# that terminal's client with an attachment to server B's selected window.
cat >"$stage/handoff-command" <<EOF
#!/usr/bin/env bash
exec env TERM=xterm-256color \
  TMUX_BIN=$(command -v tmux) \
  TVIEW_FLEET_PROFILE=$ROOT/scripts/lib/fleet-profile.py \
  NW_FLEET_PROFILE_DIR=$stage/fleets \
  $TVIEW --fleet $fleet_name --window 7
EOF
chmod +x "$stage/handoff-command"

tmux -L "$server" new-window -d -t '0:9' -n handoff-shell \
  'exec bash --noprofile --norc -i'
a_sessions_before=$(tmux -L "$server" list-sessions -F '#{session_name}' | sort)
b_sessions_before=$(tmux -L "$fleet_server" list-sessions -F '#{session_name}' | sort)

handoff_input="$stage/handoff-input"
mkfifo "$handoff_input"
exec {handoff_input_fd}<>"$handoff_input"
TERM=xterm-256color timeout 15 script -qec \
  "TERM=xterm-256color TMUX= $(command -v tmux) -L $server attach-session -t '0:9'" \
  /dev/null <"$handoff_input" >"$stage/handoff.log" 2>&1 &
handoff_pid=$!

a_client_tty=
for _ in {1..50}; do
  a_client_tty=$(tmux -L "$server" list-clients -F '#{client_tty}' \
    2>/dev/null | head -n 1 || true)
  [[ -n "$a_client_tty" ]] && break
  sleep 0.1
done
[[ -n "$a_client_tty" ]] \
  || fail "client never attached to server A: $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"

tmux -L "$server" send-keys -t '0:9' "$stage/handoff-command" Enter

b_client_row=
for _ in {1..50}; do
  b_client_row=$(tmux -L "$fleet_server" list-clients \
    -F '#{client_tty}|#{session_name}|#{window_index}' 2>/dev/null \
    | head -n 1 || true)
  [[ "$b_client_row" == *'|7' ]] && break
  sleep 0.1
done
[[ "$b_client_row" == *'|7' ]] \
  || fail "client did not reach server B window 7: $b_client_row; $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"
IFS='|' read -r b_client_tty b_client_session b_client_window <<<"$b_client_row"
[[ "$b_client_tty" == "$a_client_tty" ]] \
  || fail "cross-server handoff changed terminal: A=$a_client_tty B=$b_client_tty"
[[ "$b_client_session" == tview-* && "$b_client_window" == 7 ]] \
  || fail "server B client is not in the requested tview window: $b_client_row"

tmux -L "$fleet_server" detach-client -t "$b_client_tty"
wait "$handoff_pid" \
  || fail "cross-server client flow exited nonzero: $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"
exec {handoff_input_fd}>&-

a_sessions_after=$(tmux -L "$server" list-sessions -F '#{session_name}' | sort)
b_sessions_after=$(tmux -L "$fleet_server" list-sessions -F '#{session_name}' | sort)
missing_a=$(comm -23 <(printf '%s\n' "$a_sessions_before") \
  <(printf '%s\n' "$a_sessions_after"))
missing_b=$(comm -23 <(printf '%s\n' "$b_sessions_before") \
  <(printf '%s\n' "$b_sessions_after"))
[[ -z "$missing_a" && -z "$missing_b" ]] \
  || fail "cross-server handoff deleted sessions: A=[$missing_a] B=[$missing_b]"
tmux -L "$server" has-session -t '=0' \
  || fail "server A primary session disappeared during handoff"
tmux -L "$fleet_server" has-session -t '=main' \
  || fail "server B primary session disappeared during handoff"

if TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN="$stage/tmux" "$TVIEW" 99 \
    >"$stage/missing-window.log" 2>&1; then
  fail "missing window was accepted"
fi
grep -q "window '99' not found in session '0'" "$stage/missing-window.log" \
  || fail "missing-window error was not explicit"

if TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN="$stage/tmux" \
    "$TVIEW" alternate 99 >"$stage/old-two-positionals.log" 2>&1; then
  fail "the retired session/window positional form was accepted"
fi
grep -q "session selection was removed; expected at most one WINDOW" \
  "$stage/old-two-positionals.log" \
  || fail "the retired two-positional form did not explain the replacement"

if TERM=xterm-256color TMUX= NW_FLEET= TMUX_BIN="$stage/tmux" \
    "$TVIEW" --session alternate --window 5 >"$stage/old-session-option.log" 2>&1; then
  fail "the retired --session option was accepted"
fi
grep -q -- "--session was removed; choose a fleet and window only" \
  "$stage/old-session-option.log" \
  || fail "the retired --session option did not explain the replacement"

# ---- reap prong on the REAL private server ----
# both views are now detached; with a 1s idle bar and 2s of quiet they are
# reapable - but the kill switch must hold everything, and the primary
# must survive every pass by construction
sleep 2
TVIEW_REAP=off view_flow c '\0020' \
  || fail "kill-switch flow exited nonzero: $(tail -3 "$stage/c.log" | tr '\n' ' ')"
[[ $(tmux -L "$server" list-sessions -F '#{session_name}' | grep -c '^tview-') -ge 2 ]] \
  || fail "TVIEW_REAP=off must reap nothing"

sleep 2
TVIEW_REAP_IDLE_S=1 view_flow d '\0020' \
  || fail "reap flow exited nonzero: $(tail -3 "$stage/d.log" | tr '\n' ' ')"
after=$(tmux -L "$server" list-sessions -F "$fmt")
grep -q '^0|' <<<"$after" || fail "the PRIMARY session was reaped: $after"
live_views=$(grep -c '^tview-' <<<"$after") || true
# the reap pass ran before flow-d created/attached its own view: the two
# idle detached views from earlier must be gone, flow-d's own view remains
[[ "$live_views" -le 1 ]] \
  || fail "detached idle views survived the reap: $after"

# ---- source-code contract (updated for the sanctioned reap) ----
# kill-session may appear ONLY inside the marked reap block; kill-server
# and destroy-unattached-on stay banned everywhere in the executable path
source_code=$(awk '
  /^  cat <<.USAGE./ {in_help=1; next}
  in_help && /^USAGE$/ {in_help=0; next}
  /^# Reap ONLY detached tview-/ {in_reap=1}
  in_reap && /^fi$/ {in_reap=0; next}
  !in_help && !in_reap && $0 !~ /^[[:space:]]*#/ {print}
' "$TVIEW")
grep -Eq '(^|[;&|[:space:]])kill-server([;&|[:space:]]|$)' <<<"$source_code" \
  && fail "kill-server found in tview executable path"
grep -Eq '(^|[;&|[:space:]])kill-session([;&|[:space:]]|$)' <<<"$source_code" \
  && fail "kill-session found OUTSIDE the sanctioned reap block"
grep -Eq 'destroy-unattached[[:space:]]+on' <<<"$source_code" \
  && fail "destroy-unattached on found in tview executable path"
reap_block=$(awk '/^# Reap ONLY detached tview-/,/^fi$/' "$TVIEW")
grep -q 'tview-\*)' <<<"$reap_block" \
  || fail "the reap block lost its tview-* namespace filter"
grep -q '"$attached" == "0"' <<<"$reap_block" \
  || fail "the reap block lost its detached-only filter"

echo "tview integration test: ok (views grouped, reap bounded, primary untouchable)"
