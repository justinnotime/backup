---
name: google-chat-archive
description: Read and archive Google Chat spaces, group chats, and direct messages selected by private configuration. Use to list accessible spaces, inspect recent messages, or maintain a monthly Markdown archive with an existing authorized-user OAuth credential. Does not send, edit, or delete Google Chat messages.
---

# Google Chat archive

Use this package's `scripts/sync` from its installed directory. Read
[configuration](references/config.md) when setting up credentials, relocating
the archive, or integrating a scheduler. Configuration must be explicit; never
infer an account or conversation selection from the machine or repository.

```bash
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml --doctor
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml --list-spaces
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml --peek "spaces/EXAMPLE" --peek-limit 30
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml --dry-run
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml
```

`--doctor`, `--list-spaces`, `--peek`, and `--dry-run` make remote reads without
changing archive files or state. Doctor samples message and membership access
in one space if any are visible. Dry run fetches the configured incremental
window and reports fetched messages; that count includes overlap and is not a
count of new archive entries.

Normal sync appends messages with stable ID markers to monthly Markdown files.
It preserves recorded directory names and clamps receive progress to archived
content. Attachments and cards are represented by placeholders; attachment
bytes, edits, deletions, and messages outside the selected history window are
not mirrored. This package performs no model calls.

Authentication errors, failed API reads, and incomplete space inventories fail
the run. Message page limits may produce a successfully archived oldest prefix;
subsequent runs continue from that prefix with overlap. State is saved only at
the end. A failed run can leave partial local files, which the next run deduplicates.

For Git publication, supply an isolated output directory and staged state file
to the existing publisher. That publisher must promote state only after the
archive is committed and pushed. Keep one writer per archive/state pair. This
package has no Git or scheduling policy of its own.
