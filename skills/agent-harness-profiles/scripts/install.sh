#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
launcher_file="${HOME}/.config/agent-harness-profiles/launchers.sh"

usage() {
  printf 'usage: %s [--config FILE] [--launchers FILE]\n' "${0##*/}" >&2
}

while (( $# > 0 )); do
  case "$1" in
    --config)
      (( $# >= 2 )) || { usage; exit 2; }
      config_file=$2
      shift 2
      ;;
    --launchers)
      (( $# >= 2 )) || { usage; exit 2; }
      launcher_file=$2
      shift 2
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

command -v rsync >/dev/null 2>&1 || {
  printf 'setup installation failed: required command is unavailable: rsync\n' >&2
  exit 1
}
for script in "${SCRIPT_DIR}"/*.sh; do
  bash -n "${script}" || {
    printf 'setup installation failed: invalid script syntax: %s\n' "${script##*/}" >&2
    exit 1
  }
done

# Finish every deterministic validation before the first persistent write.
"${SCRIPT_DIR}/install-links.sh" --config "${config_file}" --check
"${SCRIPT_DIR}/render-launchers.sh" \
  --config "${config_file}" --output "${launcher_file}" --check
"${SCRIPT_DIR}/prepare-opencode-roots.sh" --config "${config_file}" --check

"${SCRIPT_DIR}/render-launchers.sh" --config "${config_file}" --output "${launcher_file}"
"${SCRIPT_DIR}/prepare-opencode-roots.sh" --config "${config_file}"
"${SCRIPT_DIR}/install-links.sh" --config "${config_file}"
"${SCRIPT_DIR}/doctor.sh" --config "${config_file}"

printf 'Setup installed. Review and source: %s\n' "${launcher_file}"
