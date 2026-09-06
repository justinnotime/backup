---
name: teams-send
description: Preview and send user-authorized messages to an existing Microsoft Teams chat, including configured markers and optional connector-based mentions, images, or quoted replies. Use for an explicit Teams send request, target lookup, or management of a pending send proposal. Reading or drafting alone does not authorize sending.
---

# Send a Teams chat message

Resolve the existing chat and preserve the user's intended message. The default
`send` command is a preview. A current explicit request to send a specified
message supplies authorization; do not ask for the same approval again. If the
user requested only a draft or the recipient/content remains uncertain, resolve
that uncertainty before using `--yes`. The tool flag is the caller's assertion
of authorization, not an independent permission check.

Use this package's `scripts/send` or installed `teams-send` command. Configuration
comes from `--config`, `TEAMS_SEND_CONFIG`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/teams-send/config.json`. Read
[configuration](references/config.md) when setting up or changing the integration.
Python 3.10 or newer, MSAL, and requests are required. The optional connector is
an external executable selected by private configuration.

```bash
/path/to/teams-send/scripts/send --config /private/config.json doctor
/path/to/teams-send/scripts/send --config /private/config.json chats "<topic-or-person>"
/path/to/teams-send/scripts/send --config /private/config.json send --chat-id "<exact-id>" -m "<message>"
```

`chats` uses the local registry, refreshing it when absent or when `--refresh`
is requested. A substring target must resolve to exactly one chat. Use its exact
ID when a person appears in several groups. Review the resolved target and text;
add `--yes` only for an authorized send. Recheck time-sensitive claims before
sending. This package sends to existing chats; it does not create chats or manage
membership.

Direct Graph sends use the separate delegated send-token cache. Login is an
explicit `login` operation for a configured client; do not start it merely
because an unrelated read or preview failed. The configured marker is added at
the send boundary. Sends use the authenticated account, not an invented author.

`--via gsk` selects the configured external connector. It is required for
`--image`, `--reply-to`, and `--mention`. Each mention needs a `{mention_N}`
placeholder, numbered from zero, and must resolve to one chat member. Images
are encoded into the connector's HTML payload; its command-argument size limit
is checked before invoking it. Text is escaped and rendered into blocks, lists,
links, and bold spans.

For an unattended proposal, use `propose`, `list`, and `reject`. `approve` shows
the stored proposal on an interactive terminal and requires a typed `yes`.
Do not simulate that confirmation. Expired proposals are excluded from pending
results; listing them does not delete their stored files.

Success requires a platform message ID. Sends are not automatically retried.
If the result is unconfirmed, inspect the destination before trying again.
If delivery succeeded but local recording failed, the command prints the message
ID with `DELIVERED` and exits 2; repair the record without resending. Normal
success exits 0; configuration and operation failures exit nonzero. `doctor`
checks local configuration and file/command availability, not live account
permissions or successful delivery.

Private presentation or identity-mapping preferences may be read from
`TEAMS_SEND_PROFILE` or `${XDG_CONFIG_HOME:-$HOME/.config}/teams-send/profile.md`
when present. Current user instructions take precedence.

Development: `uv run --project /path/to/teams-send --locked pytest /path/to/teams-send/tests -q`.
Tests use synthetic messages and transports, with no account or live sends.
