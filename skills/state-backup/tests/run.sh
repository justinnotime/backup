#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly TEMP_ROOT="$(mktemp -d)"

cleanup() {
  rm -rf -- "${TEMP_ROOT}"
}
trap cleanup EXIT

fail() {
  printf 'test failed: %s\n' "$1" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_not_exists() {
  [[ ! -e "$1" && ! -L "$1" ]] || fail "expected path to be absent: $1"
}

bash -n "${ROOT_DIR}/scripts/backup"

default_home="${TEMP_ROOT}/default"
install -d -m 0700 "${default_home}/.dsh/sessions" "${default_home}/.config/backup"
printf 'session\n' > "${default_home}/.dsh/sessions/example.jsonl"
printf 'secret\n' > "${default_home}/.dsh/.credentials.yaml"
cat > "${default_home}/.config/backup/config" <<EOF
MACHINE_ID="fixture-default"
SYNCTHING_ROOT="${default_home}/sync"
BACKUP_LOG="${default_home}/backup.log"
EOF
HOME="${default_home}" "${ROOT_DIR}/scripts/backup" >/dev/null
assert_file "${default_home}/sync/backup/fixture-default/dsh/sessions/example.jsonl"
assert_not_exists "${default_home}/sync/backup/fixture-default/dsh/.credentials.yaml"

legacy_home="${TEMP_ROOT}/legacy"
install -d -m 0700 \
  "${legacy_home}/.dsh/sessions" \
  "${legacy_home}/.dsh-alternate/sessions" \
  "${legacy_home}/.config/backup"
printf 'default\n' > "${legacy_home}/.dsh/sessions/default.jsonl"
printf 'alternate\n' > "${legacy_home}/.dsh-alternate/sessions/alternate.jsonl"
cat > "${legacy_home}/.config/backup/config" <<EOF
MACHINE_ID="fixture-legacy"
SYNCTHING_ROOT="${legacy_home}/sync"
BACKUP_LOG="${legacy_home}/backup.log"
CLAUDE_PROFILES="alternate:${legacy_home}/.claude-alternate"
CODEX_PROFILES="alternate:${legacy_home}/.codex-alternate"
OPENCODE_PROFILES="alternate:${legacy_home}/.opencode-alternate"
DSH_PROFILES="alternate:${legacy_home}/.dsh-alternate"
EOF
HOME="${legacy_home}" "${ROOT_DIR}/scripts/backup" >/dev/null
assert_file "${legacy_home}/sync/backup/fixture-legacy/dsh-alternate/sessions/alternate.jsonl"
assert_not_exists "${legacy_home}/sync/backup/fixture-legacy/dsh"

printf 'All compatibility tests passed.\n'
