# Configuration and deployment

Install with Python 3.10 or newer and the package's locked dependencies. Keep
environments and credentials outside a shared primary checkout.

```bash
uv sync --project /path/to/google-chat-archive --locked
(cd /path/to/google-chat-archive && uv run --no-sync pytest tests -q)
```

`scripts/sync` uses `GOOGLE_CHAT_ARCHIVE_PYTHON`, defaulting to `python3`. The
console entry `google-chat-archive` is also installed by the Python package.
Use `--config` or `GOOGLE_CHAT_ARCHIVE_CONFIG` to select a YAML or JSON file:

```yaml
googlechat:
  enabled: true
  base_dir: ~/archives
  output_dir: google-chat
  state_file: ~/.local/state/google-chat-archive/state.json
  token_file: ~/.config/example-google/authorized-user.json
  mode: whitelist
  chats:
    - match: spaces/EXAMPLE
      alias: example-space
  bootstrap_days: 90
  max_pages: 20
  self_email: reader@example.invalid
```

Paths expand `~` and environment variables. `base_dir` is relative to the config
file; output, state, and credentials resolve against that base. `--base-dir`
relocates output and relative state paths while credentials remain relative to
the originally configured base. `--output-dir`, `--state-file`, and `--token-file`
override individual paths. State must not alias credentials or the config file.

Selection matches a case-insensitive substring of each space's name or resource
ID. String entries are also accepted. `whitelist` selects matching spaces;
`blacklist` excludes matching spaces. Empty whitelist selects nothing. Empty
blacklist selects every visible space. Aliases must be single directory names.
`bootstrap_days` applies when no trustworthy archived progress exists.
`max_pages` bounds each listing; message pages request 100 entries in ascending
creation order and overlap the saved watermark by 15 minutes.

Use a privately supplied authorized-user JSON file with `client_id`,
`client_secret`, and `refresh_token`. This package does not run an OAuth consent
flow or persist refreshed tokens. Enable the Google Chat API and authorize
`chat.spaces.readonly`, `chat.messages.readonly`, and `chat.memberships.readonly`.
Optional People API access can improve sender names; without it, stable user
IDs are retained. `self_email` helps exclude the account itself when naming an
unnamed direct message or group through the membership email alias.
If member names cannot be resolved, an unnamed conversation uses its stable
space ID. Missing display names do not exclude an otherwise selected space;
existing archive directory names remain fixed.

An absent or invalid credential is an error by default. `--skip-unconfigured`
allows a non-owner installation to skip only an absent credential file; an
existing but invalid credential still fails. Authenticated redirects are refused
and error diagnostics omit response bodies and credential values.

## Existing archives and publication

Preserve the existing monthly files, directory names, and version-1 state file.
The state records per-space directory names, watermarks, and a sender-name cache.
Directory names can be recovered from monthly frontmatter if state is absent.
Malformed existing state is an error; keep it for recovery rather than erasing
it. Sync does not remove existing files.

An external transaction can invoke:

```bash
/path/to/google-chat-archive/scripts/sync --config /private/chat.yaml \
  --base-dir /private/transaction --output-dir archive/google-chat \
  --state-file /private/staged-state/google-chat.json
```

The external caller supplies its own lock, commit validation, and publication
rules. It must discard staged state after a failed sync or publish. Ordinary
standalone sync writes directly to its configured local archive.

API references: [message listing and timestamp filters](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages/list),
[membership lookup and email aliases](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.members/get).
