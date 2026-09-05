#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly ORIGINAL_HOME="${HOME}"
readonly PROFILE_INSTALL_HOME="${ORIGINAL_HOME}"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
check_only=false

usage() {
  printf 'usage: %s [--config FILE] [--check]\n' "${0##*/}" >&2
}

fail() {
  printf 'link installation failed: %s\n' "$1" >&2
  exit 1
}

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/profile-paths.sh"

while (( $# > 0 )); do
  case "$1" in
    --config)
      (( $# >= 2 )) || { usage; exit 2; }
      config_file=$2
      shift 2
      ;;
    --check)
      check_only=true
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

profile_require_stable_checkout "${SKILL_DIR}"
[[ -f "${config_file}" ]] || fail "configuration file not found: ${config_file}"
bash -n "${config_file}" || fail 'configuration syntax is invalid'
# shellcheck disable=SC1090
source "${config_file}"
[[ "${HOME}" == "${ORIGINAL_HOME}" ]] || fail 'configuration must not change HOME'
CLAUDE_HOME="${CLAUDE_HOME:-${PROFILE_INSTALL_HOME}/.claude}"
CODEX_HOME="${CODEX_HOME:-${PROFILE_INSTALL_HOME}/.codex}"
DSH_HOME="${DSH_HOME:-${PROFILE_INSTALL_HOME}/.dsh}"
OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-${XDG_DATA_HOME:-${PROFILE_INSTALL_HOME}/.local/share}/opencode}"
OPENCODE_CONFIG_SRC="${OPENCODE_CONFIG_SRC:-${XDG_CONFIG_HOME:-${PROFILE_INSTALL_HOME}/.config}/opencode}"
OPENCODE_STATE_DIR="${OPENCODE_STATE_DIR:-${XDG_STATE_HOME:-${PROFILE_INSTALL_HOME}/.local/state}/opencode}"
CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"

profile_reset_root_registry
profile_reserve_path 'Skill checkout' "$(git -C "${SKILL_DIR}" rev-parse --show-toplevel 2>/dev/null || printf '%s' "${SKILL_DIR}")"
profile_reserve_fixed_install_roots
profile_reserve_native_roots claude
profile_reserve_path 'profile configuration' "${config_file}"
profile_validate_root claude default "${CLAUDE_HOME}"
default_claude_root=${PROFILE_VALIDATED_ROOT}
profile_validate_managed_directory "${default_claude_root}" "${CLAUDE_HOME}/skills" \
  'native Claude Skill directory'
profile_validate_contained_path "${PROFILE_INSTALL_HOME}" \
  "${PROFILE_INSTALL_HOME}/.agents/skills" 'shared Skill link parent'
profile_validate_contained_path "${PROFILE_INSTALL_HOME}" \
  "${PROFILE_INSTALL_HOME}/bin" 'stable Backup command link parent'

targets=(
  "${PROFILE_INSTALL_HOME}/.agents/skills/agent-harness-profiles"
  "${CLAUDE_HOME}/skills/agent-harness-profiles"
)
sources=("${SKILL_DIR}" "${SKILL_DIR}")
for entry in ${CLAUDE_PROFILES}; do
  label=${entry%%:*}
  root=${entry#*:}
  [[ -n "${label}" && "${label}" != "${entry}" && -n "${root}" ]] ||
    fail "malformed CLAUDE_PROFILES entry: ${entry}"
  profile_validate_root claude "${label}" "${root}"
  profile_validate_managed_directory "${PROFILE_VALIDATED_ROOT}" "${root}/skills" \
    "Claude Skill directory for label ${label}"
  targets+=("${root}/skills/agent-harness-profiles")
  sources+=("${SKILL_DIR}")
done

profile_validate_backup_command
if [[ -n "${BACKUP_COMMAND:-}" ]]; then
  targets+=("${PROFILE_INSTALL_HOME}/bin/backup")
  sources+=("${BACKUP_COMMAND}")
fi

for index in "${!targets[@]}"; do
  target=${targets[$index]}
  source_path=${sources[$index]}
  if [[ -e "${target}" || -L "${target}" ]]; then
    [[ -L "${target}" ]] || fail "refusing non-symlink target: ${target}"
    [[ "$(readlink -f -- "${target}" 2>/dev/null || true)" == "$(readlink -f -- "${source_path}")" ]] ||
      fail "refusing divergent symlink: ${target}"
  else
    profile_validate_writable_parent "${target}" "link target ${target}"
  fi
done

[[ "${check_only}" == false ]] || exit 0

for index in "${!targets[@]}"; do
  target=${targets[$index]}
  source_path=${sources[$index]}
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    install -d -m 0700 "$(dirname -- "${target}")"
    ln -s -- "${source_path}" "${target}"
  fi
done

printf 'Installed Agent Harness Profiles links.\n'
