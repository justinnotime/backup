---
name: teams-archive
description: Archive, list, or read selected Microsoft Teams chats using caller-owned configuration, including message cards and optional attachments. Use for repeatable chat exports and archive diagnosis; sending messages is outside this Skill.
---

# Teams Archive

This package contains its own program, dependencies, and tests. Select chats,
authentication, output, and state locations in a private configuration file.
Read [the configuration reference](references/config.md) before configuring a
new consumer. Never infer an account, tenant, or output location from the host.

Install and run from this package directory:

```bash
uv sync --locked
scripts/sync --config /path/to/private/config.yaml --dry-run
scripts/sync --config /path/to/private/config.yaml
```

Use `--list-chats` to inspect available chat identities and `--peek MATCH` to
read recent messages. An exact `19:` chat ID avoids listing all chats. Ambiguous
names are reported for clarification. `--login` explicitly starts Graph device
login with the configured public client and tenant.

The archive retains the first captured message text and deduplicates by message
ID. It is an archive, not an exact replica of later edits or deletions. Monthly
files, selected stable directory names, cards, and attachment manifests remain
compatible across runs. Keep the same config and state for repeated execution.

`--dry-run` reads remote messages and computes pending message additions, but
does not save archives, synchronization state, chat registries, authentication
caches, debug dumps, or attachments. It cannot verify attachment downloads.
Ordinary list/peek operations may refresh authentication and chat metadata;
combine them with `--dry-run` when no local writes are wanted.

When attachments are enabled, the configured `gsk` command downloads them via
its Teams connector and temporary AI Drive relay. This requires an already
authorized connector and relay account. The program removes each temporary
relay file after download. It never sends chat messages. An attachment failure
fails the run so synchronization state is not advanced past missing content.

Incomplete listings or message pages return nonzero without saving new sync
state. Earlier files from a failed run may remain; retrying deduplicates them.
Consumers publishing to Git should run in their own transaction worktree and
promote the separately staged state only after publication succeeds. This
package does not schedule jobs, commit, push, or manage another repository.

Validate changes with `uv run --locked pytest tests`. Tests use synthetic inputs
and require neither real credentials nor neighboring Skill packages.
