#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly ORIGINAL_HOME="${HOME}"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
check_only=false

usage() {
  printf 'usage: %s [--config FILE] [--check]\n' "${0##*/}" >&2
}

fail() {
  printf 'OpenCode root preparation failed: %s\n' "$1" >&2
  exit 1
}

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

[[ -f "${config_file}" ]] || fail "configuration file not found: ${config_file}"
bash -n "${config_file}" || fail 'configuration syntax is invalid'
# shellcheck disable=SC1090
source "${config_file}"
[[ "${HOME}" == "${ORIGINAL_HOME}" ]] || fail 'configuration must not change HOME'
readonly PROFILE_INSTALL_HOME="${ORIGINAL_HOME}"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/profile-paths.sh"

OPENCODE_PROFILES="${OPENCODE_PROFILES:-}"
CLAUDE_HOME="${CLAUDE_HOME:-${PROFILE_INSTALL_HOME}/.claude}"
CODEX_HOME="${CODEX_HOME:-${PROFILE_INSTALL_HOME}/.codex}"
DSH_HOME="${DSH_HOME:-${PROFILE_INSTALL_HOME}/.dsh}"
OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-${XDG_DATA_HOME:-${PROFILE_INSTALL_HOME}/.local/share}/opencode}"
OPENCODE_CONFIG_SRC="${OPENCODE_CONFIG_SRC:-${XDG_CONFIG_HOME:-${PROFILE_INSTALL_HOME}/.config}/opencode}"
OPENCODE_STATE_DIR="${OPENCODE_STATE_DIR:-${XDG_STATE_HOME:-${PROFILE_INSTALL_HOME}/.local/state}/opencode}"
roots=()
labels=()
profile_reset_root_registry
profile_reserve_path 'Skill checkout' "$(git -C "${SKILL_DIR}" rev-parse --show-toplevel 2>/dev/null || printf '%s' "${SKILL_DIR}")"
profile_reserve_fixed_install_roots
profile_reserve_native_roots
profile_reserve_path 'profile configuration' "${config_file}"

for entry in ${OPENCODE_PROFILES}; do
  label=${entry%%:*}
  root=${entry#*:}
  [[ -n "${label}" && "${label}" != "${entry}" && -n "${root}" ]] ||
    fail "malformed OPENCODE_PROFILES entry: ${entry}"
  profile_validate_root opencode "${label}" "${root}"
  resolved_root=${PROFILE_VALIDATED_ROOT}
  profile_validate_managed_directory "${resolved_root}" "${root}/share" \
    "OpenCode share root for label ${label}"
  profile_validate_managed_directory "${resolved_root}" "${root}/share/opencode" \
    "OpenCode data directory for label ${label}"
  profile_validate_managed_directory "${resolved_root}" "${root}/state" \
    "OpenCode state root for label ${label}"
  profile_validate_managed_directory "${resolved_root}" "${root}/state/opencode" \
    "OpenCode state directory for label ${label}"
  profile_validate_managed_directory "${resolved_root}" "${root}/config" \
    "OpenCode config root for label ${label}"
  profile_validate_managed_directory "${resolved_root}" "${root}/config/opencode" \
    "OpenCode config directory for label ${label}"
  profile_validate_managed_file "${resolved_root}" "${root}/config/opencode/opencode.json" \
    "OpenCode config file for label ${label}"
  labels+=("${label}")
  roots+=("${root}")
done

[[ "${check_only}" == false ]] || exit 0

create_directory_if_missing() {
  local directory=$1
  [[ -d "${directory}" ]] || install -d -m 0700 "${directory}"
}

for index in "${!roots[@]}"; do
  root=${roots[$index]}
  label=${labels[$index]}
  for directory in \
    "${root}" \
    "${root}/share" "${root}/share/opencode" \
    "${root}/state" "${root}/state/opencode" \
    "${root}/config" "${root}/config/opencode"; do
    create_directory_if_missing "${directory}"
  done
  if [[ ! -e "${root}/config/opencode/opencode.json" ]]; then
    printf '%s\n' '{"$schema":"https://opencode.ai/config.json"}' \
      > "${root}/config/opencode/opencode.json"
    chmod 0600 "${root}/config/opencode/opencode.json"
  fi
  printf 'Prepared OpenCode root for label %s\n' "${label}"
done
