# Harness state-root contracts

## Claude Code

Set `CLAUDE_CONFIG_DIR` for the launched process. The selected directory holds
user settings, history, projects, plugins, and user-level configuration.

Repository-level files still follow the repository and can apply across state
roots. On platforms where authentication is stored in a system credential
service, changing the config directory may not isolate the credential itself.

## Codex

Set `CODEX_HOME` for the launched process. Sessions, history, configuration,
and authentication state are selected from that directory.

The shared Agent Skills root remains outside `CODEX_HOME`, which is deliberate:
this repository's skill contains no account policy or credentials and is meant
to be reused by every configured root.

## OpenCode

OpenCode uses three XDG roots rather than one home variable:

| Purpose | Launcher value |
|---|---|
| Data | `XDG_DATA_HOME=<root>/share` |
| Config | `XDG_CONFIG_HOME=<root>/config` |
| State | `XDG_STATE_HOME=<root>/state` |

Leave the cache shared unless local policy requires otherwise. Do not use
`OPENCODE_CONFIG` or `OPENCODE_CONFIG_DIR` as isolation controls; they are
additional configuration layers rather than replacements for the XDG user
config root.

Changing `XDG_CONFIG_HOME` also affects subprocesses. The preparation helper
creates conservative links for the other entries already present under the
normal user config directory. It never replaces a divergent target.

## DeepSeek Harness

Set `DSH_HOME` for the launched process. A top-level DSH home is the instance
boundary for credentials, settings, sessions, attachments, storages, and the
upstream `profiles/` tree.

The `--profile` argument selects a plugin composition *inside* one DSH home; it
does not replace top-level home separation. Long-running instances also need
distinct ports and independently managed service lifecycles.

DSH's filesystem skill provider can scan the shared `~/.agents/skills` root,
so the same generic skill can be visible to every DSH home without copying it
into application state.

## Launcher naming

`scripts/render-launchers.sh` creates `<tool>-<label>` shell functions from the
existing backup configuration. Labels must be lowercase alphanumeric strings
with optional underscores or hyphens. Paths must be absolute.

The plain upstream command remains the native default root. Generated launchers
select only additional roots and never export their variables globally.
