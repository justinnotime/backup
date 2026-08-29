# Repository guidance

For requests about additional harness roots, scheduled backups, exclusions,
destination compatibility, or Syncthing layout, read `PROFILES.md` before
changing files and run `tests/run.sh` afterwards.

Preserve `backup.sh`, `~/bin/backup`, `~/.config/backup/config`, the existing
`*_PROFILES` formats, and existing destination names. New behavior must support
both native-default-only and additional-root installations.

Keep this repository generic. Labels are opaque; machine- and account-specific
policy belongs in local configuration or a private orchestration repository.
Do not add a machine-specific setup Skill here.
