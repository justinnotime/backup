#!/bin/bash
#
# Universal Backup Script
# Supports unified backup structure for multiple AI tools
#
set -euo pipefail

# Load config first (so it can override defaults)
CONFIG_FILE="$HOME/.config/backup/config"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

# Configuration defaults (only used if not set by config)
MACHINE_ID="${MACHINE_ID:-$(hostname)}"
SYNCTHING_ROOT="${SYNCTHING_ROOT:-$HOME/syncthing}"

# Unified backup structure: ~/syncthing/backup/{machine-id}/{tool}/
BACKUP_ROOT="${BACKUP_ROOT:-$SYNCTHING_ROOT/backup/$MACHINE_ID}"
OPENCLAW_BACKUP_DIR="${OPENCLAW_BACKUP_DIR:-$BACKUP_ROOT/openclaw}"
CLAUDE_BACKUP_DIR="${CLAUDE_BACKUP_DIR:-$BACKUP_ROOT/claude}"
CODEX_BACKUP_DIR="${CODEX_BACKUP_DIR:-$BACKUP_ROOT/codex}"
CURSOR_BACKUP_DIR="${CURSOR_BACKUP_DIR:-$BACKUP_ROOT/cursor}"
OPENCODE_BACKUP_DIR="${OPENCODE_BACKUP_DIR:-$BACKUP_ROOT/opencode}"

# Source directories
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# opencode uses XDG dirs, not a single home (see PROFILES.md)
OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/opencode}"
OPENCODE_CONFIG_SRC="${OPENCODE_CONFIG_SRC:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}"
OPENCODE_STATE_DIR="${OPENCODE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/opencode}"

# Additional profiles (isolated CLAUDE_CONFIG_DIR / CODEX_HOME spaces),
# space-separated "name:path" entries, e.g.
#   CLAUDE_PROFILES="work:$HOME/.claude-work personal:$HOME/.claude-personal"
# Each profile is backed up to ${CLAUDE_BACKUP_DIR}-{name} / ${CODEX_BACKUP_DIR}-{name}.
# The primary CLAUDE_HOME/CODEX_HOME is always backed up regardless.
# For opencode the path is a profile ROOT containing share/config/state
# subdirs (the wrapper sets XDG_DATA_HOME=$root/share, XDG_CONFIG_HOME=$root/config,
# XDG_STATE_HOME=$root/state, so opencode config lives at $root/config/opencode —
# see PROFILES.md).
CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"
CODEX_PROFILES="${CODEX_PROFILES:-}"
OPENCODE_PROFILES="${OPENCODE_PROFILES:-}"
CURSOR_HOME="${CURSOR_HOME:-$HOME/.cursor}"
# Cursor IDE user dir (Linux default; macOS: $HOME/Library/Application Support/Cursor/User)
CURSOR_USER_DIR="${CURSOR_USER_DIR:-$HOME/.config/Cursor/User}"

# Log
BACKUP_LOG="${BACKUP_LOG:-$HOME/.local/log/backup.log}"

# Initialize log
mkdir -p "$(dirname "$BACKUP_LOG")"
log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$BACKUP_LOG"
}

# Track what was backed up
BACKED_UP_TOOLS=()

# ============================================================================
# OpenClaw Backup
# ============================================================================
backup_openclaw() {
  log "=== OpenClaw Backup ==="
  
  local sessions_src="$OPENCLAW_HOME/agents/main/sessions"
  local sessions_dst="$OPENCLAW_BACKUP_DIR/sessions"
  if [ -d "$sessions_src" ]; then
    mkdir -p "$sessions_dst"
    rsync -a --update "$sessions_src/" "$sessions_dst/"
    local count=$(find "$sessions_src" -name "*.jsonl" 2>/dev/null | wc -l)
    log "  Sessions: $count files → $sessions_dst"
  else
    log "  Sessions: source not found ($sessions_src)"
  fi

  # Memory (markdown files)
  local memory_md_src="$OPENCLAW_HOME/workspace/memory"
  local memory_md_dst="$OPENCLAW_BACKUP_DIR/memory-md"
  if [ -d "$memory_md_src" ]; then
    mkdir -p "$memory_md_dst"
    rsync -a --update --include="*.md" --exclude="*" "$memory_md_src/" "$memory_md_dst/"
    local count=$(find "$memory_md_src" -name "*.md" 2>/dev/null | wc -l)
    log "  Memory MD: $count files → $memory_md_dst"
  else
    log "  Memory MD: source not found"
  fi

  # Memory database
  local memory_db_src="$OPENCLAW_HOME/memory/main.sqlite"
  local memory_db_dst="$OPENCLAW_BACKUP_DIR/memory-db"
  if [ -f "$memory_db_src" ]; then
    mkdir -p "$memory_db_dst"
    rsync -a --update "$memory_db_src" "$memory_db_dst/"
    local size=$(du -h "$memory_db_src" | cut -f1)
    log "  Memory DB: $size → $memory_db_dst/main.sqlite"
  else
    log "  Memory DB: source not found"
  fi

  # Workspace config files
  local workspace_src="$OPENCLAW_HOME/workspace"
  local workspace_dst="$OPENCLAW_BACKUP_DIR/workspace-config"
  if [ -d "$workspace_src" ]; then
    mkdir -p "$workspace_dst"
    for file in SOUL.md IDENTITY.md USER.md TOOLS.md AGENTS.md; do
      if [ -f "$workspace_src/$file" ]; then
        rsync -a --update "$workspace_src/$file" "$workspace_dst/"
      fi
    done
    local count=$(ls -1 "$workspace_dst" 2>/dev/null | wc -l)
    log "  Workspace config: $count files → $workspace_dst"
  else
    log "  Workspace config: source not found"
  fi

  # OpenClaw config
  local config_src="$OPENCLAW_HOME/openclaw.json"
  local config_dst="$OPENCLAW_BACKUP_DIR/config"
  if [ -f "$config_src" ]; then
    mkdir -p "$config_dst"
    rsync -a --update "$config_src" "$config_dst/"
    log "  Config: openclaw.json → $config_dst"
  fi

  if [ -d "$sessions_src" ] || [ -f "$memory_db_src" ]; then
    log "  OpenClaw backup completed"
    BACKED_UP_TOOLS+=("OpenClaw")
  else
    log "  OpenClaw not installed (skipped)"
  fi
}

# ============================================================================
# Claude Code Backup
# ============================================================================
backup_claude_dir() {
  local label="$1" src_home="$2" dst_root="$3"
  log "=== Claude Code Backup ($label) ==="

  # Projects (main backup target)
  local projects_src="$src_home/projects"
  local projects_dst="$dst_root/projects"
  if [ -d "$projects_src" ]; then
    mkdir -p "$projects_dst"
    rsync -a --update "$projects_src/" "$projects_dst/"
    local size=$(du -sh "$projects_src" 2>/dev/null | cut -f1)
    log "  Projects: $size → $projects_dst"
  else
    log "  Projects: source not found ($projects_src)"
  fi

  # History
  local history_src="$src_home/history.jsonl"
  local history_dst="$dst_root/history"
  if [ -f "$history_src" ]; then
    mkdir -p "$history_dst"
    rsync -a --update "$history_src" "$history_dst/"
    local size=$(du -h "$history_src" | cut -f1)
    log "  History: $size → $history_dst/history.jsonl"
  else
    log "  History: source not found"
  fi

  # Settings
  local settings_src="$src_home/settings.json"
  local settings_dst="$dst_root/config"
  if [ -f "$settings_src" ]; then
    mkdir -p "$settings_dst"
    rsync -a --update "$settings_src" "$settings_dst/"
    log "  Settings: settings.json → $settings_dst"
  fi

  if [ -d "$projects_src" ]; then
    log "  Claude Code ($label) backup completed"
    BACKED_UP_TOOLS+=("Claude Code ($label)")
  else
    log "  Claude Code ($label) not installed (skipped)"
  fi
}

backup_claude() {
  backup_claude_dir "default" "$CLAUDE_HOME" "$CLAUDE_BACKUP_DIR"

  local entry name path
  for entry in $CLAUDE_PROFILES; do
    name="${entry%%:*}"
    path="${entry#*:}"
    if [ -z "$name" ] || [ "$name" = "$entry" ]; then
      log "  ⚠ Skipping malformed CLAUDE_PROFILES entry: '$entry' (expected name:path)"
      continue
    fi
    backup_claude_dir "$name" "$path" "${CLAUDE_BACKUP_DIR}-${name}"
  done
}

# ============================================================================
# Codex Backup
# ============================================================================
backup_codex_dir() {
  local label="$1" src_home="$2" dst_root="$3"
  log "=== Codex Backup ($label) ==="

  local sessions_src="$src_home/sessions"
  local sessions_dst="$dst_root/sessions"
  if [ -d "$sessions_src" ]; then
    mkdir -p "$sessions_dst"
    rsync -a --update "$sessions_src/" "$sessions_dst/"
    local count=$(find "$sessions_src" -name "*.jsonl" 2>/dev/null | wc -l)
    log "  Sessions: $count files → $sessions_dst"
  else
    log "  Sessions: source not found ($sessions_src)"
  fi

  local history_src="$src_home/history.jsonl"
  local history_dst="$dst_root/history"
  if [ -f "$history_src" ]; then
    mkdir -p "$history_dst"
    rsync -a --update "$history_src" "$history_dst/"
    local size=$(du -h "$history_src" | cut -f1)
    log "  History: $size → $history_dst/history.jsonl"
  else
    log "  History: source not found"
  fi

  local config_src="$src_home/config.toml"
  local config_dst="$dst_root/config"
  if [ -f "$config_src" ]; then
    mkdir -p "$config_dst"
    rsync -a --update "$config_src" "$config_dst/"
    log "  Config: config.toml → $config_dst"
  fi

  if [ -d "$sessions_src" ] || [ -f "$history_src" ]; then
    log "  Codex ($label) backup completed"
    BACKED_UP_TOOLS+=("Codex ($label)")
  else
    log "  Codex ($label) not installed (skipped)"
  fi
}

backup_codex() {
  backup_codex_dir "default" "$CODEX_HOME" "$CODEX_BACKUP_DIR"

  local entry name path
  for entry in $CODEX_PROFILES; do
    name="${entry%%:*}"
    path="${entry#*:}"
    if [ -z "$name" ] || [ "$name" = "$entry" ]; then
      log "  ⚠ Skipping malformed CODEX_PROFILES entry: '$entry' (expected name:path)"
      continue
    fi
    backup_codex_dir "$name" "$path" "${CODEX_BACKUP_DIR}-${name}"
  done
}

# ============================================================================
# opencode Backup
# ============================================================================
backup_opencode_dir() {
  local label="$1" data_dir="$2" config_dir="$3" state_dir="$4" dst_root="$5"
  log "=== opencode Backup ($label) ==="

  # Sessions DB (SQLite WAL) — consistent snapshot via sqlite3 .backup
  local db_dst="$dst_root/db"
  local db_found=0
  local db
  for db in "$data_dir"/*.db; do
    [ -f "$db" ] || continue
    db_found=1
    mkdir -p "$db_dst"
    if command -v sqlite3 >/dev/null 2>&1; then
      if sqlite3 "$db" ".backup '$db_dst/$(basename "$db")'"; then
        log "  DB: $(basename "$db") ($(du -h "$db" | cut -f1)) → $db_dst (sqlite3 .backup)"
      else
        log "  ⚠ DB: sqlite3 .backup failed for $db"
      fi
    else
      rsync -a "$db" "$db_dst/"
      [ -f "${db}-wal" ] && rsync -a "${db}-wal" "${db}-shm" "$db_dst/"
      log "  DB: $(basename "$db") → $db_dst (rsync; sqlite3 not installed)"
    fi
  done
  [ "$db_found" -eq 1 ] || log "  DB: none found in $data_dir"

  # Legacy JSON session storage (pre-SQLite layouts, still present on old installs)
  local legacy
  for legacy in storage project; do
    if [ -d "$data_dir/$legacy" ]; then
      mkdir -p "$dst_root/$legacy"
      rsync -a --update "$data_dir/$legacy/" "$dst_root/$legacy/"
      log "  Legacy $legacy/: → $dst_root/$legacy"
    fi
  done

  # Config (opencode.json, agents/, commands/, themes/; plugin node_modules excluded)
  if [ -d "$config_dir" ]; then
    mkdir -p "$dst_root/config"
    rsync -a --update --exclude="node_modules" "$config_dir/" "$dst_root/config/"
    log "  Config: $config_dir → $dst_root/config"
  else
    log "  Config: source not found ($config_dir)"
  fi

  # Prompt history
  local hist="$state_dir/prompt-history.jsonl"
  if [ -f "$hist" ]; then
    mkdir -p "$dst_root/history"
    rsync -a --update "$hist" "$dst_root/history/"
    log "  History: $(du -h "$hist" | cut -f1) → $dst_root/history/prompt-history.jsonl"
  fi

  # auth.json / mcp-auth.json intentionally NOT backed up (credentials)

  if [ "$db_found" -eq 1 ] || [ -d "$config_dir" ]; then
    log "  opencode ($label) backup completed"
    BACKED_UP_TOOLS+=("opencode ($label)")
  else
    log "  opencode ($label) not installed (skipped)"
  fi
}

backup_opencode() {
  backup_opencode_dir "default" "$OPENCODE_DATA_DIR" "$OPENCODE_CONFIG_SRC" "$OPENCODE_STATE_DIR" "$OPENCODE_BACKUP_DIR"

  local entry name root
  for entry in $OPENCODE_PROFILES; do
    name="${entry%%:*}"
    root="${entry#*:}"
    if [ -z "$name" ] || [ "$name" = "$entry" ]; then
      log "  ⚠ Skipping malformed OPENCODE_PROFILES entry: '$entry' (expected name:root)"
      continue
    fi
    backup_opencode_dir "$name" "$root/share/opencode" "$root/config/opencode" "$root/state/opencode" "${OPENCODE_BACKUP_DIR}-${name}"
  done
}

# ============================================================================
# Cursor Backup
# ============================================================================
backup_cursor() {
  log "=== Cursor Backup ==="

  # Agent transcripts + per-project session state (terminals, canvases, ...)
  local projects_src="$CURSOR_HOME/projects"
  local projects_dst="$CURSOR_BACKUP_DIR/projects"
  if [ -d "$projects_src" ]; then
    mkdir -p "$projects_dst"
    rsync -a --update --exclude="node_modules" "$projects_src/" "$projects_dst/"
    local count=$(find "$projects_src" -path "*/agent-transcripts/*" -name "*.jsonl" 2>/dev/null | wc -l)
    log "  Projects: $count transcript files → $projects_dst"
  else
    log "  Projects: source not found ($projects_src)"
  fi

  # Settings
  local settings_src="$CURSOR_USER_DIR/settings.json"
  local settings_dst="$CURSOR_BACKUP_DIR/config"
  if [ -f "$settings_src" ]; then
    mkdir -p "$settings_dst"
    rsync -a --update "$settings_src" "$settings_dst/"
    log "  Settings: settings.json → $settings_dst"
  fi

  if [ -d "$projects_src" ]; then
    log "  Cursor backup completed"
    BACKED_UP_TOOLS+=("Cursor")
  else
    log "  Cursor not installed (skipped)"
  fi
}

# ============================================================================
# Main
# ============================================================================
main() {
  log "Starting backup for machine: $MACHINE_ID"
  log "Backup targets:"
  log "  OpenClaw → $OPENCLAW_BACKUP_DIR"
  log "  Claude   → $CLAUDE_BACKUP_DIR"
  log "  Codex    → $CODEX_BACKUP_DIR"
  log "  Cursor   → $CURSOR_BACKUP_DIR"
  log "  opencode → $OPENCODE_BACKUP_DIR"
  [ -n "$CLAUDE_PROFILES" ]   && log "  Claude profiles:   $CLAUDE_PROFILES"
  [ -n "$CODEX_PROFILES" ]    && log "  Codex profiles:    $CODEX_PROFILES"
  [ -n "$OPENCODE_PROFILES" ] && log "  opencode profiles: $OPENCODE_PROFILES"
  
  backup_openclaw
  backup_claude
  backup_codex
  backup_opencode
  backup_cursor
  
  log ""
  if [ ${#BACKED_UP_TOOLS[@]} -eq 0 ]; then
    log "⚠ No tools found to backup"
    exit 1
  else
    local tools_list=$(IFS=" "; echo "${BACKED_UP_TOOLS[*]}")
    log "✓ Backup complete! ($tools_list)"
  fi
}

main "$@"
