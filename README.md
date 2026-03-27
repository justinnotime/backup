# Universal Backup System for AI Development Tools

Unified backup solution for OpenClaw, Claude Code, and future AI tools (Codex, Cursor, etc.) with Syncthing P2P synchronization.

## Architecture

```
~/syncthing/backup/{machine-id}/
├── openclaw/          # OpenClaw sessions, memory, config
├── claude/            # Claude Code projects, history
├── codex/             # (future)
└── cursor/            # (future)
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
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
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

## Adding New Tools

Add to `~/.config/backup/config`:
```bash
CODEX_BACKUP_DIR="$BACKUP_ROOT/codex"
CODEX_HOME="$HOME/.codex"
```

Then add a `backup_codex()` function in `backup.sh` and call it from `main()`.

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

## License

MIT
