# Multi-Profile Setup for Claude Code, Codex, opencode & DSH

How to run separate work/personal "spaces" (isolated accounts, settings,
history, MCP config) for Claude Code, Codex, opencode, and DSH on one machine,
and have this backup system pick them all up. Reference this doc when setting
up a new machine.

## How it works

Claude Code, Codex, and DSH relocate their entire user-level state directory
through an environment variable:

| Tool | Env var | Default | Isolates |
|------|---------|---------|----------|
| Claude Code | `CLAUDE_CONFIG_DIR` | `~/.claude` | login credentials¹, settings, history, projects, plugins, user-level MCP config |
| Codex | `CODEX_HOME` | `~/.codex` | `auth.json`, `config.toml`, sessions, history |
| DeepSeek Harness | `DSH_HOME` | `~/.dsh` | credentials, settings, sessions, attachments, storages, profiles |

Point the variable at a different directory and you get a fully independent
space. A process with no variable set keeps using the tool's default directory.

¹ Platform caveat: on **Linux/Windows** credentials live in a file inside the
config dir (`.credentials.json`), so isolation includes login state. On
**macOS** Claude Code stores credentials in the system Keychain, which is
shared — `CLAUDE_CONFIG_DIR` still isolates settings/history, but login state
may be shared across profiles.

Note: project-level config (`.claude/settings.json`, `CLAUDE.md`, `AGENTS.md`
inside a repo) follows the repo, not the profile — it applies in every space.

### opencode is different: no single home dir

opencode follows the XDG spec and has **no single env var that relocates all
state**. Its state is spread across four dirs, each governed separately:

| What | Default | Env var |
|------|---------|---------|
| Config (`opencode.json`, agents/, plugins/) | `~/.config/opencode` | `XDG_CONFIG_HOME` (see warning below) |
| Data: **auth.json**, sessions DB (`opencode.db`), logs | `~/.local/share/opencode` | `XDG_DATA_HOME` only |
| State: lock files, prompt history | `~/.local/state/opencode` | `XDG_STATE_HOME` |
| Cache: downloaded provider packages | `~/.cache/opencode` | `XDG_CACHE_HOME` |

⚠️ **`OPENCODE_CONFIG_DIR` / `OPENCODE_CONFIG` do NOT isolate config** —
they are *additive layers*: opencode still merges the global
`~/.config/opencode/opencode.json` underneath (verified empirically on
v1.18.0 with `opencode models`). Anything defined globally — e.g. an LLM
provider with an inline API key — remains visible and usable in every
profile. Only `XDG_CONFIG_HOME` truly relocates the global config.

So an opencode profile is a *root directory* plus three XDG env vars (set
per-invocation by the wrapper below). Cache is deliberately left shared —
it holds no account state, and separate caches would re-download provider
binaries per profile. Native multi-account support is an open opencode
feature request; the XDG approach is what community profile tools use today.

Because `XDG_CONFIG_HOME` also redirects config lookups for subprocesses
spawned inside opencode sessions (git, gh, ...), symlink the other entries
of `~/.config` into the profile's config dir once at setup:

```bash
for p in ~/.opencode-work ~/.opencode-personal; do
  mkdir -p "$p/config/opencode"
  for d in ~/.config/*; do
    base=$(basename "$d")
    [ "$base" = "opencode" ] && continue
    ln -sfn "$d" "$p/config/$base"
  done
done
```

(Re-run the loop if a newly installed tool's `~/.config/<tool>` needs to be
visible inside profile sessions.)

### DSH uses one home per instance

Each long-lived DSH Web process needs a distinct `DSH_HOME` and listening port.
The `profiles/` directory inside a DSH home selects agent compositions; it is
not an account or transcript isolation boundary. Use separate top-level homes,
such as `~/.dsh-personal` and `~/.dsh-work`.

## New machine checklist

1. **Shell wrappers and services** — add the CLI wrappers to `~/.zshrc` /
   `~/.bashrc`:

   ```bash
   # --- AI tool profiles: isolated work/personal spaces ---
   claude-work()     { CLAUDE_CONFIG_DIR="$HOME/.claude-work"     command claude "$@"; }
   claude-personal() { CLAUDE_CONFIG_DIR="$HOME/.claude-personal" command claude "$@"; }
   codex-work()      { CODEX_HOME="$HOME/.codex-work"             command codex "$@"; }
   codex-personal()  { CODEX_HOME="$HOME/.codex-personal"         command codex "$@"; }

   # opencode: profile root + three XDG env vars (see "opencode is different" above;
   # OPENCODE_CONFIG_DIR would NOT isolate — the global config gets merged in)
   opencode-work() {
     local p="$HOME/.opencode-work"
     XDG_DATA_HOME="$p/share" XDG_STATE_HOME="$p/state" XDG_CONFIG_HOME="$p/config" command opencode "$@"
   }
   opencode-personal() {
     local p="$HOME/.opencode-personal"
     XDG_DATA_HOME="$p/share" XDG_STATE_HOME="$p/state" XDG_CONFIG_HOME="$p/config" command opencode "$@"
   }
   ```

   For opencode, also run the config-symlink loop from the section above and
   drop a minimal `$p/config/opencode/opencode.json`
   (`{"$schema": "https://opencode.ai/config.json"}`) into each profile.

   For DSH Web, create one service per role with a unique `DSH_HOME` and port;
   do not globally export `DSH_HOME`.

   If an existing Claude/Codex/opencode default profile already serves as one
   of the roles, skip that wrapper. DSH backup intentionally requires named
   `.dsh-<role>` homes; migrate a default `~/.dsh` home before listing it.

   For a one-off run without wrappers: `CLAUDE_CONFIG_DIR=~/.claude-work claude`.
   Don't globally `export` these variables — that would lock every shell to
   one space.

2. **First login** — run each wrapper once and log in to the matching
   account (`/login` in Claude Code, `codex login` for Codex,
   `opencode auth login` for opencode). Start each DSH instance and configure
   its matching provider credentials separately. Each space remembers its own
   login afterwards.

3. **Backup config** — declare the extra profiles in
   `~/.config/backup/config` as `name:path` entries. DSH entries are
   newline-separated so their paths may contain spaces:

   ```bash
   CLAUDE_PROFILES="work:$HOME/.claude-work personal:$HOME/.claude-personal"
   CODEX_PROFILES="work:$HOME/.codex-work personal:$HOME/.codex-personal"
   OPENCODE_PROFILES="work:$HOME/.opencode-work personal:$HOME/.opencode-personal"
   DSH_PROFILES="personal:$HOME/.dsh-personal
   work:$HOME/.dsh-work"
   ```

   For Claude/Codex/DSH the path is the profile root itself; for opencode it is
   the profile *root* (the backup script derives `share/opencode`,
   `config/opencode`, and `state/opencode` under it, matching the wrapper's
   env vars).

   DSH labels must begin with a lowercase letter or digit and may then use
   lowercase letters, digits, underscores, or hyphens. Each source directory
   must be named exactly `.dsh-<label>`; duplicate labels are skipped rather
   than merged. Paths may contain spaces but must be absolute.

   Profiles whose directory doesn't exist are logged and skipped, so it's safe
   to declare them before first use. An existing empty DSH root is backed up as
   an empty profile.

4. **Verify** — run `~/bin/backup` and check the log: each profile gets its
   own sibling backup dir under `~/syncthing/backup/{machine-id}/`:

   ```
   claude/            # default profile (unchanged layout)
   claude-work/
   claude-personal/
   codex/
   codex-work/
   codex-personal/
   opencode/
   opencode-work/
   opencode-personal/
   dsh-work/
   dsh-personal/
   ```

   The Syncthing `.stignore` pattern (`!{machine-id}/**`) already covers
   these — no Syncthing changes needed.

## Gotchas

- **Known credential stores are excluded** (`.credentials.json`, `auth.json`,
  `mcp-auth.json`, DSH `.credentials.yaml`, `.oauth/`, and `.env*`). Re-login
  on restore.
  A custom DSH credential filename or a secret manually embedded in settings
  or transcripts cannot be identified by filename and remains the operator's
  responsibility.
- **Profile suffixes are not a trust boundary.** `dsh-personal/` and
  `dsh-work/` are siblings in the same Syncthing machine tree. Every receiver
  of that shared backup folder may receive both profiles.
- **opencode env vars are read at process startup** — the wrapper approach
  works, but you can't switch profiles mid-session, and IDE/desktop
  integrations that spawn opencode won't inherit shell functions; set the
  env vars in whatever launches opencode there.
- **XDG vars also affect subprocesses** opencode spawns during that
  invocation. `XDG_CONFIG_HOME` is the impactful one (git global ignore,
  gh auth, etc.) — mitigated by the config-symlink loop above. A subprocess
  that *creates* a new `~/.config/<tool>` inside a profile session writes it
  under the profile root instead; re-run the symlink loop if that happens.
- **Provider API keys inlined in the global `opencode.json` are shared** by
  any profile that can see that config — and before the `XDG_CONFIG_HOME`
  fix they leaked into every profile via config merging. For per-profile
  provider accounts, issue separate keys and reference them as
  `{env:VAR_NAME}` in each profile's config rather than inlining.
- **opencode sessions live in SQLite** (`opencode.db`, WAL mode). The backup
  script snapshots it with `sqlite3 .backup` for consistency; install
  `sqlite3` on new machines (it falls back to rsync of db+wal+shm with a
  warning otherwise).
- **`~/.claude.json`** (user-level MCP servers, project trust/onboarding
  state) moves inside the config dir when `CLAUDE_CONFIG_DIR` is set. If MCP
  servers or project trust look wrong after switching profiles, check which
  copy of this file the session is using.
- **macOS Cursor path** differs (`~/Library/Application Support/Cursor/User`)
  — set `CURSOR_USER_DIR` in the backup config. Cursor itself has no
  equivalent profile env var.
