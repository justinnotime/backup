#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SKILL_DIR}/../.." && pwd -P)"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"

usage() {
  printf 'usage: %s [--config FILE]\n' "${0##*/}" >&2
}

fail() {
  printf 'link installation failed: %s\n' "$1" >&2
  exit 1
}

if (( $# > 0 )); then
  [[ $# -eq 2 && $1 == --config ]] || { usage; exit 2; }
  config_file=$2
fi

[[ -f "${config_file}" ]] || fail "configuration file not found: ${config_file}"
# shellcheck disable=SC1090
source "${config_file}"
CLAUDE_HOME="${CLAUDE_HOME:-${HOME}/.claude}"
CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"

targets=(
  "${HOME}/.agents/skills/agent-harness-profiles"
  "${CLAUDE_HOME}/skills/agent-harness-profiles"
)
sources=("${SKILL_DIR}" "${SKILL_DIR}")
for entry in ${CLAUDE_PROFILES}; do
  label=${entry%%:*}
  root=${entry#*:}
  [[ -n "${label}" && "${label}" != "${entry}" && -n "${root}" ]] ||
    fail "malformed CLAUDE_PROFILES entry: ${entry}"
  [[ "${label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "unsafe Claude label: ${label}"
  [[ "${root}" == /* && "${root}" != / && "${root}" != "${HOME}" ]] ||
    fail "Claude root must be an absolute non-home path for label ${label}"
  targets+=("${root}/skills/agent-harness-profiles")
  sources+=("${SKILL_DIR}")
done

targets+=("${HOME}/bin/backup")
sources+=("${REPO_ROOT}/backup.sh")

for index in "${!targets[@]}"; do
  target=${targets[$index]}
  source_path=${sources[$index]}
  if [[ -e "${target}" || -L "${target}" ]]; then
    [[ -L "${target}" ]] || fail "refusing non-symlink target: ${target}"
    [[ "$(readlink -f -- "${target}" 2>/dev/null || true)" == "$(readlink -f -- "${source_path}")" ]] ||
      fail "refusing divergent symlink: ${target}"
  fi
done

for index in "${!targets[@]}"; do
  target=${targets[$index]}
  source_path=${sources[$index]}
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    install -d -m 0700 "$(dirname -- "${target}")"
    ln -s -- "${source_path}" "${target}"
  fi
done

printf 'Installed Agent Harness Profiles links.\n'
