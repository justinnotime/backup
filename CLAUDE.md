# Repository guidance

`CLAUDE.md` is the canonical repository contract. `AGENTS.md` must remain a
relative symbolic link to this file so every supported agent reads one copy.

Make and verify changes in a sibling task worktree. Pull requests may be used
when CI runs the checks for the affected Skills; inspect actual results before
reporting success. Without working CI, publish verified, authorized changes
directly to `main`. Initial CI setup and explicitly authorized direct updates
may also use that path. Synchronize the clean primary checkout by
fast-forwarding. Do not import another repository's approval workflow here.
Keep the existing GitHub account identity and its public-safe commit email.

For requests about additional harness roots, scheduled backups, exclusions,
destination compatibility, or Syncthing layout, read `PROFILES.md` before
changing files. Run `bash skills/state-backup/tests/run.sh` and its package-local
Python tests after changing state-backup behavior.

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

Every Skill owns its implementation, tests, dependency manifests, lockfiles,
and supporting documentation inside `skills/<name>/`. A package must work when
copied without its siblings. Do not import or load source files from another
Skill or depend on repository-root source or test directories. An optional
external command is configured by its public executable interface, never by
discovering a sibling package's source layout. Root compatibility links remain
supported, but do not add root runtime frameworks or source compatibility links.

Run checks from the affected package using its own dependencies. The root
README lists package commands; it is not a shared test runner or dependency
project. Keep generated test output and environments outside tracked sources.

The GitHub Actions workflow only coordinates package-owned checks. Run tests
on GitHub-hosted runners with synthetic inputs, read-only repository access,
and no personal credentials or source directories. Copy each tested package
without its siblings so CI also checks package isolation. Shell syntax checks
alone do not establish that a clipboard or Syncthing integration works.
