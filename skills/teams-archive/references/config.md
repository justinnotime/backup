# Caller configuration

Copy [config.example.yaml](config.example.yaml) into a private location. The
program reads the `teams` mapping; other mappings are ignored so a caller may
keep multiple platform settings in one private file.

| Setting | Meaning |
|---|---|
| `base_dir` | Base for relative paths, relative to the configuration directory; defaults to that directory. |
| `output_dir` | Archive root; required. Each chat owns a directory containing UTC `YYYY-MM.md` files. |
| `state_file` | Required JSON synchronization state. The caller may stage it for later publication. |
| `registry_file` | Optional JSON chat directory, compatible with caller-owned lookup tools. |
| `backend` | `graph` (default) or `gsk`. |
| `graph.client_id` | Explicit public application client ID for delegated device login; required for Graph. |
| `graph.tenant` | Login tenant, default `organizations`. |
| `graph.token_cache` | Private MSAL cache path, required for Graph. Login requests `Chat.Read`. |
| `gsk_command` | Executable path/name for the optional connector, default `gsk`. |
| `command_environment` | Optional string mapping added to external commands' environment, including the publisher and attachment connector. Values are literal, without shell expansion. |
| `mode` | `whitelist` (default) or `blacklist`. Empty whitelist selects nothing. |
| `chats` | List of match strings or mappings with `match`, optional `alias`, `include_groups`, and `bootstrap_days`. |
| `bootstrap_days` | Initial lookback, default 14. Existing disk content limits recovery if stored progress is ahead of it. |
| `max_pages_per_chat` | Page safety limit; Graph sync uses a floor of 200 to drain newest-first history. Peek uses its requested page budget. |
| `attachments` | Download inline images and file attachments via the configured connector, default false. |
| `attachment_relay_dir` | Temporary AI Drive directory, default `/teams-archive`. |
| `enabled` | Set false to disable sync. |

Matches are case-insensitive substrings. In whitelist mode, person matching
applies to one-to-one chats; group chats match their topic unless the entry
sets `include_groups: true`. Exact chat IDs are also accepted. Blacklist mode
checks both topic and members. Invalid match entries fail before fetching.
Aliases must be single directory names. Existing state preserves each chat's
directory even when its topic changes.

Relative paths resolve from `base_dir`, or the config file's directory when
omitted. `--base-dir` overrides it and resolves relative to the invoking
directory. `--output-dir`, `--state-file`, `--registry-file`, `--token-cache`,
`--client-id`, and `--backend` override their corresponding settings. A private
adapter can supply machine paths without embedding them in the public package.

## External publication

`--publish` invokes the optional `teams.publish.command` argument array,
appending this package's reader command. The publisher must supply absolute,
separate output and staged-state directories in the environment variables
named by `publish.base_env` and `publish.state_env`. Only archive and progress
paths move into those directories; credentials and the optional chat registry
keep their original locations. The output must be a subdirectory of `base_dir`.

The publisher owns locks, worktree creation, commit/push, attachment delivery,
and state promotion. A failed reader must abort publication; a failed publisher
returns nonzero through this entry. Include `--` in the command array when the
external publisher uses it to delimit its reader command. This package does
not discover or import any other package's implementation.

Arguments support `{base_dir}`, `{output_dir}` (relative archive path),
`{state_dir}` (original progress directory), and `{utc}`. They are passed
directly without shell evaluation. For example, inside the `teams` mapping:

```yaml
publish:
  command: [/path/to/publisher, --root, '{base_dir}', --paths, '{output_dir}', --]
  base_env: ARCHIVE_WORKTREE
  state_env: ARCHIVE_STAGED_STATE
```

Use `command_environment.PATH` when an attachment connector needs a runtime
that the scheduler does not provide. Use explicit paths; strings such as
`$PATH` and `~` are not expanded in environment values. Do not store credentials
in a public configuration example.

Publication is a sync mode, separate from login, inspection and attachment
backfill. `--dry-run` and `--dump-raw` cannot be combined with publication.

`--backfill-attachments DAYS` downloads attachments into existing archive
directories and adds missing attachment links to captured messages; it does
not advance synchronization state. `--dump-raw` writes private diagnostic API
responses under the archive's `.debug/`; do not publish that directory.

Restore the previous package revision to roll back program behavior. Retain
the caller's archive and state; the migration does not rename historical data.
