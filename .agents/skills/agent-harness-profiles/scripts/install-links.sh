#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SKILL_DIR}/../../../" && pwd -P)"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
install_command=1

usage() {
  printf 'usage: %s [--config FILE] [--without-command]\n' "${0##*/}" >&2
}

fail() {
  printf 'link installation failed: %s\n' "$1" >&2
  exit 1
}

while (( $# > 0 )); do
  case "$1" in
    --config)
      (( $# >= 2 )) || { usage; exit 2; }
      config_file=$2
      shift 2
      ;;
    --without-command)
      install_command=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

CLAUDE_HOME="${CLAUDE_HOME:-${HOME}/.claude}"
CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"
if [[ -f "${config_file}" ]]; then
  # shellcheck disable=SC1090
  source "${config_file}"
fi

skill_targets=(
  "${HOME}/.agents/skills/agent-harness-profiles"
  "${HOME}/.claude/skills/agent-harness-profiles"
  "${CLAUDE_HOME}/skills/agent-harness-profiles"
)

seen_labels=""
for entry in ${CLAUDE_PROFILES:-}; do
  label=${entry%%:*}
  root=${entry#*:}
  [[ -n "${label}" && "${label}" != "${entry}" && -n "${root}" ]] ||
    fail "malformed CLAUDE_PROFILES entry: ${entry}"
  [[ "${label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "unsafe Claude label: ${label}"
  [[ "${root}" == /* && "${root}" != / ]] || fail "Claude root must be absolute for label ${label}"
  [[ " ${seen_labels} " != *" ${label} "* ]] || fail "duplicate Claude label: ${label}"
  seen_labels="${seen_labels} ${label}"
  skill_targets+=("${root}/skills/agent-harness-profiles")
done

link_is_expected() {
  local target=$1 source=$2
  [[ -L "${target}" ]] || return 1
  [[ "$(readlink -f -- "${target}" 2>/dev/null || true)" == "${source}" ]]
}

# Refuse all divergent targets before creating any link.
for target in "${skill_targets[@]}"; do
  if [[ -e "${target}" || -L "${target}" ]]; then
    link_is_expected "${target}" "${SKILL_DIR}" || fail "refusing divergent target: ${target}"
  fi
done

command_target="${HOME}/bin/backup"
command_source="${REPO_ROOT}/backup.sh"
if (( install_command )) && [[ -e "${command_target}" || -L "${command_target}" ]]; then
  link_is_expected "${command_target}" "${command_source}" ||
    fail "refusing divergent command target: ${command_target}"
fi

for target in "${skill_targets[@]}"; do
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    install -d -m 0700 "$(dirname -- "${target}")"
    ln -s -- "${SKILL_DIR}" "${target}"
  fi
done

if (( install_command )); then
  install -d -m 0700 "$(dirname -- "${command_target}")"
  if [[ ! -e "${command_target}" && ! -L "${command_target}" ]]; then
    ln -s -- "${command_source}" "${command_target}"
  fi
fi

printf 'Installed shared skill links to %s\n' "${SKILL_DIR}"
(( install_command == 0 )) || printf 'Installed backup command: %s\n' "${command_target}"
