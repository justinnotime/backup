# Explicit Git hook installation

The package can install links to caller-selected executable hooks. It also
provides a generic `pre-commit` guard; repository policy stays in configuration.
Neither command discovers other repositories or installs hooks globally.

```sh
scripts/git-hooks --config /private/git-hooks.json --dry-run
scripts/git-hooks --config /private/git-hooks.json
```

A synthetic configuration:

```json
{
  "schema": "runtime-install/v1",
  "kind": "git-hooks",
  "repository": "~/projects/example",
  "lock": "~/.local/state/example/install-hooks.lock",
  "backup_directory": "~/.local/state/example/hook-backups",
  "main_guard": {
    "when_environment": "EXAMPLE_AGENT_SESSION",
    "bypass_environment": "EXAMPLE_OPERATOR_OVERRIDE"
  },
  "hooks": [
    {
      "name": "pre-commit",
      "source": "/path/to/runtime-install/scripts/main-worktree-guard",
      "replace_sha256": [],
      "replace_targets": []
    },
    {
      "name": "prepare-commit-msg",
      "source": "/path/to/optional-attribution-command"
    }
  ]
}
```

The source must already be executable and accept Git's hook arguments. A linked
command must locate its package through the resolved script path. Configuration
uses the package's normal environment and home-directory expansion. The optional
attribution program is an explicit external command; this package never loads
another package's source files.

The selected repository's effective hooks directory comes from Git. By default
it must be that repository's common Git directory plus `hooks`. If `core.hooksPath`
selects another location, include its exact absolute path as `hooks_directory`
to acknowledge that shared destination. The installer never changes
`core.hooksPath`. A caller can remove a redundant local absolute setting after
checking it equals Git's default directory; this avoids retaining an obsolete
directory after a move. A custom shared directory can affect other repositories,
so inspect their configuration before approving it.

Absent hooks are linked. A link already pointing at the selected program stays
unchanged. Other files and links are preserved unless the caller explicitly lists
their exact SHA-256 in `replace_sha256` or their raw link target in
`replace_targets`. A filename or a script comment is not sufficient proof of
ownership. Review the preview for `managed: false`: such an existing hook was
preserved and the selected replacement is not active. The installer does not
chain a foreign hook or silently disable it.

Real installation rereads under the configured lock, validates every source
before mutation, writes a private backup directory and verifies the installed
links. A later failure restores earlier changed hooks and the previous guard
policy. Backups include original regular files with their modes and a
`snapshot.json` recording original symlink targets, absent entries and the old
guard setting. Successful installation prints that backup directory. To roll
back manually, stop concurrent hook installers, restore the named hook files or
symlinks from this snapshot, remove newly created hooks, and restore the saved
`runtimeinstall.mainGuard` values in local Git configuration. Preserve unrelated
hooks and configuration. Preview creates no locks, backups or directories.

## Main-worktree guard

`scripts/main-worktree-guard` runs directly as a `pre-commit` symlink and reads
the `runtimeinstall.mainGuard` JSON object from the committing repository's local
Git configuration. The installer writes it only when its configured `pre-commit`
is installed or already managed. A missing or malformed policy refuses the
commit instead of silently disabling an installed guard.

The protected checkout is the explicit installation `repository`. The installer
records its path relative to Git's common directory as `worktree` in the local
policy; it does not store a username or home path. The hook compares that location
to the current checkout using Git's metadata. Changing branches or detaching HEAD
does not remove protection. Other worktrees remain permitted.

The guard refuses commits in the protected checkout when `when_environment` is
nonempty in the process environment; set that field to `null` to cover every
invocation. `bypass_environment` allows an explicit nonempty override, or can be
`null`. These choices are private policy. The guard does not restrict worktree
creation or Git synchronization. Moving the common Git directory and checkout
together preserves their relative path; after moving only the checkout, rerun
the installer with its new explicit repository location before using it.

When copying this installation to another machine, install the public programs,
review that machine's private repository and environment selections, inspect its
existing hooks and run the preview before applying. Existing regular copies do
not update when their former source changes. Each participating repository needs
its own local guard policy even if it shares a hooks directory.
