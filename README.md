# Universal Backup System for AI Development Tools

Unified backup solution for OpenClaw, Claude Code, and future AI tools (Codex, Cursor, etc.) with Syncthing P2P synchronization.

## Architecture

```
~/syncthing/backup/{machine-id}/
├── openclaw/          # OpenClaw sessions, memory, config
├── claude/            # Claude Code projects, history (default profile)
├── claude-work/       # Claude Code extra profile (optional, see Multiple Profiles)
├── codex/             # Codex sessions, history, config (default profile)
├── codex-work/        # Codex extra profile (optional)
├── opencode/          # opencode sessions DB, config, prompt history (default profile)
├── opencode-work/     # opencode extra profile (optional)
└── cursor/            # Cursor agent transcripts, chat DBs, settings
```

- Single Syncthing folder for all AI tools
- Add new tools without reconfiguring Syncthing
- Machine-isolated via `.stignore`
- P2P sync across devices

## Files

| File | Description |
|------|-------------|
| `backup.sh` | Parameterized incremental backup script |
| `syncthing-doctor.sh` | Comprehensive Syncthing health check (v2.0) |
| `PROFILES.md` | Multi-profile (work/personal) setup guide for Claude Code & Codex |
| `~/.config/backup/config` | Per-machine configuration (not in repo) |

## Quick Start

```bash
# 1. Clone
git clone https://github.com/justinnotime/backup.git ~/src/backup

# 2. Symlink
mkdir -p ~/bin
ln -s ~/src/backup/backup.sh ~/bin/backup

# 3. Configure
mkdir -p ~/.config/backup
cat > ~/.config/backup/config << 'EOF'
MACHINE_ID="my-machine-name"
SYNCTHING_ROOT="$HOME/syncthing"
BACKUP_ROOT="$SYNCTHING_ROOT/backup/$MACHINE_ID"
OPENCLAW_BACKUP_DIR="$BACKUP_ROOT/openclaw"
CLAUDE_BACKUP_DIR="$BACKUP_ROOT/claude"
CODEX_BACKUP_DIR="$BACKUP_ROOT/codex"
CURSOR_BACKUP_DIR="$BACKUP_ROOT/cursor"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CURSOR_HOME="${CURSOR_HOME:-$HOME/.cursor}"
CURSOR_USER_DIR="${CURSOR_USER_DIR:-$HOME/.config/Cursor/User}"
BACKUP_LOG="$HOME/.local/log/backup.log"
EOF

# 4. Create directories
source ~/.config/backup/config
mkdir -p "$BACKUP_ROOT"

# 5. Test
~/bin/backup
```

## Syncthing Setup

Each machine writes to its own `{machine-id}/` subdirectory under a shared Syncthing folder. `.stignore` prevents syncing other machines' data.

### .stignore (machine isolation)

Place at `~/syncthing/backup/.stignore`:
```
!my-machine-name
!my-machine-name/**
*
```

For receive-only / hub machines that want ALL machines' data, leave `.stignore` empty or absent.

## Automation

### System cron
```bash
(crontab -l 2>/dev/null; echo '*/30 * * * * /home/$(whoami)/bin/backup >> ~/.local/log/backup-cron.log 2>&1') | crontab -
```

## Diagnostics

```bash
./syncthing-doctor.sh        # Health check
tail -f ~/.local/log/backup.log  # View log
~/bin/backup                 # Manual backup
```

## Backup Contents

### OpenClaw
- Sessions: `~/.openclaw/agents/main/sessions/*.jsonl`
- Memory: `~/.openclaw/workspace/memory/*.md` + `~/.openclaw/memory/main.sqlite`
- Config: `~/.openclaw/openclaw.json`, SOUL.md, IDENTITY.md, USER.md, TOOLS.md, AGENTS.md

### Claude Code
- Projects: `~/.claude/projects/**` (conversations, subagents, tool-results)
- History: `~/.claude/history.jsonl`
- Settings: `~/.claude/settings.json`

### Codex
- Sessions: `~/.codex/sessions/**/*.jsonl`
- History: `~/.codex/history.jsonl`
- Config: `~/.codex/config.toml`

### opencode
- Sessions DB: `~/.local/share/opencode/opencode.db` (SQLite snapshot via `sqlite3 .backup`; falls back to rsync of db+wal+shm)
- Legacy JSON sessions: `~/.local/share/opencode/{storage,project}/` (pre-SQLite installs)
- Config: `~/.config/opencode/` (plugin `node_modules` excluded)
- Prompt history: `~/.local/state/opencode/prompt-history.jsonl`
- NOT backed up: `auth.json`, `mcp-auth.json` (credentials)

### Cursor
- Agent transcripts + per-project state: `~/.cursor/projects/**` (transcripts in `agent-transcripts/*/*.jsonl`, `node_modules` excluded)
- Settings: `~/.config/Cursor/User/settings.json`
- macOS: set `CURSOR_USER_DIR="$HOME/Library/Application Support/Cursor/User"` in config

## Multiple Profiles (work / personal accounts)

Claude Code (`CLAUDE_CONFIG_DIR`) and Codex (`CODEX_HOME`) support fully
isolated "spaces" — separate logins, settings, history, MCP config — on one
machine. Declare the extra spaces in `~/.config/backup/config`:

```bash
CLAUDE_PROFILES="work:$HOME/.claude-work personal:$HOME/.claude-personal"
CODEX_PROFILES="work:$HOME/.codex-work personal:$HOME/.codex-personal"
OPENCODE_PROFILES="work:$HOME/.opencode-work personal:$HOME/.opencode-personal"
```

Each profile is backed up to its own sibling directory (`claude-work/`,
`codex-personal/`, ...); the primary dir keeps the plain `claude/` / `codex/`
layout. Note: opencode profile paths are profile *roots* containing
`share/config/state` subdirs, not the config dir itself. See
**[PROFILES.md](PROFILES.md)** for the full setup guide: shell wrappers,
new-machine checklist, platform caveats (macOS Keychain), and gotchas.

## Adding New Tools

Add new `*_BACKUP_DIR` and `*_HOME` variables in `~/.config/backup/config`, then add a matching `backup_<tool>()` function in `backup.sh` and call it from `main()`.

## License

MIT
