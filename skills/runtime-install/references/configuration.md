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
installer accepts the selected cron syntax verbatim. The caller must quote commands
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

## Native environment values and structured jobs

A configuration file can be kept directly in its owning repository and linked
into a caller's configuration directory. `${CONFIG_DIR}` resolves to the actual
source file's directory after following that link. Moving the source and link
together therefore preserves adjacent-file references. Standard `${HOME}`,
`${HOSTNAME}` and XDG config, state and cache variables are available; unset XDG
variables use their standard HOME-relative defaults. Other `${NAME}` references
must exist in the environment. No shell expansion or command substitution runs.
`--config -` has no source directory and cannot use `${CONFIG_DIR}`.

A value can explicitly select an environment variable with a private default:

```json
{
  "env": "EXAMPLE_PACKAGE_ROOT",
  "default": "~/packages/example-tool",
  "suffix": "/scripts/run"
}
```

The optional suffix is a path suffix beginning with `/`. The selected value is
joined with that suffix and normalized without following links. A default can
itself be another environment selection. Empty and unset variables use the
configured default; an absent default fails. This is value selection only:
configuration cannot define variables or execute a configuration generator.

Cron settings may use `jobs` instead of `lines`:

```json
{
  "jobs": [{
    "id": "collect",
    "schedule": "15 * * * *",
    "argv": ["/example/bin/collect", "--config", "${CONFIG_DIR}/job.json"],
    "environment": {"OPTION": "a value with spaces"},
    "log": "/example/logs/collect.log"
  }]
}
```

The caller chooses the five-field schedule. The installer quotes each argument,
environment value and optional append-log path, rejecting newline, NUL and
percent characters. Use explicit `lines` for cron percent syntax. Empty
environment values are omitted. `jobs` and `lines` cannot appear together.
`--print-job collect` prints only that selected command line; it does not read
crontab, run checks, create files or provision installation prerequisites. A
caller can use this read-only command as the authoritative expected cron line.
