# Cost and verification

Normal generation makes one Claude CLI invocation per selected, changed daily
date and one per changed weekly window. Hash-matching summaries require no model
call. Retrying pending publication also requires no model call. Each invocation
receives the caller's exact argument list, including its explicit budget cap and
model/fallback selection. This package does not invent model prices or replace
an account with an ambient API key.

Dry-run token estimates use request characters divided by three. This is an
order-of-magnitude planning approximation, not provider usage: one million input
characters is roughly a few hundred thousand tokens. For example, one hundred
selected daily requests of about twenty thousand tokens each would be on the
order of two million input tokens before caching. Tool reads, output, tokenizer
differences and retries can increase usage; prefix caching can reduce billed
input. Run dry-run over the actual selected dates and inspect the configured
per-call budget before authorizing a full history regeneration.

Put reused instructions first and variable records at `{{inputs}}` last. The
selected CLI/provider controls prompt caching; the package does not claim cache
hits it cannot observe. Daily facts and weekly inputs are already batched into
one request per output. Date-level output hashes are the durable reuse mechanism.

The package is independently installable and testable:

```sh
uv run --project /path/to/activity-summary --locked --group dev pytest -q
uv run --project /path/to/activity-summary --locked --group dev ruff check /path/to/activity-summary
uv run --project /path/to/activity-summary --locked --group dev ruff format --check /path/to/activity-summary
```

Tests use synthetic archives, local Git repositories and synthetic model
executables. They cover exact source timestamps, reference identity, deterministic
rendering, candidate validators, source-hash reuse, publication failure recovery,
model failures/timeouts, isolated account environments and zero-call inspection.
They do not authenticate or call a real model service. Separately verify any
private source/configuration migration by comparing fact bytes and weekly input
bytes at the same repository version before switching an existing schedule.
