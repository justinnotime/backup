# Compatibility and migration

## Stable interfaces

The following existing interfaces are retained:

- repository entrypoint `backup.sh`;
- common command symlink `~/bin/backup`;
- local configuration `~/.config/backup/config`;
- `CLAUDE_PROFILES`, `CODEX_PROFILES`, `OPENCODE_PROFILES`, and
  `DSH_PROFILES` variable names and list formats;
- unsuffixed destinations for default roots;
- suffixed destinations for configured labels;
- `PROFILES.md` as the backup source-layout reference.

Do not rename, replace, or silently rewrite these interfaces during an upgrade.

## DeepSeek Harness default behavior

Older multi-root installations explicitly set `DSH_PROFILES` and did not back
up a native default DSH home. `DSH_INCLUDE_DEFAULT=auto` preserves that behavior
when the list is non-empty while allowing a default-only machine to back up
`DSH_HOME` without extra configuration.

Set `DSH_INCLUDE_DEFAULT=true` to include both the default and configured roots,
or `false` to omit the default explicitly.

## Safe migration sequence

1. Inspect the current config, command symlinks, shell functions, scheduler,
   and destination directories.
2. Pull the new repository version without changing local configuration.
3. Run `tests/run.sh` in the checkout.
4. Run `scripts/doctor.sh` against the existing config.
5. Generate launchers into a new temporary path and compare their environment
   variables with the existing functions.
6. Install only non-divergent links. The installer refuses other targets.
7. Run the existing `~/bin/backup` command once and inspect its normal log.
8. Change the scheduler only if its current stable entrypoint is absent.

## Rollback

The setup helpers create links and generated launcher text; they do not move or
delete harness roots. To roll back, stop sourcing the generated launcher file
and remove only links that still resolve to this checkout. Existing backup data
and local configuration remain usable by the old entrypoint.

The backup operation is incremental and does not delete old destination files.
If exclusion rules are tightened, historical destination content may need a
separate, explicitly reviewed cleanup before synchronization.
