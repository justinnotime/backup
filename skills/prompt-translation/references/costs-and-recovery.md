# Costs and recovery

Run `scripts/translate --config FILE --dry-run` with no date or file limit to
estimate the selected archive without reading credentials, writing files or
calling a model. The report lists source and work-unit counts, approximate input
and output tokens, batch counts and configured price estimates. When using
`--cheatsheet-only --dry-run`, it estimates the selected sample separately.

The token estimate treats each Chinese character as approximately one token and
four other characters as approximately one token. For an illustrative archive
with one million retained Chinese characters, translation alone is on the order
of one million input tokens and one million output tokens, before system-prompt
overhead, retries and an optional classification pass. This is a sizing example,
not a measurement of the caller's archive. Classification estimates assume half
of retained prompts proceed to translation; actual model decisions can differ.
The estimate is approximate and does not promise a final bill.

Repeated system prompts include ephemeral prompt-cache metadata. Translation and
classification process batches. Malformed multi-item JSON responses are retried
in smaller batches; malformed single-item responses fail. Transport retries use
the explicit API retry setting. Retries can incur additional cost.

One UTC day's ledger is resolved before replacing that day's output. A failed
day leaves its prior output untouched. Days that completed before a later failure
remain on disk. Daily ledgers record stable identities, input hashes, decisions
and English translations, so retrying unchanged completed days does not repeat
their model calls. Individual batches from a day that never completed are not
persisted and may need translation again.

The scheduled entrypoint uses a persistent worktree and a configured external
publisher. It can retain completed paid results after a failed push or a failed
later day. Its configuration and recovery contract are documented in
[scheduled operation](scheduled.md). The package does not install a schedule or
choose private validation and commit policy.
