#!/bin/bash
# Syncthing Doctor - Comprehensive Syncthing health check
# License: MIT

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Global state
EXIT_CODE=0
WARNINGS=0
ERRORS=0

# Detected config path (shared across checks)
SYNCTHING_CONFIG=""

log_info() {
  echo -e "${BLUE}[INFO]${NC} $*"
}

log_ok() {
  echo -e "${GREEN}[✓]${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}[⚠]${NC} $*"
  WARNINGS=$((WARNINGS + 1))
}

log_error() {
  echo -e "${RED}[✗]${NC} $*"
  ERRORS=$((ERRORS + 1))
  EXIT_CODE=1
}

section() {
  echo ""
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BLUE}$*${NC}"
  echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

find_config() {
  if [ -f "$HOME/.local/state/syncthing/config.xml" ]; then
    SYNCTHING_CONFIG="$HOME/.local/state/syncthing/config.xml"
  elif [ -f "$HOME/.config/syncthing/config.xml" ]; then
    SYNCTHING_CONFIG="$HOME/.config/syncthing/config.xml"
  fi
}

# 1. Check if Syncthing is running
check_running() {
  section "1. Syncthing Process Status"

  local pids
  pids=$(pgrep -x syncthing 2>/dev/null || true)

  if [ -z "$pids" ]; then
    log_error "Syncthing is NOT running"
    return
  fi

  local count
  count=$(echo "$pids" | wc -l)
  log_ok "Syncthing is running ($count process(es))"

  echo "$pids" | while read -r pid; do
    local user cmd
    user=$(ps -o user= -p "$pid" 2>/dev/null || echo "?")
    cmd=$(ps -o args= -p "$pid" 2>/dev/null || echo "?")
    echo "  PID $pid (user: $user)"
    echo "    Command: $cmd"
  done

  if [ "$count" -gt 2 ]; then
    log_warn "Multiple Syncthing processes detected — may indicate duplicate instances"
  fi
}

# 2. Check service configuration
check_services() {
  section "2. Syncthing Service Configuration"

  local user_svc_running=false
  local user_svc_file=false
  local system_svc_running=false
  local system_svc_file=false
  local cron_found=false

  # User systemd service (running?)
  if systemctl --user is-active syncthing.service &>/dev/null; then
    user_svc_running=true
    log_ok "Systemd user service: active (running)"
    systemctl --user status syncthing.service --no-pager 2>/dev/null | head -10 | sed 's/^/    /'
  elif systemctl --user is-enabled syncthing.service &>/dev/null; then
    log_warn "Systemd user service: enabled but NOT running"
  fi

  # User service file exists?
  if [ -f "$HOME/.config/systemd/user/syncthing.service" ]; then
    user_svc_file=true
    if ! $user_svc_running; then
      log_info "User service file: $HOME/.config/systemd/user/syncthing.service"
    fi
  fi

  # System-level service (only check file existence, avoid sudo)
  for f in /etc/systemd/system/syncthing@.service /lib/systemd/system/syncthing@.service \
           /etc/systemd/system/syncthing.service /lib/systemd/system/syncthing.service; do
    if [ -f "$f" ]; then
      system_svc_file=true
      log_info "System service file exists: $f (apt package default, not active)"
      break
    fi
  done

  # Cron
  if crontab -l 2>/dev/null | grep -q syncthing; then
    cron_found=true
    log_warn "Syncthing found in crontab — may conflict with systemd service"
    crontab -l 2>/dev/null | grep syncthing | sed 's/^/    /'
  fi

  # Conflict detection
  local active_methods=0
  $user_svc_running && active_methods=$((active_methods + 1))
  $system_svc_running && active_methods=$((active_methods + 1))
  $cron_found && active_methods=$((active_methods + 1))

  if [ "$active_methods" -eq 0 ] && ! $user_svc_file && ! $system_svc_file; then
    log_warn "No service configuration found — Syncthing may be started manually"
  elif [ "$active_methods" -gt 1 ]; then
    log_warn "Multiple ACTIVE launch methods detected ($active_methods) — may cause conflicts"
  else
    log_ok "No conflicting launch methods"
  fi
}

# 3. Check for conflicting configurations
check_conflicts() {
  section "3. Configuration Conflict Detection"

  local configs=()

  [ -f "$HOME/.config/syncthing/config.xml" ] && configs+=("$HOME/.config/syncthing/config.xml")
  [ -f "$HOME/.local/state/syncthing/config.xml" ] && configs+=("$HOME/.local/state/syncthing/config.xml")
  [ -f "/var/syncthing/config.xml" ] && configs+=("/var/syncthing/config.xml")

  while IFS= read -r cfg; do
    local already=false
    for c in "${configs[@]}"; do
      [ "$c" = "$cfg" ] && already=true && break
    done
    $already || configs+=("$cfg")
  done < <(find "$HOME" -name "config.xml" -path "*/syncthing/*" -not -name "*.v*" 2>/dev/null)

  if [ "${#configs[@]}" -eq 0 ]; then
    log_error "No Syncthing config files found"
  elif [ "${#configs[@]}" -eq 1 ]; then
    log_ok "Single config file: ${configs[0]}"
  else
    log_warn "Multiple config files found (${#configs[@]}):"
    for cfg in "${configs[@]}"; do
      echo "    $cfg"
    done
  fi
}

# 4. Check backup folder configuration (unified architecture)
check_backup_folders() {
  section "4. Backup Folder Configuration"

  if [ -z "$SYNCTHING_CONFIG" ]; then
    log_error "Cannot find Syncthing config.xml"
    return
  fi

  log_info "Using config: $SYNCTHING_CONFIG"

  # Load backup config to find expected path
  local backup_config="$HOME/.config/backup/config"
  local expected_root=""
  if [ -f "$backup_config" ]; then
    log_ok "Backup config found: $backup_config"
    # shellcheck disable=SC1090
    expected_root=$(bash -c "source '$backup_config' && echo \"\$SYNCTHING_ROOT/backup\"" 2>/dev/null || true)
    if [ -n "$expected_root" ]; then
      log_info "Expected backup root: $expected_root"
    fi
  else
    log_warn "No backup config found at $backup_config"
  fi

  # Parse all folders from Syncthing config
  local folder_count=0
  local backup_folder_found=false

  while IFS='|' read -r fid flabel fpath ftype; do
    folder_count=$((folder_count + 1))
    local expanded="${fpath/#\~/$HOME}"

    echo "  📁 Folder: $flabel"
    echo "     ID:   $fid"
    echo "     Path: $fpath"
    echo "     Type: $ftype"

    # Count shared devices (exclude self)
    local dev_count
    dev_count=$(grep -A50 "id=\"$fid\"" "$SYNCTHING_CONFIG" | grep -c '<device id=' || true)
    dev_count=$((dev_count > 0 ? dev_count - 1 : 0))
    echo "     Shared with: $dev_count remote device(s)"

    if [ "$dev_count" -eq 0 ]; then
      log_warn "Folder '$flabel' is NOT shared with any remote device — data won't sync!"
    fi

    # Check if path exists
    if [ -d "$expanded" ]; then
      local size
      size=$(du -sh "$expanded" 2>/dev/null | cut -f1)
      echo "     Disk usage: $size"
    else
      log_warn "Folder path does not exist: $expanded"
    fi

    # Check if this is the backup folder
    if [ -n "$expected_root" ] && [ "$expanded" = "$expected_root" ]; then
      backup_folder_found=true
      log_ok "This is the unified backup folder ✓"
    fi

    echo ""
  done < <(grep -oP '<folder id="\K[^"]+(?="[^>]*label=")' "$SYNCTHING_CONFIG" | while read -r fid; do
    flabel=$(grep -oP "id=\"${fid}\"[^>]*label=\"\K[^\"]+" "$SYNCTHING_CONFIG")
    fpath=$(grep -oP "id=\"${fid}\"[^>]*path=\"\K[^\"]+" "$SYNCTHING_CONFIG")
    ftype=$(grep -oP "id=\"${fid}\"[^>]*type=\"\K[^\"]+" "$SYNCTHING_CONFIG")
    echo "${fid}|${flabel}|${fpath}|${ftype}"
  done)

  if [ "$folder_count" -eq 0 ]; then
    log_error "No folders configured in Syncthing"
  else
    log_info "Total folders: $folder_count"
  fi

  if [ -n "$expected_root" ] && ! $backup_folder_found; then
    log_error "Expected backup folder ($expected_root) not found in Syncthing config"
  fi
}

# 5. Check ignore patterns for machine isolation
check_ignore_patterns() {
  section "5. Machine Isolation Check"

  # Get machine ID from backup config or hostname
  local machine_id=""
  local backup_config="$HOME/.config/backup/config"
  if [ -f "$backup_config" ]; then
    machine_id=$(bash -c "source '$backup_config' && echo \"\$MACHINE_ID\"" 2>/dev/null || true)
  fi
  machine_id="${machine_id:-$(hostname -s)}"
  log_info "Machine ID: $machine_id"

  # Find all Syncthing-managed backup directories
  local backup_dirs=()
  if [ -f "$SYNCTHING_CONFIG" ]; then
    while IFS='|' read -r fpath flabel; do
      local expanded="${fpath/#\~/$HOME}"
      [ -d "$expanded" ] && backup_dirs+=("$expanded|$flabel")
    done < <(grep -oP '<folder [^>]*label="\K[^"]+' "$SYNCTHING_CONFIG" | while read -r flabel; do
      fpath=$(grep -oP "label=\"${flabel}\"[^>]*path=\"\K[^\"]+" "$SYNCTHING_CONFIG" || true)
      echo "${fpath}|${flabel}"
    done)
  fi

  if [ "${#backup_dirs[@]}" -eq 0 ]; then
    log_error "No Syncthing-managed directories found to check"
    return
  fi

  for entry in "${backup_dirs[@]}"; do
    local dir="${entry%%|*}"
    local label="${entry##*|}"
    echo ""
    log_info "Checking folder '$label' → $dir"

    local stignore="$dir/.stignore"
    if [ ! -f "$stignore" ]; then
      log_warn "No .stignore — ALL data will sync (no machine isolation)"
      echo "    Recommended .stignore:"
      echo "      !$machine_id"
      echo "      !$machine_id/**"
      echo "      *"
      continue
    fi

    log_ok ".stignore exists"
    echo "    Contents:"
    sed 's/^/      /' "$stignore"

    # Check machine whitelist
    if grep -qE "^!${machine_id}$|^!${machine_id}/\*\*$" "$stignore"; then
      log_ok "Machine '$machine_id' is whitelisted"
    else
      log_warn "Machine '$machine_id' NOT explicitly whitelisted in .stignore"
    fi

    # Count machine subdirectories
    if [ -d "$dir" ]; then
      local subdirs
      subdirs=$(find "$dir" -maxdepth 1 -mindepth 1 -type d ! -name ".st*" 2>/dev/null || true)
      local total
      total=$(echo "$subdirs" | grep -c . || true)
      echo "    Machine directories: $total"
      if [ -n "$subdirs" ]; then
        echo "$subdirs" | while read -r sd; do
          local name size
          name=$(basename "$sd")
          size=$(du -sh "$sd" 2>/dev/null | cut -f1)
          local marker=""
          [ "$name" = "$machine_id" ] && marker=" ← this machine"
          echo "      📂 $name ($size)$marker"
        done
      fi
    fi
  done
}

# Summary
print_summary() {
  section "Summary"

  echo ""
  if [ "$ERRORS" -eq 0 ] && [ "$WARNINGS" -eq 0 ]; then
    log_ok "All checks passed! Syncthing is healthy."
  elif [ "$ERRORS" -eq 0 ]; then
    log_warn "Checks completed with $WARNINGS warning(s)"
  else
    log_error "Checks completed with $ERRORS error(s) and $WARNINGS warning(s)"
  fi

  echo ""
}

# Main
main() {
  echo ""
  echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BLUE}║              Syncthing Doctor v2.0                              ║${NC}"
  echo -e "${BLUE}║          Comprehensive Syncthing Health Check                   ║${NC}"
  echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════╝${NC}"

  find_config
  check_running
  check_services
  check_conflicts
  check_backup_folders
  check_ignore_patterns
  print_summary

  exit $EXIT_CODE
}

main "$@"
