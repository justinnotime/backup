# Multi-Profile Setup for Claude Code & Codex

How to run separate work/personal "spaces" (isolated accounts, settings,
history, MCP config) for Claude Code and Codex on one machine, and have this
backup system pick them all up. Reference this doc when setting up a new
machine.

## How it works

Both tools support relocating their entire user-level state directory via an
environment variable:

| Tool | Env var | Default | Isolates |
|------|---------|---------|----------|
| Claude Code | `CLAUDE_CONFIG_DIR` | `~/.claude` | login credentials¹, settings, history, projects, plugins, user-level MCP config |
| Codex | `CODEX_HOME` | `~/.codex` | `auth.json`, `config.toml`, sessions, history |

Point the variable at a different directory and you get a fully independent
space. The plain `claude` / `codex` command (no variable set) keeps using the
default directory — that's the "default profile", i.e. whatever account you
were already logged into.

¹ Platform caveat: on **Linux/Windows** credentials live in a file inside the
config dir (`.credentials.json`), so isolation includes login state. On
**macOS** Claude Code stores credentials in the system Keychain, which is
shared — `CLAUDE_CONFIG_DIR` still isolates settings/history, but login state
may be shared across profiles.

Note: project-level config (`.claude/settings.json`, `CLAUDE.md`, `AGENTS.md`
inside a repo) follows the repo, not the profile — it applies in every space.

## New machine checklist

1. **Shell wrappers** — add to `~/.zshrc` / `~/.bashrc`:

   ```bash
   # --- AI tool profiles: isolated work/personal spaces ---
   claude-work()     { CLAUDE_CONFIG_DIR="$HOME/.claude-work"     command claude "$@"; }
   claude-personal() { CLAUDE_CONFIG_DIR="$HOME/.claude-personal" command claude "$@"; }
   codex-work()      { CODEX_HOME="$HOME/.codex-work"             command codex "$@"; }
   codex-personal()  { CODEX_HOME="$HOME/.codex-personal"         command codex "$@"; }
   ```

   If the existing default profile already serves as one of the roles (e.g.
   it's your personal account), skip that wrapper — you only need dirs for
   the *additional* accounts.

   For a one-off run without wrappers: `CLAUDE_CONFIG_DIR=~/.claude-work claude`.
   Don't globally `export` these variables — that would lock every shell to
   one space.

2. **First login** — run each wrapper once and log in to the matching
   account (`/login` in Claude Code, `codex login` for Codex). The directory
   is created automatically; each space remembers its own login afterwards.

3. **Backup config** — declare the extra profiles in
   `~/.config/backup/config` as space-separated `name:path` entries:

   ```bash
   CLAUDE_PROFILES="work:$HOME/.claude-work personal:$HOME/.claude-personal"
   CODEX_PROFILES="work:$HOME/.codex-work personal:$HOME/.codex-personal"
   ```

   Profiles whose directory doesn't exist (or has no data yet) are logged and
   skipped, so it's safe to declare them before first use.

4. **Verify** — run `~/bin/backup` and check the log: each profile gets its
   own sibling backup dir under `~/syncthing/backup/{machine-id}/`:

   ```
   claude/            # default profile (unchanged layout)
   claude-work/
   claude-personal/
   codex/
   codex-work/
   codex-personal/
   ```

   The Syncthing `.stignore` pattern (`!{machine-id}/**`) already covers
   these — no Syncthing changes needed.

## Gotchas

- **Credentials are never backed up** (`.credentials.json`, `auth.json`) —
  by design; don't sync login tokens through Syncthing. Re-login on restore.
- **`~/.claude.json`** (user-level MCP servers, project trust/onboarding
  state) moves inside the config dir when `CLAUDE_CONFIG_DIR` is set. If MCP
  servers or project trust look wrong after switching profiles, check which
  copy of this file the session is using.
- **macOS Cursor path** differs (`~/Library/Application Support/Cursor/User`)
  — set `CURSOR_USER_DIR` in the backup config. Cursor itself has no
  equivalent profile env var; multi-profile here covers Claude Code and
  Codex only.
