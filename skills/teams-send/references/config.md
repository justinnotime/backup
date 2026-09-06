# Private Teams sender configuration

The JSON object uses `schema: teams-send/v1`. Paths expand `~` and environment
variables; relative paths are resolved beside the configuration file. Keep this
file and credential caches outside the public package.

```json
{
  "schema": "teams-send/v1",
  "state_directory": "state",
  "read_token_file": "credentials/read.json",
  "send_token_file": "credentials/send.json",
  "client_id": "<your-configured-client-id>",
  "marker": "[assistant]"
}
```

Required fields are the schema, state directory, both token-cache paths, and
client ID. They do not create an account or grant consent. The read cache is
used for chat and member lookup; the separate send cache is used for delegated
message creation. See the [Microsoft send-message API](https://learn.microsoft.com/en-us/graph/api/chat-post-messages?view=graph-rest-1.0)
and [MSAL token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens).
Direct Graph operations target the Microsoft global cloud service.

Optional fields:

| Field | Meaning |
|---|---|
| `registry_file` | Existing or generated chat registry; defaults to `teams-chats.json` in the state directory |
| `authority` | MSAL authority; defaults to the Microsoft organizations authority |
| `login_hint` | Select one account when the cache contains several |
| `marker` | Prefix applied once to outgoing text; empty by default |
| `proposal_ttl_seconds` | Pending proposal lifetime, default 3600 |
| `mirrored_chat_patterns` | Substrings used only for the registry's display marker |
| `audit_preview_chars` | Optional message excerpt length in the private audit; default 0 |
| `gsk_command` | External send-command argv prefix, default `["gsk", "microsoft_teams", "send"]` |

The connector prefix is executed without a shell. The package appends
`--chat_id`, `--content`, and optional `--reply_to_message_id`,
`--mention_user_ids`, and `--mention_user_names` arguments. A successful connector
returns JSON with `status: ok` and `data.message_id`, and exits zero. Its account
selection and permissions belong to that connector's configuration.

Local records preserve a JSON chat registry, proposal JSON files under
`teams-send-queue/`, and an append-only `teams-send.log` containing timestamps,
target identifiers, message IDs, and a content digest. Message text excerpts
are stored only when configured. These are private operational records.

The CLI is intended for a POSIX shell. Interactive proposal approval additionally
requires `/dev/tty`. No operating-system service, schedule, or credential is
installed by the package.
