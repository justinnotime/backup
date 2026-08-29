---
name: agent-harness-profiles
description: Provision, inspect, migrate, or back up isolated state roots for Claude Code, Codex, OpenCode, and DeepSeek Harness. Use when setting up a new machine, creating named harness profiles, generating launch commands, sharing this skill across harnesses, configuring Syncthing-backed state copies, or checking compatibility with an existing backup installation.
---

# Agent Harness Profiles

Treat labels as opaque local names. Never infer account ownership, trust level,
or data classification from a label.

Require Bash 4+, coreutils, and rsync. Recommend sqlite3 for consistent OpenCode
database snapshots.

## Select the operation

- For a new machine or a new label, follow **Provision or reproduce** below.
- For an existing machine, read [references/migration.md](references/migration.md)
  before changing any entrypoint or configuration.
- For exact environment-variable and state-root behavior, read
  [references/harness-roots.md](references/harness-roots.md).
- For component ownership and discovery, read
  [references/architecture.md](references/architecture.md).
- For backup-only source and destination contracts, read the repository's
  `PROFILES.md`; do not duplicate that material in another document.

## Provision or reproduce

1. Locate this skill's repository checkout. All bundled scripts resolve the
   checkout from their own physical path; do not copy a helper out by itself.
2. Inspect existing shell functions, services, `~/.config/backup/config`, and
   deployed symlinks before writing. Preserve an existing valid setup.
3. Create or update `~/.config/backup/config` using
   [assets/backup-config.example](assets/backup-config.example). Keep existing
   `*_PROFILES` variables and `label:path` formats when migrating.
4. Generate shell launchers without editing a shell startup file directly:

   ```bash
   scripts/render-launchers.sh --output "$HOME/.config/agent-harness-profiles/launchers.sh"
   ```

   Review the generated file, then source it from the applicable shell startup
   file. Do not globally export a profile-selection variable.
5. When OpenCode has additional roots, prepare their XDG layout and conservative
   config links:

   ```bash
   scripts/prepare-opencode-roots.sh
   ```

6. Install shared discovery links and the stable backup command:

   ```bash
   scripts/install-links.sh
   ```

   This links one canonical skill into the shared `.agents` root used by Codex,
   OpenCode, and DSH, and into Claude Code's default and configured roots.
7. For a long-running DSH instance, ensure every service has a distinct
   `DSH_HOME` and listening port. Keep service definitions in their deployment
   owner; this repository generates CLI launchers but does not own service
   units or provider credentials.
8. Run `scripts/doctor.sh`, then run `scripts/backup` manually and inspect its
   log before enabling or changing a scheduler.

## Scheduler contract

Keep existing jobs pointed at `backup.sh` or `~/bin/backup`; both remain stable
compatibility entrypoints. New jobs may call `scripts/backup` from this skill.
The scheduler invokes deterministic code, not an interactive agent or a skill
activation.

## Safety boundaries

- Refuse to overwrite divergent files or symlinks. Report the path and let the
  operator choose how to reconcile it.
- Never copy authentication stores merely to make a profile portable. The
  backup script excludes known credential locations; re-authenticate after a
  restore.
- A separate state root prevents accidental cross-use. It is not an operating
  system security boundary for processes running as the same user.
- A destination suffix is organization, not access control. Every peer allowed
  to receive the Syncthing folder may receive every contained root.
- Keep machine-specific labels, paths, service units, ports, and account policy
  in local configuration or a private orchestration repository, not here.

## Verify changes to this repository

Run:

```bash
tests/run.sh
python3 /path/to/skill-creator/scripts/quick_validate.py \
  .agents/skills/agent-harness-profiles
```

Also run `bash -n` on every changed shell script. Do not declare a migration
complete until a legacy configuration and a default-only configuration both
pass.
