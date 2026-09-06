#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v ss >/dev/null || { echo "FAIL ss is required to verify tmux shutdown" >&2; exit 1; }
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/fleet-staging-test.XXXXXX")"
: >"$STAGE/.fleet-staging"
export TMUX_TMPDIR="$STAGE"
server="fleet-staging-$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$STAGE")"
private_socket="$STAGE/tmux-$(id -u)/$server"
preserve_on_failure() {
  local status=$?
  trap - EXIT
  if (( status != 0 )); then
    echo "NOTE staging test failed; preserved $STAGE" >&2
  fi
  exit "$status"
}
trap preserve_on_failure EXIT

tmux -L "$server" new-session -d -s socket-path-check 'sleep 30'
actual_socket="$(tmux -L "$server" display-message -p '#{socket_path}')"
tmux -L "$server" kill-server
[[ "$actual_socket" == "$private_socket" ]] \
  || { echo "FAIL staging server used unexpected socket $actual_socket" >&2; exit 1; }

bash "$REPO/scripts/fleet-staging.sh" e2e "$STAGE"
listeners="$(ss -xlpH)"
if grep -F -- "$private_socket" <<<"$listeners" >/dev/null; then
  echo "FAIL staging server still listens at $private_socket; preserved $STAGE" >&2
  exit 1
fi
rm -rf "$STAGE"
trap - EXIT
[[ ! -e "$STAGE" ]] || { echo "FAIL staging temp directory survived cleanup: $STAGE" >&2; exit 1; }
