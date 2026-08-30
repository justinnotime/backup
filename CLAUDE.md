# Repository guidance

`CLAUDE.md` is the canonical repository contract. `AGENTS.md` must remain a
relative symbolic link to this file so every supported agent reads one copy.

For requests about additional harness roots, scheduled backups, exclusions,
destination compatibility, or Syncthing layout, read `PROFILES.md` before
changing files. Run `tests/run.sh` after changing state-backup behavior.

Preserve `backup.sh`, `~/bin/backup`, `~/.config/backup/config`, the existing
`*_PROFILES` formats, and existing destination names. New backup behavior must
support both native-default-only and additional-root installations. The root
script names are compatibility links; their behavior authority lives in the
matching Skill package.

Keep this repository generic. Labels are opaque; machine- and account-specific
policy belongs in local configuration or a private orchestration repository.
Do not add machine launchers, services, credentials, schedules, or account
policy here. Backup Profile labels never grant session-extraction access.

For a new or changed Skill, run the Skill validator against that package. Run
the focused Python tests after changing the session-extraction runtime.
