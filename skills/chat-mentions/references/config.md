# Private configuration

Python 3.10 or newer is required. Install the package's own MSAL and requests
dependencies for collection. Local draft operations make no network requests.
The CLI targets POSIX systems; the collector uses the configured file lock.

A local-only draft box needs no account settings:

```json
{
  "schema": "chat-mentions/v1",
  "state_directory": "state",
  "collection_enabled": false
}
```

Paths expand `~` and environment variables. Relative paths are resolved beside
the configuration file. Keep configuration, state, and credentials private.

For authorized collection, configure `collection_enabled: true`, `client_id`,
and `read_token_file` for an existing MSAL cache with delegated `Chat.Read`.
The package uses Microsoft Graph's global service. It never initiates device
login or grants account permissions. `login_hint` selects an account when needed;
`authority` can choose the account's Microsoft authority. Otherwise the
organizations authority is used. `own_user_id` may identify the selected account
explicitly; otherwise the token's account identifier or `/me` is used.

Optional settings:

| Field | Default | Purpose |
|---|---|---|
| `lock_file` | `collector.lock` under state | Exclude simultaneous collectors |
| `sender_hourly_limit` | 4 | Emitted events per sender in the preceding hour |
| `list_page_limit` | 10 | Maximum pages for the active-chat listing |
| `message_page_limit` | 10 | Maximum pages per chat |
| `first_run_lookback_minutes` | 30 | Initial lookback when no progress exists |
| `overlap_minutes` | 10 | Revisit recent history behind stored progress |
| `draft_expiry_hours` | 48 | Age after which pending drafts display as expired |

State uses `state.json`, `queue.jsonl`, and dated Markdown files under `drafts/`.
Existing queue/progress files and draft metadata are read without requiring a
bulk migration. A draft's identity is its chat/message pair; new filenames use
a digest of that pair instead of embedding untrusted message IDs into paths.
The queue is written before progress; a retry deduplicates already stored events.
Malformed stored data is an error rather than a reason to reset progress.

The collection window follows creation timestamps and has configured caps; it
is not a complete archive of edits, old mentions, or messages suppressed by the
sender limit. Increasing a page limit addresses an incomplete walk; successful
completion must precede a progress update. The upstream APIs are
[chat listing](https://learn.microsoft.com/en-us/graph/api/chat-list?view=graph-rest-1.0)
and [message listing](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0).
