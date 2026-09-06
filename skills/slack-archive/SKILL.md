---
name: slack-archive
description: Archive, list, or read Slack channels and direct messages using caller-owned configuration, including late replies to old threads. Use for repeatable message exports and archive diagnosis; this package never sends messages.
---

# Slack Archive

This package owns its program, dependencies, and tests. Read the
[configuration reference](references/config.md) when configuring a consumer.
Keep account tokens, workspace selection, output, state, and scheduling private.

From this package directory:

```bash
uv sync --locked
scripts/sync --config /path/to/private/config.yaml --dry-run
scripts/sync --config /path/to/private/config.yaml
```

For a configured Git consumer, the scheduler can call this same public entry
with `--publish`. The private `publish.command` selects the existing
transactional publisher; no private forwarding script is needed. Read the
publication section of the configuration reference before configuring it.

Use `--list-channels` to inspect available conversation identities. Use
`--peek MATCH --peek-limit 30` for recent messages; ambiguous matches fail.
An exact conversation ID or `workspace/ID` selects one conversation.
List and peek do not write archives or state. `--dry-run` computes pending
archive additions without creating output directories or saving state.

The archive retains the first captured text, sender rendering, monthly filenames,
and message IDs. Later edits and deletions do not replace the original record.
Files are represented by their names; this package does not download file bytes.

Every sync scans all retained parent history in selected active conversations,
including parents older than the archive's message range. It reads new replies
in those threads even when the parent had no replies on the previous run.
This costs history requests proportional to retained parent history. Unchanged
threads with a `latest_reply` older than the last completed scan need no reply
request. Page size and request interval are caller-configurable; rate-limit
responses are retried with the server's delay.

Missing credentials, failed API calls, malformed pages, or unfinished pagination
return nonzero without saving synchronization progress. A caller publishing
archives to Git should use its own transaction worktree and staged state, and
promote state only after publication succeeds. Earlier output files may remain
after a standalone failure; retries deduplicate their message IDs.

Run package tests with `uv run --locked pytest tests`. Tests require synthetic
inputs only. This package does not install schedules or implement Git publication;
`--publish` explicitly invokes the configured external publisher. It never reads
another Skill's source files.
