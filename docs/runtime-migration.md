# Migrating runtimes into independent Skills

A migration is complete when the public package contains the working capability
and its consumers use that package. Moving only `SKILL.md` while leaving the
implementation or required helper libraries in a private repository does not
achieve this. Each machine still needs its own installation and verification;
a merged change does not update every existing caller.

## Separate behavior from private choices

Keep implementation, executable entries, tests, dependency manifests, lockfiles
and supporting references inside `skills/<name>/`. Verify that the package works
when copied without its siblings or the private repository. Integrations use
explicit external commands or installed dependencies, rather than locating
another package's source tree.

| Public package | Caller-owned configuration or policy |
|---|---|
| API clients, pagination, parsing and rendering | Accounts, selected sources, destination paths and exclusions |
| Incremental processing and recovery | State locations, existing checkpoints and pending outputs |
| Worktree and publication mechanisms | Repository, branch, owned paths, writer identity and locks |
| Model invocation and input reuse | Model/provider selection, credential references, prompts and spending limits |
| Installation and link operations | Package selection, schedules, service commands and hook settings |
| General validation mechanisms | Repository-specific content rules and commit metadata |

Credentials and source content stay private. Configuration should point to
credential storage without embedding credential values. A private policy command
may remain when it expresses actual repository rules; it should not retain a
second generic publisher, scheduler or client implementation.

Review the exact publication set before copying it into a public package.
Use synthetic examples, fixtures and identities. Check error messages, comments,
test metadata and dependency files as well as the main source. Removing a
username from a path does not make deployment history or private source layouts
appropriate for publication.

## Inspect current callers before deleting entries

Inspect the current checkout and the actual installation on each affected
machine: crontab entries, command links and launchers, user services, harness
hooks, installed configuration, and Python or shell imports. Read configuration
without exposing credential contents. Keep the search bounded to executable
and configuration locations; archived conversations are not a call graph.

Classify matches before acting:

- An active command or import needs to point at the public interface.
- An installer's exact old-command match may remain to remove a retired cron
  entry during an upgrade, even after that executable is deleted.
- Tests for a removed forwarding layer can be deleted; tests for private
  selection and public integration still matter.
- Historical documentation or a backup launcher is evidence of an earlier
  installation, not proof that a compatibility entry is still required.

Absence on one machine does not establish absence elsewhere. Give other owners
explicit upgrade instructions and the required public version. Preserve any
compatibility interface required by the affected package's contract; otherwise
delete obsolete forwards after updating callers, instead of maintaining another
permanent dispatch layer.

## Prefer a directly consumed private configuration

Before retaining a configuration generator, check whether the public loader can
already read the required settings. Static source selection, prompts, paths and
publication arguments usually belong in one private configuration. Remove the
old copy of a setting when moving it, so two files do not become competing
authorities. Keep an exporter only for necessary behavior the native interface
cannot express.

Read the selected package's path contract. Support for `~`, environment
variables, relative paths and command placeholders differs between packages
and sometimes between fields. A path relative to the configuration file may be
supported while the same text inside an argument array is passed literally.
Some loaders resolve a configuration symlink before interpreting relative paths;
others do not. Do not assume changing the working directory fixes either case.

The existing `profiles` array in
[runtime-install](../skills/runtime-install/references/configuration.md) also
accepts caller-selected configuration files. This synthetic example installs a
package discovery link and a JSON configuration link:

```json
{
  "schema": "runtime-install/v1",
  "kind": "skills",
  "lock": "/example/state/install.lock",
  "packages": {
    "example-tool": {
      "source": "/example/packages/example-tool",
      "required": [
        {"path": "SKILL.md", "kind": "file"},
        {"path": "scripts/run", "kind": "executable"}
      ]
    }
  },
  "destinations": ["/example/client/skills"],
  "profiles": [
    {
      "source": "/example/private/example-tool.settings.json",
      "destination": "/example/config/example-tool/config.json"
    }
  ]
}
```

Replace the example paths with explicit private locations. Installation paths
must be absolute after `~` expansion; this installer does not expand arbitrary
environment variables in those fields. Sources must already exist. Preview with
the installed package's entry:

```sh
"$RUNTIME_INSTALL_ROOT/scripts/skills" --config /example/private/install.json --dry-run
```

Set `RUNTIME_INSTALL_ROOT` to the selected package directory. The preview creates
no links or directories. For `profiles`, existing regular files and links to
other sources are preserved. Review a retained custom configuration explicitly;
successful installation does not mean it was replaced by the proposed file.
Discovery links have different ownership rules, documented in the package.
Keep access permissions appropriate for private configuration and its parent
directories; a symlink does not restrict access to its target.

## Compare meaning before switching

Load the old and proposed configurations under the intended runtime environment
and compare their resolved values, not just their text. A useful comparison
covers:

- credential references and read/write scope separation;
- selected source identities, exclusions, ownership and output boundaries;
- state, pending-publication records, reusable generated output and filenames;
- task locks, publication locks, installer locks and the scheduled writer;
- publication arguments, private validators and commit-message commands;
- model/provider choice, prompts, budgets, timeouts and no-work behavior.

Do not silently narrow source selection or grant another machine writer access
while changing implementation. Keep one active writer for each owned output.
A failed source read must follow the explicitly chosen failure policy; treating
an error as an empty successful result can discard data or advance progress.

When changing HOME or repository location, inspect every resolved path and
installed command again. Quote shell paths, including paths containing spaces;
argument arrays avoid shell quoting only when the consumer executes them
directly. Regenerate installation artifacts that intentionally contain absolute
paths. Git worktree metadata, service commands and already-running processes can
still refer to the old location after a directory move.

Move durable state deliberately, with the relevant writers stopped or locked.
Two path spellings of a lock must not create two independent locks. The optional
[runtime-layout migration command](../skills/runtime-layout/SKILL.md) supports
an explicit configured move and read-only preview; it does not discover the
right accounts, sources or services to migrate.

## Verify recovery and activate the actual installation

Run package-owned checks using isolated HOME and configuration, synthetic inputs,
fake external commands and local test repositories. Ensure tests cannot fall
back to real credentials or a real model. Cover failure propagation, incomplete
reads, publication rejection and recovery of already-generated output. Compare
unchanged inputs and outputs where compatibility is required. A dry run or doctor
has only the guarantees documented by that command; some inspection commands
contact the configured service.

For repository writers, verify separately that publication succeeded and that
durable progress advanced only after the required checks. Preserve pending
verification records and persistent generated results until recovery completes.
[repository-publish](../skills/repository-publish/SKILL.md) distinguishes
reproducible temporary transactions from existing persistent worktrees. Do not
delete a pending draft or reset a checkpoint merely to make a retry look clean.

Preview installation against the current machine. For managed cron changes,
retain unrelated jobs, preserve the chosen schedule and configuration selection,
and share the installer's lock with other crontab writers. Narrow old-command
matches enough to preserve another repository or configuration using the same
public executable. Configured pre-install commands may have external effects;
the installer cannot undo those effects when a later step fails.

After authorized installation, read back the actual links, crontab, service
commands and hook configuration. A configuration file on disk does not prove a
running service or harness has loaded it. Use the applicable reload, restart or
hook trust procedure, then observe the new entry in the real invocation path.
Do not equate a process being present, a successful skip, or a doctor result with
successful execution of the intended task.

Keep private rollback evidence tying together code versions, installed commands,
configuration, output and state. Before reverting, check state compatibility
and stop the replaced writer so two versions cannot write concurrently. Record
what was actually checked and which machines remain unverified. Leave changing
deployment inventories and run results in private evidence, rather than turning
this public guide into a deployment log.
