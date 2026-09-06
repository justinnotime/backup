# Configuration and compatibility

The schema is `runtime-layout/v1`. Select the file explicitly; this package does
not search another repository for settings. Configuration controls local paths,
command execution during apply, and generated Bash function names, so treat it as
trusted executable policy. Credential files are located, never opened by a path
query or included in its output.

A small example:

```json
{
  "schema": "runtime-layout/v1",
  "root": {
    "default": "~/.sample-runtime",
    "environment": "SAMPLE_RUNTIME_ROOT"
  },
  "repository": {
    "environment": "SAMPLE_REPOSITORY_ROOT",
    "branch": "main"
  },
  "paths": {
    "root": {"kind": "root"},
    "read_token": {
      "kind": "file",
      "path": "{root}/credentials/read.json",
      "legacy": ["~/.sample-legacy/read.json"],
      "environment": ["SAMPLE_READ_TOKEN"]
    },
    "progress": {
      "kind": "content",
      "arguments": 1,
      "path": "{root}/state/{0}",
      "legacy": ["~/.sample-legacy/{0}"]
    },
    "writer_lock": {
      "kind": "active",
      "arguments": 1,
      "path": "{root}/locks/{0}",
      "legacy": ["~/.sample-legacy/{0}.lock"]
    }
  },
  "shell_functions": {
    "sample_root": "root",
    "sample_progress": "progress",
    "sample_lock": "writer_lock"
  }
}
```

Path templates support `~`, `$HOME`, `${HOME}`, `{home}`, `{root}`,
`{repository}` and numbered arguments such as `{0}`. Other `$VARIABLE` text is
literal; declare overrides in `environment` instead. A nonempty override wins.
Python expands `~` in overrides by default; `expand_override: false` preserves
its literal spelling. Bash preserves override spelling, including `~`, matching
ordinary shell variable expansion. Use absolute override values for portability.

`root.empty_environment` is `value` by default: an explicitly empty variable is
a Python `Path('.')`. `root.activation` defaults to `present`. Bash defaults to
`shell_empty_environment: default` and `shell_activation: nonempty`, preserving
shell `${VARIABLE:-default}` semantics. These compatibility choices can be set to
`default`/`nonempty` for matching Python behavior. Ordinary nonempty overrides and
unset variables agree across both interfaces. Migration refuses a relative root.

| Path kind | Selection |
|---|---|
| `root` / `active_flag` | Runtime root / selected activation predicate |
| `file` | Existing new target, then existing `legacy` entries, otherwise new target |
| `active` | New target when the root is active, otherwise the single legacy target |
| `content` | Nonempty new directory, then nonempty legacy directory, then activation rule |
| `glob` | First new/legacy directory containing `pattern`, otherwise new directory |
| `fixed` | Configured target regardless of existence |
| `alias` | Another named path, optionally with its own environment override |
| `repository` | Explicit override, otherwise configured branch from `git worktree list`, otherwise the supplied source directory |
| `sibling` | `name` beside the selected repository root |

The `arguments` count must be a nonnegative integer. New and legacy templates are
separate so read and write credentials can stay distinct. `legacy_note` may
contain `{path}`; `shell_legacy_note` optionally preserves a caller's existing
shell diagnostic. Python emits a legacy-path notice once per selected path.

Python callers use `Layout.load(config, repository_source=source)`, then
`resolve(name, *arguments)`. The generic `root`, `active`, `repository`,
`resolve_file(new_relative, legacy, env)`, `active_path(new_relative, legacy)` and
`active_dir(new_relative, legacy)` methods support existing adapters. A shell
binding can map to `@file`, `@active`, `@content` or `@note` for those primitives.
The repository's default branch is queried once when bindings are generated;
explicit repository and runtime overrides remain dynamic afterward.

## Explicit migration

Add a `migration` object. It declares all scope; there is no built-in account,
service, directory or lock inventory.

- `locks`: ordered `{legacy, current}` absolute path templates. Each current
  lock is inside the root and each legacy lock outside. Include every writer
  which can access selected files; order nested writer locks before their shared
  publication lock. Default `lock_timeout` is 600 seconds per lock.
- `directories`: relative directories to create. `private_directories` selects
  directories whose mode becomes 0700, defaulting to the root itself.
- `items`: objects with `kind`, `source`, `destination`. `move` renames one entry;
  `contents` moves immediate children including dotfiles; `glob` expands selected
  source files and substitutes `{name}` in the destination; `directories` expands
  selected source directories and moves their immediate children. `exclude` lists
  basenames excluded from the last two modes. `worktree` additionally requires
  `repository` and uses Git's move and repair commands, preserving dirty output.
- `services`: named objects with `active`, `stop`, `start` argument arrays and
  optional `inactive_codes` (default `[3]`). An item selects one with `service`.
  Only a previously active service is stopped and restarted. Every stopped
  service gets a restart attempt even if another restart fails.
- `symlinks`: `{path, old_prefix, target}` records for replacing an existing
  matching cache link. Other links and regular files remain unchanged.
- `empty_directories`: old directories removed only when empty.
- `after_commands`: explicit argument arrays, for example a caller's schedule
  installer. They execute only after successful data movement.
- `environment`: default values for absent command environment variables.
  Migration templates additionally accept `{uid}`. `command_timeout` defaults
  to 30 seconds. Commands never run through a shell unless the caller explicitly
  selects a shell executable.

Planning reads metadata, not credential or state contents. It rejects existing
destination conflicts, parent/child move overlap, unregistered or locked Git
worktrees, symlink destination ancestors, cross-filesystem moves, and distinct
existing old/new lock inodes. A diagnostic and nonzero exit require inspection;
there is no overwrite or automatic historical-state repair mode.

On first activation, the tool acquires the old locks and prepares new lock paths
as hard links to those same held inodes in a temporary sibling directory. Moves
are prepared there before an atomic root rename. Old and new lock paths continue
to refer to one inode afterward, so existing waiters and newer writers remain
serialized. It never unlinks or replaces an existing lock to establish exclusion.

A caught failure before activation restores completed moves to their old paths;
failed restoration retains the staging contents for inspection. A process killed
without cleanup can also leave staging contents: do not start another migration
or remove that directory before inspecting the original paths and writer state.
After activation, data remains at the complete new layout even if Git registration
repair or a post-command fails; rerunning the same configuration can finish those
steps without regenerating data. A failed service restart is reported, and no
other service's restart is skipped. This is a bounded local migration tool, not
a crash-proof transaction coordinator or a service-discovery system.
