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

# Source directories
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# Additional profiles (isolated CLAUDE_CONFIG_DIR / CODEX_HOME spaces),
# space-separated "name:path" entries, e.g.
#   CLAUDE_PROFILES="work:$HOME/.claude-work personal:$HOME/.claude-personal"
# Each profile is backed up to ${CLAUDE_BACKUP_DIR}-{name} / ${CODEX_BACKUP_DIR}-{name}.
# The primary CLAUDE_HOME/CODEX_HOME is always backed up regardless.
CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"
CODEX_PROFILES="${CODEX_PROFILES:-}"
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
  [ -n "$CLAUDE_PROFILES" ] && log "  Claude profiles: $CLAUDE_PROFILES"
  [ -n "$CODEX_PROFILES" ]  && log "  Codex profiles:  $CODEX_PROFILES"
  
  backup_openclaw
  backup_claude
  backup_codex
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
