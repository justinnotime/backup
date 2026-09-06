# Scheduling, input hashes and recovery

```sh
scripts/run-daily --config /private/config.json
scripts/run-daily --config /private/config.json --target 2024-01-02 --force
scripts/run-daily --config /private/config.json --max-dates 2
scripts/run-weekly --config /private/config.json --end 2024-01-07
```

Daily `--target` accepts a completed UTC date or `yesterday`; `--force` requires
an explicit target. Without a target, missing outputs and recent hash-bearing
outputs are selected chronologically. Existing legacy outputs without a hash
are left alone unless explicitly forced. The maximum candidate count is applied
before unchanged-hash candidates are skipped, preserving existing selection
behavior. The default target is yesterday for weekly, and its input window is
the seven completed dates ending on `--end`. Weekly `--force` bypasses the hash
shortcut for that window.

`--doctor` checks local configuration and executable availability without
invoking the configured CLI or account-status command. `--dry-run` reads the main
repository and reports selected inputs and estimates without locks, worktree
preparation, network fetches, account checks, model calls, publication or writes.
It does not wait for missing weekly inputs.

Daily facts preserve `json.dumps(ensure_ascii=False, indent=2)` plus exactly one
newline as their hashed bytes. The daily summary embeds `facts_sha256` and the
three-day facts window. Weekly hashing concatenates existing daily files in date
order using these exact bytes before each file, then its unmodified file bytes:

```text
\n===== DAILY YYYY-MM-DD (relative/output/YYYY-MM-DD.md) =====\n\n
```

Missing daily dates are excluded from the concatenation and explicitly listed.
No daily inputs is a failure without a model call. Normal weekly runs can wait
for missing inputs, refreshing the clean worktree at most once a minute until
the configured wait expires. Existing weekly `inputs_sha256` avoids a new call
when the closed input set is unchanged.

## Persistent output and publication

Both runners prepare the configured persistent worktree through the external
publisher, recover any interrupted owned output using content validation, and
publish pending commits before generating anything. This prevents a failed push
from causing a second paid generation. Each valid new daily output is committed
and published before the next date is attempted; failure on a later date returns
nonzero while keeping earlier published work. The weekly runner uses the same
recovery behavior for its one complete result.

Candidates are validated before atomic installation. Model failure, malformed
response or invalid output does not replace an existing summary. Successful
generation embeds its durable source hash in the output; there is no separate
uncommitted progress database. Do not delete a worktree with unpublished output.

The publisher fetches/rebases and then calls this package's content validation
again against the updated sources before invoking the private validator.
On source drift, only the explicitly configured recovery policy may discard
generated paths. Daily reports the deferred count and continues the remaining
selected dates, returning zero after those are handled. Weekly returns nonzero
after discarding the obsolete candidate. Push, private-validation, commit and
unexpected-path errors preserve output and return nonzero.

The public runtime never deletes unrelated files or resets Git itself; all Git
worktree/publication operations are external publisher commands. Recovery is a
separate, explicit caller policy. Any interruption between atomic installation
and commit leaves the complete file in the worktree for the next run. An abrupt
kill during temporary-file writing can leave an unowned temporary file; the
runner stops for inspection rather than silently deleting it.

## Private policy contract

Every policy argv supports `{repository}`, `{worktree}`, `{scope}` and `{kind}`.
`scope` is `worktree` for uncommitted paths or `committed` for unpublished commits;
`kind` is `daily` or `weekly`.

| Command | Required behavior |
| --- | --- |
| `validate_command` | Read-only ownership and repository-specific checks; exit 0 to permit publication |
| `commit_command` | Commit only validated owned paths; exit 0 or 2 for no changes; must leave a clean worktree |
| `message_command` | Print the complete commit message with current caller-required trailers; the publisher can refresh it after rebase |
| `recover_command` | Independently verify ownership and branch, then restore only invalid generated paths or discard only their unpublished commits |

Content hashes and structural validators remain public. Local lint baselines,
commit acknowledgements, author identity and allowed recovery paths remain
private. Publication validation runs before message refresh; do not reject an
otherwise valid commit merely because a rebase made its previous acknowledgement
stale. The message command must generate the current required metadata.

Policy commands receive the caller environment plus explicit top-level
`environment` additions. Claude and authentication commands receive only their
explicit schedule environment. They are separate because private Git policy may
need repository-specific settings while account selection must be predictable.
