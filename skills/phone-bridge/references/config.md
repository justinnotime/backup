# Private configuration

The runtime needs Python 3.10 or newer on a Unix-like host; it uses only the
standard library. Tests and lint dependencies belong to this package:

```bash
uv sync --project /path/to/phone-bridge --locked
(cd /path/to/phone-bridge && uv run --no-sync pytest tests)
```

Commands use `--config PATH`, then `PHONE_BRIDGE_CONFIG`, then
`$XDG_CONFIG_HOME/phone-bridge/config.json` (defaulting to
`$HOME/.config/phone-bridge/config.json`). Keep real configuration and credentials
outside this public package.

```json
{
  "schema": "phone-bridge/v1",
  "homeserver": "https://matrix.example.invalid",
  "room_id": "!transfer:example.invalid",
  "user_id": "@sender:example.invalid",
  "auth_file": "~/.config/example-matrix/auth.hdr",
  "state_file": "~/.local/state/phone-bridge/since",
  "inbox_dir": "~/.cache/phone-bridge/inbox",
  "max_file_bytes": 52428800,
  "timeline_limit": 100
}
```

The account must already be joined to the room. `auth_file` contains a single
`Authorization: Bearer ...` header, supplied privately by the account owner.
The runtime verifies that the token belongs to `user_id`. It does not join,
invite, log in, or obtain tokens. Use a dedicated account/token for this bridge;
do not share its `/sync` stream with an encryption-capable client.

Paths expand `~` and caller environment variables. Relative paths are relative
to the configuration file's directory. The auth and cursor paths must differ.
The configured homeserver uses HTTPS; HTTP is accepted only on loopback for
local tests. Authenticated requests do not follow redirects.

`max_file_bytes` defaults to 50 MiB for uploads and downloads; the actual server
may impose a smaller limit. Files remain in `inbox_dir` until the user removes
them. Downloads use an opaque media prefix plus a sanitized basename, and
receive state is written only after all attachments in the batch are available.

`timeline_limit` defaults to 100 and accepts 1..1000. The server may return fewer
events. If it reports a truncated timeline, the command stops without advancing
the cursor. Increase the configured limit if appropriate, or recover the gap
with a full Matrix client. This small transfer tool does not backfill history.

## Existing command compatibility

Both executable names remain `mx-send` and `mx-recv`. Without `--text` or `--file`,
`mx-send` uploads when every argument names an existing local path; otherwise it
joins the arguments as text. Prefer explicit flags to remove that ambiguity.
`--file` validates all named inputs before the first upload. A multi-file send
can still be partially delivered if a later network request fails; successful
sends print their confirmed event IDs before the error.

The cursor is a plain Matrix `next_batch` token. To adopt an existing bridge,
configure its exact cursor path instead of creating an empty file. Empty or
unreadable cursor files are errors. First use with no cursor intentionally
starts from now. Encrypted events/attachments and failed downloads also stop
without moving the cursor. Only one receiver may hold the cursor lock.

After moving accounts or homes, preserve the credential and cursor files,
update private paths as needed, reinstall the Skill link, then run `mx-recv
--doctor`. Doctor does not create state/download directories or consume messages.

## Protocol references

Incremental reads use the [`/sync` next-batch token](https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3sync).
Sending uses explicit Matrix room IDs and per-request transaction IDs. Downloads
try the [authenticated media endpoint](https://matrix.org/docs/spec-guides/authed-media-servers/),
with the legacy endpoint only after a 404 or 405. Credentials are sent to the
configured homeserver only; media URI hosts are path components, not new HTTP
destinations.
