---
name: syncthing-doctor
description: Diagnose local Syncthing process, service, configuration, shared-folder, and ignore-pattern health for the state-backup tree. Use for Syncthing inspection; do not use to change backup source selection or extract sessions.
---

# Syncthing Doctor

Run `scripts/doctor` for the deterministic diagnostic. The repository-root
`syncthing-doctor.sh` path remains a compatibility link to the same script.

The doctor reads live local process, service, cron, Syncthing configuration,
and `~/.config/backup/config` state. Run it only when inspection of that machine
is within scope. It does not repair configuration. Its output may contain local
paths or device information, so summarize findings instead of copying raw
output into public artifacts.

```bash
scripts/doctor
```

Use `state-backup` for source roots, exclusions, Profile formats, or backup
destination behavior.
