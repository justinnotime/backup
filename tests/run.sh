#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly TEMP_ROOT="$(mktemp -d)"

cleanup() {
  case "${TEMP_ROOT}" in
    /tmp/*) rm -rf -- "${TEMP_ROOT}" ;;
    *) printf 'refusing unsafe test cleanup: %s\n' "${TEMP_ROOT}" >&2 ;;
  esac
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

bash -n "${ROOT_DIR}/backup.sh"

default_home="${TEMP_ROOT}/default"
install -d -m 0700 "${default_home}/.dsh/sessions" "${default_home}/.config/backup"
printf 'session\n' > "${default_home}/.dsh/sessions/example.jsonl"
printf 'secret\n' > "${default_home}/.dsh/.credentials.yaml"
cat > "${default_home}/.config/backup/config" <<EOF
MACHINE_ID="fixture-default"
SYNCTHING_ROOT="${default_home}/sync"
BACKUP_LOG="${default_home}/backup.log"
EOF
HOME="${default_home}" "${ROOT_DIR}/backup.sh" >/dev/null
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
HOME="${legacy_home}" "${ROOT_DIR}/backup.sh" >/dev/null
assert_file "${legacy_home}/sync/backup/fixture-legacy/dsh-alternate/sessions/alternate.jsonl"
assert_not_exists "${legacy_home}/sync/backup/fixture-legacy/dsh"

profile_home="${TEMP_ROOT}/profiles"
profile_config="${profile_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${profile_config}")" "${profile_home}/.config/example-tool"
cat > "${profile_config}" <<EOF
MACHINE_ID="fixture-profiles"
BACKUP_ROOT="${profile_home}/sync/backup/fixture-profiles"
BACKUP_LOG="${profile_home}/backup.log"
CLAUDE_PROFILES="alpha:${profile_home}/.claude-alpha beta:${profile_home}/.claude-beta"
CODEX_PROFILES="alpha:${profile_home}/.codex-alpha beta:${profile_home}/.codex-beta"
OPENCODE_PROFILES="alpha:${profile_home}/.opencode-alpha beta:${profile_home}/.opencode-beta"
DSH_PROFILES="alpha:${profile_home}/.dsh-alpha
beta:${profile_home}/.dsh-beta"
EOF
HOME="${profile_home}" \
  "${ROOT_DIR}/skills/agent-harness-profiles/scripts/install.sh" \
  --config "${profile_config}" >/dev/null
for launcher in claude-alpha claude-beta codex-alpha codex-beta opencode-alpha opencode-beta dsh-alpha dsh-beta; do
  grep -q "^${launcher}()" "${profile_home}/.config/agent-harness-profiles/launchers.sh" ||
    fail "generated launcher is missing: ${launcher}"
done
for label in alpha beta; do
  assert_file "${profile_home}/.opencode-${label}/config/opencode/opencode.json"
  [[ -L "${profile_home}/.claude-${label}/skills/agent-harness-profiles" ]] ||
    fail "configured Claude Skill link is missing: ${label}"
done
[[ -L "${profile_home}/.agents/skills/agent-harness-profiles" ]] ||
  fail 'shared Agent Skill link is missing'
[[ "$(readlink -f -- "${profile_home}/bin/backup")" == "$(readlink -f -- "${ROOT_DIR}/backup.sh")" ]] ||
  fail 'stable Backup command link has the wrong target'

forbidden_label_one='pers''onal'
forbidden_label_two='wo''rk'
if grep -R -n -i -E "\\b(${forbidden_label_one}|${forbidden_label_two})\\b" \
  --exclude-dir=.git --exclude=run.sh "${ROOT_DIR}" >/dev/null; then
  fail 'repository contains a policy-specific profile label'
fi

printf 'All compatibility tests passed.\n'
