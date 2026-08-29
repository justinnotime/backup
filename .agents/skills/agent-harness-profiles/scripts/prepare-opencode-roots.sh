#!/usr/bin/env bash
set -euo pipefail

config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"

usage() {
  printf 'usage: %s [--config FILE]\n' "${0##*/}" >&2
}

fail() {
  printf 'OpenCode root preparation failed: %s\n' "$1" >&2
  exit 1
}

if (( $# > 0 )); then
  [[ $# -eq 2 && $1 == --config ]] || { usage; exit 2; }
  config_file=$2
fi

[[ -f "${config_file}" ]] || fail "configuration file not found: ${config_file}"
# shellcheck disable=SC1090
source "${config_file}"
OPENCODE_PROFILES="${OPENCODE_PROFILES:-}"

roots=()
labels=()
seen=""
for entry in ${OPENCODE_PROFILES}; do
  label=${entry%%:*}
  root=${entry#*:}
  [[ -n "${label}" && "${label}" != "${entry}" && -n "${root}" ]] ||
    fail "malformed OPENCODE_PROFILES entry: ${entry}"
  [[ "${label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "unsafe label: ${label}"
  [[ "${root}" == /* && "${root}" != / && "${root}" != "${HOME}" ]] ||
    fail "root must be an absolute non-home path for label ${label}"
  [[ " ${seen} " != *" ${label} "* ]] || fail "duplicate label: ${label}"
  seen="${seen} ${label}"
  labels+=("${label}")
  roots+=("${root}")
done

shopt -s nullglob
config_sources=("${HOME}/.config"/*)
shopt -u nullglob

# Preflight every existing target before creating anything.
for root in "${roots[@]}"; do
  for source_path in "${config_sources[@]}"; do
    base=${source_path##*/}
    [[ "${base}" != opencode ]] || continue
    target_path="${root}/config/${base}"
    if [[ -e "${target_path}" || -L "${target_path}" ]]; then
      [[ -L "${target_path}" ]] || fail "refusing divergent target: ${target_path}"
      [[ "$(readlink -f -- "${target_path}" 2>/dev/null || true)" == "$(readlink -f -- "${source_path}")" ]] ||
        fail "refusing divergent symlink: ${target_path}"
    fi
  done
done

for index in "${!roots[@]}"; do
  root=${roots[$index]}
  label=${labels[$index]}
  install -d -m 0700 "${root}/share/opencode" "${root}/state/opencode" "${root}/config/opencode"
  for source_path in "${config_sources[@]}"; do
    base=${source_path##*/}
    [[ "${base}" != opencode ]] || continue
    target_path="${root}/config/${base}"
    if [[ ! -e "${target_path}" && ! -L "${target_path}" ]]; then
      ln -s -- "${source_path}" "${target_path}"
    fi
  done
  if [[ ! -e "${root}/config/opencode/opencode.json" ]]; then
    printf '%s\n' '{"$schema":"https://opencode.ai/config.json"}' > "${root}/config/opencode/opencode.json"
    chmod 0600 "${root}/config/opencode/opencode.json"
  fi
  printf 'Prepared OpenCode root for label %s\n' "${label}"
done
