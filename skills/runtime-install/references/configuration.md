# Installation configuration

Both commands require `schema: "runtime-install/v1"` and a matching `kind`
(`skills` or `cron`). All installation paths are explicit absolute paths;
`~` in a path is expanded against the caller's HOME. No private defaults or
credential paths are discovered. `--config -` reads JSON from stdin.

## Discovery links

```json
{
  "schema": "runtime-install/v1",
  "kind": "skills",
  "lock": "/example/state/links.lock",
  "packages": {
    "example-tool": {
      "source": "/example/packages/example-tool",
      "required": [{"path": "SKILL.md", "kind": "file"},
                   {"path": "scripts/run", "kind": "executable"}]
    }
  },
  "destinations": ["/example/client/skills"],
  "profiles": [{"source": "/example/private/preferences.md",
                "destination": "/example/config/example-tool/profile.md"}],
  "retired_links": [{"path": "/example/client/skills/old-tool",
                     "replacement": "/example/client/skills/example-tool",
                     "owned_targets": ["/example/packages/old-tool"]}]
}
```

Each selected package needs an existing source directory and an explicit list
of required files/executables. Every destination receives each selected package.
Existing symlinks may be refreshed; actual files and directories are preserved.
Profile symlinks are relative, so moving both directories together preserves
links. Only absent or already-owned profile links are installed. Foreign links
and custom files remain unchanged. Retirement removes only a symlink with an
exact configured target when its replacement is available as a symlink.

The plan checks every source and destination parent before writes. Real link
installation holds its configured lock and restores previously changed links
if an operation fails. Another noncooperating program changing those same
paths concurrently remains outside this transaction's ownership.

## Managed crontab block

```json
{
  "schema": "runtime-install/v1",
  "kind": "cron",
  "lock": "/example/state/crontab-install.lock",
  "backup_directory": "/example/state/cron-backups",
  "directories": ["/example/logs"],
  "markers": ["# BEGIN example jobs", "# END example jobs"],
  "legacy_markers": [],
  "nested_marker_prefixes": ["# BEGIN ", "# END "],
  "lines": ["15 * * * * /example/bin/collect --config /example/private/job.json"],
  "remove_commands": [["/example/bin/old-collect", "--config", "/example/private/job.json"]],
  "remove_lines": [],
  "requirements": [{"path": "/example/bin/collect", "kind": "executable"}],
  "checks": [{"argv": ["/example/bin/collect", "--config", "/example/private/job.json", "--doctor"]}],
  "before_apply": [],
  "crontab_command": ["crontab"]
}
```

`lines` contain complete single-line cron entries chosen by the caller; the
installer is not a cron expression generator. The caller must quote commands
correctly, including cron's special percent syntax. `remove_commands` matches
contiguous shell argument sequences outside removed blocks; quoted paths with
spaces are supported and a different config argument remains distinct. One
explicit shell `-c` argument is inspected for legacy wrapped commands. Comment
text is not treated as an executable command. `remove_lines` matches exact
whole lines. Missing, reversed, duplicated, nested or overlapping selected
markers fail before installation. Other blocks remain untouched. Optional
`leading_blank` preserves an existing installer's leading separator convention.

`requirements` accepts `file`, `executable` or `directory`. Commands in `checks`
and `before_apply` have `argv`, optional `environment`, `unset_environment`,
`timeout` (seconds; default 60), and `message` for a caller-supplied failure
label. Check commands must themselves be read-only. Captured command diagnostics
are not printed. `before_apply` runs only for real installation, after reading
the existing crontab under the lock, before any backup or crontab update. Its
external side effects cannot be undone by this package.

The crontab command follows the standard `-l`, `FILE`, `-r` interface. A failed
read is accepted as an empty initial crontab only for exit 1 containing
`no crontab`. Installation verifies exact output and restores the original
bytes, or the original absence, if write/readback fails. Backups are private
files and remain available for inspection. Repeating an install yields the
same managed contents; unrelated retained lines keep their original bytes.
