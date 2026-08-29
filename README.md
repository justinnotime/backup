# Agent Harness State Backup

A self-contained backup module and Agent Skill for Claude Code, Codex,
OpenCode, DeepSeek Harness, OpenClaw, and Cursor. It copies selected local state
into one machine-scoped tree that can be synchronized with Syncthing.

The repository works in both shapes:

- native defaults only, with no named-root configuration;
- any number of additional labeled roots for supported harnesses.

Labels are opaque. This repository does not assign account, organization, or
trust semantics to them.

## Repository map

| Path | Purpose |
|---|---|
| `backup.sh` | Stable backup entrypoint retained for existing jobs and links |
| `.agents/skills/agent-harness-profiles/` | Canonical cross-harness Agent Skill |
| `PROFILES.md` | Backup source-root, destination, exclusion, and upgrade contract |
| `syncthing-doctor.sh` | Syncthing process, folder, and ignore-pattern diagnostics |
| `clip.sh` | Optional remote-to-local clipboard shell helper |
| `tests/run.sh` | Default-only and legacy multi-root compatibility checks |

The skill contains the new-machine workflow, launcher generator, conservative
install helpers, detailed harness root contracts, and migration guidance.

## Backup layout

```text
<syncthing-root>/backup/<machine>/
  openclaw/
  claude/
  claude-<label>/
  codex/
  codex-<label>/
  opencode/
  opencode-<label>/
  dsh/
  dsh-<label>/
  cursor/
```

Default roots keep an unsuffixed destination. Additional roots append their
configured label. All destinations are in the same Syncthing tree; suffixes do
not create an access-control boundary.

## Quick start: native defaults

```bash
git clone https://github.com/justinnotime/backup.git "$HOME/src/backup"
mkdir -p "$HOME/bin"
ln -s "$HOME/src/backup/backup.sh" "$HOME/bin/backup"
"$HOME/bin/backup"
```

Without a config file, the script uses the upstream default state locations,
`$HOME/syncthing`, the current hostname as its machine directory, and
`$HOME/.local/log/backup.log`.

## Quick start: additional roots and shared skill

```bash
mkdir -p "$HOME/.config/backup"
cp "$HOME/src/backup/.agents/skills/agent-harness-profiles/assets/backup-config.example" \
  "$HOME/.config/backup/config"

# Edit the local labels and paths, then:
"$HOME/src/backup/.agents/skills/agent-harness-profiles/scripts/render-launchers.sh" \
  --output "$HOME/.config/agent-harness-profiles/launchers.sh"
"$HOME/src/backup/.agents/skills/agent-harness-profiles/scripts/prepare-opencode-roots.sh"
"$HOME/src/backup/.agents/skills/agent-harness-profiles/scripts/install-links.sh"
```

Review the generated launcher file and source it from the relevant shell startup
file. Run the doctor before enabling a scheduler:

```bash
"$HOME/src/backup/.agents/skills/agent-harness-profiles/scripts/doctor.sh"
"$HOME/bin/backup"
```

See [PROFILES.md](PROFILES.md) for the exact config and compatibility contract.

## Supported state

- **OpenClaw:** sessions, memory, selected workspace documents, and config.
- **Claude Code:** projects, history, and settings from the default and labeled
  `CLAUDE_CONFIG_DIR` roots.
- **Codex:** sessions, history, and config from the default and labeled
  `CODEX_HOME` roots.
- **OpenCode:** SQLite or legacy session data, config, and prompt history from
  default and labeled XDG roots.
- **DeepSeek Harness:** sessions, attachments, storages, settings, skills, and
  profile manifests from default and labeled `DSH_HOME` roots.
- **Cursor:** project agent transcripts and user settings.

Known authentication files, OAuth state, environment files, generated
dependencies, and selected telemetry identifiers are excluded. Custom secrets
stored under arbitrary names cannot be detected reliably; review local config
before synchronizing a new source.

OpenCode databases use `sqlite3 .backup` when available. Without `sqlite3`, the
script falls back to copying the database and WAL companions with a warning.

## Syncthing

Configure one Syncthing folder for `<syncthing-root>/backup`. A source machine
that should upload only its own directory can place this in the folder root's
`.stignore`:

```text
!<machine>
!<machine>/**
*
```

A receiver intended to hold every machine directory should leave that ignore
file absent or empty.

Run diagnostics with:

```bash
./syncthing-doctor.sh
```

## Scheduling

Existing scheduler entries should keep using `backup.sh` or `~/bin/backup`.
Both are compatibility interfaces. A new job may also use the skill-owned
wrapper:

```cron
*/30 * * * * /absolute/path/to/backup/.agents/skills/agent-harness-profiles/scripts/backup >> /absolute/path/to/backup-cron.log 2>&1
```

The scheduler calls a deterministic script. It does not invoke an interactive
agent.

## Upgrade safety

The project retains existing configuration variable names, list formats,
destination suffixes, `backup.sh`, `~/bin/backup`, and `PROFILES.md`. Install
helpers preflight every destination and refuse divergent files or links.

Before deploying an update:

```bash
tests/run.sh
.agents/skills/agent-harness-profiles/scripts/doctor.sh
```

Then run the existing backup command manually and inspect its log.

## Clipboard helper

`clip.sh` defines a `clip` shell function that uses `wl-copy` locally and OSC 52
over SSH, mosh, or tmux. Install it separately if needed:

```bash
ln -s "$HOME/src/backup/clip.sh" "$HOME/.clip.sh"
```

Source `$HOME/.clip.sh` from the applicable shell startup file. tmux 3.3 or
newer should use `set -g allow-passthrough all`.

## License

MIT
