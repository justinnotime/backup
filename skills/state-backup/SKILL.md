---
name: state-backup
description: Back up local agent-harness state into the stable machine-scoped Syncthing layout. Use when running or changing backup source roots, exclusions, profile formats, or destination compatibility; do not use for human-readable session extraction or Syncthing health diagnosis.
---

# State Backup

Use `scripts/backup` as the deterministic behavior authority. Existing jobs may
call the repository-root `backup.sh` compatibility link or `~/bin/backup`;
preserve both paths, `~/.config/backup/config`, existing `*_PROFILES` formats,
and existing destination names.

Before changing backup behavior, read
[the source-root contract](references/profiles.md). Support native-default-only and
additional-root installations. Treat every configured label as an opaque
destination suffix, never as an account identity or extraction permission.

Schedulers call the script directly rather than invoking this Skill
conversationally:

```bash
scripts/backup
```

From the repository root, run `tests/run.sh` after any backup behavior or
compatibility change. Keep machine launchers, credentials, schedules, and
account-specific policy outside this shared package.
