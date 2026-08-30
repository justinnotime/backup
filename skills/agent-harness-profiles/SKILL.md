---
name: agent-harness-profiles
description: Configure, install, inspect, or migrate named isolated roots for Claude Code, Codex, OpenCode, and DeepSeek Harness. Use for generated launchers, shared Skill discovery, or compatible Backup setup; profile names and meanings must come from caller-owned configuration.
---

# Agent Harness Profiles

This Skill is generic. Treat labels as opaque and never infer account type,
trust, ownership, or machine identity from a label or path. Real names, meanings,
roots, ports, repository choices, and schedules belong in caller-owned
configuration.

The stable configuration interface is `~/.config/backup/config` and its existing
`CLAUDE_PROFILES`, `CODEX_PROFILES`, `OPENCODE_PROFILES`, and `DSH_PROFILES`
variables. A private repository may own that file or source a tracked private
fragment from it. Read the Backup contract in
`../state-backup/references/profiles.md` before changing the formats.
The configuration is trusted local shell code; path checks prevent accidental
misconfiguration, not hostile commands in that file.
The scripts require Bash 4+, Git, rsync, and a `realpath` implementation with
GNU-compatible `-m` and `-s` options.

## Install or update

Inspect the current config, generated launchers, links, and shell functions.
Then run:

```bash
scripts/install.sh --config "$HOME/.config/backup/config"
```

The installer:

1. validates the checkout, launcher target, every configured root, and every
   planned link before writing;
2. renders `~/.config/agent-harness-profiles/launchers.sh`;
3. prepares only `share/opencode`, `state/opencode`, and `config/opencode`
   inside each configured OpenCode root, without copying native configuration
   or credentials;
4. links this canonical Skill into the shared and configured Claude discovery
   roots;
5. preserves `~/bin/backup` as a link to the stable repository entrypoint;
6. runs the doctor.

It does not edit shell startup files, install services or schedulers, start a
process, move state roots, or copy authentication data. Review the generated
launcher file before sourcing it. Plain upstream commands continue to select
their native default roots.

For diagnosis without mutation, run `scripts/doctor.sh --config FILE`. Use
`scripts/render-launchers.sh --config FILE` to inspect generated text on stdout.

## Safety

- Refuse unmanaged output files and divergent links.
- Refuse roots that use dot components, resolve to native roots, overlap one
  another, or redirect managed children outside their configured root.
- Never derive a machine label from host or hardware state; require an opaque
  value in local configuration.
- Profile isolation prevents accidental cross-use but is not a same-user
  security boundary.
- Keep service ports and lifecycles in their owner configuration.
- Install only from the primary `main` checkout. Linked worktrees, detached
  checkouts, topic branches, and uncommitted linked implementation files are
  rejected before any persistent write. Git cannot identify which independent
  clone an operator considers long-lived, so invoke the installer only from the
  selected durable checkout.

After changing this Skill, run `tests/run.sh`, the repository Python tests, and
the Skill Creator validator against this directory.
