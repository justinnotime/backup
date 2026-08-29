# Architecture and ownership

## One canonical skill

The canonical skill lives at:

```text
<checkout>/.agents/skills/agent-harness-profiles/
```

The layout follows the Agent Skills standard: `SKILL.md` contains the workflow,
`references/` contains details, `assets/` contains templates, and `scripts/`
contains deterministic operations.

Codex, OpenCode, and DeepSeek Harness can discover a shared user skill under
`~/.agents/skills`. Claude Code discovers user skills under its own skill root.
`scripts/install-links.sh` links every supported discovery location back to the
same canonical directory, so there is only one copy to update.

## Four layers

```text
local policy/configuration
        |
        v
generated launchers or service environment
        |
        v
upstream harness state roots
        |
        v
backup.sh -> machine directory -> Syncthing
```

- Local policy owns label meanings, account selection, ports, and which roots
  exist.
- The generated launcher layer only sets upstream-supported environment
  variables for one process.
- Each upstream harness owns the contents and schema of its state root.
- This repository owns backup selection, exclusions, destination naming, and
  reusable setup helpers.

Do not make the backup code infer local policy from directory names. Do not make
local policy reimplement the backup engine.

## Plugin boundary

A plugin is an optional distribution envelope around one or more skills and
possibly hooks, connectors, or MCP servers. It is not the canonical format for
this repository because plugin manifests and marketplaces are host-specific.

If plugin distribution is added later, keep this directory unchanged and add a
thin adapter that packages or points to it. Scheduler jobs and local entrypoints
must not depend on a marketplace installation.

## Repository discovery

An agent operating inside this checkout should discover the project skill from
`.agents/skills`. After `scripts/install-links.sh`, the same skill is also
available across repositories from the user's shared skill locations.

When a private repository describes a particular machine, it should contain a
short project page that points here and supplies only the local policy overlay.
It should not copy these reference files or scripts.
