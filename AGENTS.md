# Repository guidance

For requests about isolated harness roots, named profiles, new-machine setup,
migration, skill sharing, scheduled backups, or Syncthing layout, read and
follow `.agents/skills/agent-harness-profiles/SKILL.md` before changing files.

Preserve `backup.sh`, `~/bin/backup`, `~/.config/backup/config`, the existing
`*_PROFILES` formats, and existing destination names. New behavior must support
both native-default-only and additional-root installations.

Keep this repository generic. Labels are opaque; machine- and account-specific
policy belongs in local configuration or a private orchestration repository.
