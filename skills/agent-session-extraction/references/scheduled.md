# Scheduled extraction

`scripts/run --config /path/to/private/schedule.json` invokes the same
extraction API as `scripts/extract`. Its external publisher is selected by
configuration. No consumer repository, source directory, account, schedule,
lock location, or publication convention is built into the package.

The schedule JSON contains:

| Field | Meaning |
|---|---|
| `schema_version` | `agent-session-schedule/v1` |
| `manifest` | Absolute path to the private source manifest |
| `repository_root` | Absolute source checkout, matching the manifest |
| `publication.command` | External publisher command as an argument array |
| `publication.output_root_environment` | Variable in which that publisher supplies its output worktree |
| `environment` | Optional explicit environment overrides for the publisher |
| `failure_marker` | Optional absolute path for a sanitized failure report |
| `validate_command` | Optional argument array validating the generated output |

The publisher receives the extraction command appended to its argument array.
For a publisher using `--` before its writer, include that delimiter at the end
of `publication.command`. Arguments are passed directly, without shell
evaluation. A shell can be explicitly selected as an executable when needed.

Command arguments support `{manifest}`, `{repository_root}`, `{owned_paths}`,
and, for validation, `{output_root}`. The owned paths come from the manifest;
they are not independently copied into the schedule. `{owned_paths}` expands
to one space-separated argument and therefore accepts only unambiguous path
components. Source selection, node ownership, cleanup, indexes, redaction,
retention, and output naming remain in the existing manifest.

The publisher creates a linked Git worktree, invokes the appended command,
and commits/pushes only after that command succeeds. The extraction command
refuses the main checkout and worktrees belonging to another repository.
The external publisher owns locking, Git conflict handling, repository policy,
attachment delivery, and progress-state publication. Existing publisher
implementations can be reused without copying their private policy here.

The manifest must use `filesystem-atomic`: the publisher owns Git publication.
Scheduled extraction requires redaction, output auditing, reconciliation, and
prepublication scanning. Failure in extraction or configured validation must
abort the publisher. Publisher failures produce a nonzero exit status and the
configured failure marker without relaying its potentially sensitive output.

`--doctor` checks the manifest and source capabilities without executing the
publisher. `--dry-run` performs the extraction plan against existing output;
it does not create a worktree, write a marker, or execute the publisher.

```sh
scripts/run --config /path/to/private/schedule.json --doctor
scripts/run --config /path/to/private/schedule.json --dry-run
scripts/run --config /path/to/private/schedule.json
```

Keep real schedule files and credentials outside this package. Test with
synthetic transcripts and publishers operating only on temporary repositories.
