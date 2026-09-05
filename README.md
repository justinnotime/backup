# Shared Agent Harness Skills

This repository retains its `backup` name and stable backup entrypoints while
growing into a collection of deterministic agent-harness Skills. It contains a
state backup, Syncthing diagnostics, a remote clipboard helper, and a shared
session-extraction runtime, and a configurable GitHub archive. It also provides generic setup helpers for named
harness roots; callers retain all label meaning in their own configuration.

The repository works in both shapes:

- native defaults only, with no named-root configuration;
- any number of additional labeled roots for supported harnesses.

Labels are opaque. This repository does not assign account, organization, or
trust semantics to them.

## Repository map

| Path | Purpose |
|---|---|
| `skills/` | Installable deterministic behavior packages |
| `skills/state-backup/` | State-backup Skill and canonical backup script |
| `backup.sh` | Stable compatibility link retained for existing jobs and links |
| `skills/syncthing-doctor/` | Syncthing diagnostic Skill and canonical doctor script |
| `PROFILES.md` | Backup source-root, destination, exclusion, and upgrade contract |
| `syncthing-doctor.sh` | Stable compatibility link for Syncthing diagnostics |
| `skills/remote-clipboard/` | Remote-to-local clipboard Skill and shell function |
| `clip.sh` | Stable compatibility link for the clipboard shell helper |
| `skills/agent-harness-profiles/` | Configuration-driven launcher and Skill-link setup |
| `skills/agent-session-extraction/` | Manifest-driven extraction Skill and command wrappers |
| `skills/github-archive/` | Caller-configured GitHub issue, pull-request, and comment archive |
| `skills/agent-session-extraction/src/agent_skills/sessions/` | Package-owned normalized-session runtime |
| `skills/<name>/tests/` | Tests owned and runnable by that Skill |

The three root shell paths are relative symbolic links into their Skill
packages. This keeps existing scheduler and shell configuration working while
making each implementation part of its behavior package.

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

## Quick start: additional roots

```bash
mkdir -p "$HOME/.config/backup"
${EDITOR:-vi} "$HOME/.config/backup/config"
"$HOME/src/backup/backup.sh"
```

See [PROFILES.md](PROFILES.md) for the exact config, source-root, exclusion,
destination, and compatibility contract. Machine-specific launchers, account
policy, service definitions, and orchestration belong outside this module.

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

Existing and new scheduler entries should use `backup.sh` or `~/bin/backup`.
Both are compatibility interfaces:

```cron
*/30 * * * * /absolute/path/to/backup/backup.sh >> /absolute/path/to/backup-cron.log 2>&1
```

The scheduler calls a deterministic script. It does not invoke an interactive
agent.

## Upgrade safety

The project retains existing configuration variable names, list formats,
destination suffixes, `backup.sh`, `~/bin/backup`, and `PROFILES.md`. Backup
validation rejects unsafe DSH sources, destinations, labels, and links.

Before deploying an update:

```bash
bash skills/state-backup/tests/run.sh
python3 -B -m unittest discover -s skills/state-backup/tests -v
```

Then run the existing backup command manually and inspect its log.

Each Skill contains its own source, tests, and dependency declarations and can
be copied without sibling packages. There is no repository-wide Python project
or shared source directory. Run additional checks from the affected package:

```bash
(cd skills/agent-harness-profiles && bash tests/run.sh)
(cd skills/agent-session-extraction && uv sync --locked --extra test && uv run --no-sync pytest tests)
(cd skills/github-archive && uv run --locked pytest tests)
```

Consumers of the session Python API configure the runtime root as
`/absolute/path/to/skills/agent-session-extraction`; its import directory is
`<runtime-root>/src`. The package's command wrappers resolve this themselves.
The profiles installer accepts an optional `BACKUP_COMMAND` in local
configuration instead of discovering another Skill's source files.

Validate every new or changed Skill with the Skill Creator validator before
publishing it. Dependency installation and validation artifacts belong in the
individual package or a disposable workspace, never a root shared environment.

## GitHub archive

The `github-archive` Skill contains its own Python package, dependencies, and
tests. Select repositories and archive paths in a private YAML file outside
this repository, following [its configuration reference](skills/github-archive/references/config.md).
Authenticate `gh` separately, then run:

```bash
uv sync --project skills/github-archive --locked
skills/github-archive/scripts/sync --config /path/to/private/config.yaml --dry-run
skills/github-archive/scripts/sync --config /path/to/private/config.yaml
```

The command uses read-only GitHub requests. Callers own configuration, archive,
state, scheduling, and publication. No other Skill is required.

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
