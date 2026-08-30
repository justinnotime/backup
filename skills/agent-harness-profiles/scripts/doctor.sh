#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly REPO_ROOT="$(cd -- "${SKILL_DIR}/../.." && pwd -P)"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
errors=0
warnings=0

usage() {
  printf 'usage: %s [--config FILE]\n' "${0##*/}" >&2
}

if (( $# > 0 )); then
  [[ $# -eq 2 && $1 == --config ]] || { usage; exit 2; }
  config_file=$2
fi

ok() { printf 'ok: %s\n' "$1"; }
warn() { printf 'warning: %s\n' "$1" >&2; warnings=$((warnings + 1)); }
error() { printf 'error: %s\n' "$1" >&2; errors=$((errors + 1)); }

if (( BASH_VERSINFO[0] >= 4 )); then
  ok 'Bash 4+'
else
  error 'Bash 4+ is required'
fi
for required in rsync; do
  if command -v "${required}" >/dev/null 2>&1; then
    ok "command available: ${required}"
  else
    error "required command unavailable: ${required}"
  fi
done
command -v sqlite3 >/dev/null 2>&1 || warn 'sqlite3 is unavailable; OpenCode Backup uses its fallback copy path'

if [[ -x "${REPO_ROOT}/backup.sh" ]]; then
  ok 'stable Backup entrypoint'
else
  error 'stable Backup entrypoint is unavailable'
fi
for script in "${SCRIPT_DIR}"/*.sh; do
  if bash -n "${script}"; then
    ok "script syntax: ${script##*/}"
  else
    error "script syntax: ${script##*/}"
  fi
done

if [[ -f "${config_file}" ]]; then
  if bash -n "${config_file}"; then
    ok 'configuration syntax'
  else
    error 'configuration syntax'
  fi
  if "${SCRIPT_DIR}/render-launchers.sh" --config "${config_file}" >/dev/null; then
    ok 'configured profile entries'
  else
    error 'configured profile entries'
  fi
else
  error "configuration not found: ${config_file}"
fi

shared_target="${HOME}/.agents/skills/agent-harness-profiles"
if [[ -L "${shared_target}" && "$(readlink -f -- "${shared_target}" 2>/dev/null || true)" == "${SKILL_DIR}" ]]; then
  ok 'shared Skill link'
else
  warn 'shared Skill link is not installed from this checkout'
fi

backup_target="${HOME}/bin/backup"
if [[ -L "${backup_target}" && "$(readlink -f -- "${backup_target}" 2>/dev/null || true)" == "$(readlink -f -- "${REPO_ROOT}/backup.sh")" ]]; then
  ok 'stable Backup command link'
elif [[ -e "${backup_target}" || -L "${backup_target}" ]]; then
  error "divergent Backup command target: ${backup_target}"
else
  warn 'stable Backup command link is not installed'
fi

printf 'Doctor summary: %d error(s), %d warning(s)\n' "${errors}" "${warnings}"
(( errors == 0 ))
