# Configuration and archive contract

Start from [config.example.yaml](config.example.yaml). The root `slack` mapping
contains the following fields:

| Field | Meaning |
| --- | --- |
| `output_dir`, `state_file` | Required archive directory and state file; overridden by `--output-dir` and `--state-file`. Relative paths use `--base-dir`, or the config file's directory when omitted. |
| `enabled` | Explicit `false` disables sync. Defaults to enabled. |
| `workspaces` | List of mappings with distinct caller-chosen `name` and explicit `token_file`. |
| `mode`, `chats` | Inherited selection defaults: `whitelist` with an empty list selects nothing; `blacklist` with an empty list selects every visible active conversation. Entries are substrings or `{match, alias}` mappings. |
| `bootstrap_days` | Initial message range, default 14 days. Each workspace can override it. The range is fixed on the first successful run; it does not slide forward. |
| `max_pages` | Optional per-history/thread request limit, default 0 (exhaust pagination). Reaching a positive limit while data remains fails the run. Each workspace can override it. |
| `request_interval` | Minimum seconds between requests, default 1.3. |
| `page_size` | Requested results per page, default 200; range 1–999. The API may return fewer. |

Each workspace can override selection, `bootstrap_days`, and `max_pages`.
Workspace names and aliases must be single path components. Explicit token-file
paths use the same base as archive paths. Optionally pass `--token-dir DIR` to
supply `DIR/slack-token-<workspace>` for workspaces without `token_file`.
Token values never belong in command arguments or public configuration.

Configure a Slack app and obtain a user token separately. The reader uses
`channels:history`, `channels:read`, `groups:history`, `groups:read`, `im:history`,
`im:read`, `mpim:history`, `mpim:read`, and `users:read` as applicable to selected
conversation types. See Slack's [history](https://docs.slack.dev/reference/methods/conversations.history/)
and [thread reply](https://docs.slack.dev/reference/methods/conversations.replies/)
documentation for token access and rate limits. Commercially distributed apps
can have substantially lower history limits than internal apps; choose request
settings for the actual installation.

## Progress and compatibility

Monthly files live at `<output>/<workspace>/<conversation>/<YYYY-MM>.md` and
deduplicate `<!-- id: timestamp -->` records. Version-1 state remains readable;
existing slugs and monthly text remain stable. On upgrade, the first scan uses
the earlier of the configured bootstrap start, existing archived message IDs,
and the legacy watermark. It recovers late replies missed by a legacy reader
within that range. Upstream retention and token access still bound availability.

`archive_from` persists that initial range. `scanned_before` records the start
of the last fully successful scan. Parent discovery has no lower time bound;
reply reads use the archived range on the first run and overlap the previous
scan's start thereafter. The ordinary `watermark` remains for older consumers.
No per-thread registry, event subscription, extra token scope, or second
scheduled job is needed. Restoring an older consumer retains filenames/state,
but restores its older thread-discovery behavior as well.

Each run is read-only against Slack. List/peek and dry runs never save local
progress. Sync saves state atomically only after every selected workspace and
conversation succeeds. A transactional publisher must separately stage that
state and promote it only after its archive publication succeeds.
