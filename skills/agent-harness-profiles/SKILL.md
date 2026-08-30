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

## Install or update

Inspect the current config, generated launchers, links, and shell functions.
Then run:

```bash
scripts/install.sh --config "$HOME/.config/backup/config"
```

The installer:

1. renders `~/.config/agent-harness-profiles/launchers.sh`;
2. prepares configured OpenCode roots without replacing divergent targets;
3. links this canonical Skill into the shared and configured Claude discovery
   roots;
4. preserves `~/bin/backup` as a link to the stable repository entrypoint;
5. runs the doctor.

It does not edit shell startup files, install services or schedulers, start a
process, move state roots, or copy authentication data. Review the generated
launcher file before sourcing it. Plain upstream commands continue to select
their native default roots.

For diagnosis without mutation, run `scripts/doctor.sh --config FILE`. Use
`scripts/render-launchers.sh --config FILE` to inspect generated text on stdout.

## Safety

- Refuse unmanaged output files and divergent links.
- Never derive a machine label from host or hardware state; require an opaque
  value in local configuration.
- Profile isolation prevents accidental cross-use but is not a same-user
  security boundary.
- Keep service ports and lifecycles in their owner configuration.
- Publish links only from the stable Backup checkout, never from a disposable
  topic worktree.

After changing this Skill, run `tests/run.sh`, the repository Python tests, and
the Skill Creator validator against this directory.
