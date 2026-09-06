---
name: whatsapp-archive
description: Receive and archive WhatsApp messages through an explicitly configured linked-device spool. Use for pairing or operating the receive-only bridge, listing or inspecting spooled chats, rebuilding selected monthly Markdown archives, or running scheduled publication through an external publisher. Does not send messages or mark chats read.
---

# WhatsApp archive

Use this package's `scripts/sync` entry with an explicit private YAML config.
Read [references/config.md](references/config.md) for configuration, setup,
publication, and the spool contract before installing or changing a job.

```bash
scripts/sync --config /path/to/private.yaml --doctor
scripts/sync --config /path/to/private.yaml --list-chats
scripts/sync --config /path/to/private.yaml --peek "Example" --peek-limit 30
scripts/sync --config /path/to/private.yaml --dry-run
scripts/sync --config /path/to/private.yaml --publish
```

Keep credentials, chat selections, raw messages, storage paths, service units,
and schedules outside this package. Use synthetic inputs in tests and examples.
The bridge receives all messages locally; selection controls only what enters
the Markdown archive. Empty whitelist selects nothing. Removing a selection
does not delete previously archived content.

Pair only at the account owner's request. A migration preserves the existing
store, pairing, and device identity. Stop the previous bridge before starting
another against that store. Never add sending, read-receipt, or group-mutation
operations. Media is retained as metadata and captions, without binary downloads.

Run the package's checks after changing behavior:

```bash
uv sync --locked
uv run --no-sync pytest tests -q
uv run --no-sync ruff check src tests --select E4,E7,E9,F
npm ci --prefix bridge
npm test --prefix bridge
```
