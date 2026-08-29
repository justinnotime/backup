#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly SKILL_DIR="${ROOT_DIR}/.agents/skills/agent-harness-profiles"
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

for script in "${ROOT_DIR}/backup.sh" "${SKILL_DIR}/scripts/"*; do
  bash -n "${script}"
done

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

launchers="${legacy_home}/launchers.sh"
HOME="${legacy_home}" "${SKILL_DIR}/scripts/render-launchers.sh" \
  --config "${legacy_home}/.config/backup/config" --output "${launchers}" >/dev/null
bash -n "${launchers}"
command -v zsh >/dev/null 2>&1 && zsh -n "${launchers}"
grep -q '^claude-alternate()' "${launchers}" || fail 'Claude launcher missing'
grep -q '^codex-alternate()' "${launchers}" || fail 'Codex launcher missing'
grep -q '^opencode-alternate()' "${launchers}" || fail 'OpenCode launcher missing'
grep -q '^dsh-alternate()' "${launchers}" || fail 'DSH launcher missing'

install_home="${TEMP_ROOT}/install"
install -d -m 0700 "${install_home}/.config/backup" "${install_home}/.config/example-tool"
cat > "${install_home}/.config/backup/config" <<EOF
CLAUDE_PROFILES="alternate:${install_home}/.claude-alternate"
OPENCODE_PROFILES="alternate:${install_home}/.opencode-alternate"
EOF
HOME="${install_home}" "${SKILL_DIR}/scripts/prepare-opencode-roots.sh" \
  --config "${install_home}/.config/backup/config" >/dev/null
[[ -L "${install_home}/.opencode-alternate/config/example-tool" ]] ||
  fail 'OpenCode config link missing'
assert_file "${install_home}/.opencode-alternate/config/opencode/opencode.json"

HOME="${install_home}" "${SKILL_DIR}/scripts/install-links.sh" \
  --config "${install_home}/.config/backup/config" >/dev/null
[[ "$(readlink -f -- "${install_home}/.agents/skills/agent-harness-profiles")" == "${SKILL_DIR}" ]] ||
  fail 'shared skill link has the wrong target'
[[ "$(readlink -f -- "${install_home}/.claude-alternate/skills/agent-harness-profiles")" == "${SKILL_DIR}" ]] ||
  fail 'Claude profile skill link has the wrong target'
[[ "$(readlink -f -- "${install_home}/bin/backup")" == "${ROOT_DIR}/backup.sh" ]] ||
  fail 'backup command link has the wrong target'

divergent_home="${TEMP_ROOT}/divergent"
install -d -m 0700 "${divergent_home}/.agents/skills/agent-harness-profiles"
if HOME="${divergent_home}" "${SKILL_DIR}/scripts/install-links.sh" --without-command >/dev/null 2>&1; then
  fail 'installer accepted a divergent skill target'
fi

forbidden_label_one='pers''onal'
forbidden_label_two='wo''rk'
if grep -R -n -i -E "\\b(${forbidden_label_one}|${forbidden_label_two})\\b" \
  --exclude-dir=.git --exclude=run.sh "${ROOT_DIR}" >/dev/null; then
  fail 'repository contains a policy-specific profile label'
fi

printf 'All compatibility tests passed.\n'
