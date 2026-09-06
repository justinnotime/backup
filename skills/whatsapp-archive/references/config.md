# Configuration and operation

Python 3.10+ and PyYAML run the archive reader. The bridge also needs Node 20+
and the exact dependencies in `bridge/package-lock.json`. Run installation and
tests in a worktree or deployment directory, not in a shared primary checkout.
The Skill is independent of its siblings. It makes no LLM calls.

## Private configuration

```yaml
whatsapp:
  enabled: true
  base_dir: /path/to/archive-repository
  output_dir: Raw/messages
  state_file: /path/to/private-state/whatsapp.state.json
  spool_dir: /path/to/device-store
  mode: whitelist
  chats:
    - match: Example group
      alias: example-group
  refresh_before_sync: true
  bridge:
    node: /path/to/node
    dependencies_dir: /path/to/bridge-dependencies
  command_environment:
    PATH: /usr/local/bin:/usr/bin:/bin
  publish:
    command: [/path/to/publisher, --repository, '{base_dir}', --paths, '{output_dir}', --state, '{state_dir}', --]
    base_env: ARCHIVE_WORKTREE
    state_env: ARCHIVE_STAGED_STATE
```

`base_dir` is relative to the configuration directory. Output and state resolve
against it; `--base-dir` overrides their base without relocating the input
spool. The spool resolves against the configured base; `bridge.dependencies_dir`
resolves against the configuration directory. Absolute paths are recommended
for device storage and durable state. CLI overrides are `--output-dir`,
`--state-file`, and `--spool-dir`.

Selection is case-insensitive substring matching against chat name and identifier.
`whitelist` keeps matches; `blacklist` excludes them. A chat entry is a string or
an object with `match` and an optional directory `alias`. Invalid selections
fail before output is written. Slugs remain pinned in state across name changes.
Selection or indexed-name changes rebuild selected chats from the full spool.
Archive removal is always a separate operator action.

## Dependency deployment and pairing

For local development run `npm ci --prefix bridge`. For a read-only source
checkout, install dependencies separately and configure `dependencies_dir`:

```bash
mkdir -p /path/to/bridge-dependencies
cp bridge/package.json bridge/package-lock.json bridge/apply-baileys-patch.mjs /path/to/bridge-dependencies/
npm ci --prefix /path/to/bridge-dependencies --no-audit --no-fund
```

The postinstall patch preserves the Windows desktop platform mapping for the
pinned Baileys version. It fails if the expected mapping is absent; review a
version update before using it with a paired device.

```bash
scripts/sync --config /path/to/private.yaml --bridge login
scripts/sync --config /path/to/private.yaml --bridge daemon
scripts/sync --config /path/to/private.yaml --bridge status
scripts/sync --config /path/to/private.yaml --bridge drain --seconds 45
```

`login` displays a QR and keeps receiving the initial history after pairing.
Reuse the same store on upgrades. The caller owns the service definition and
restart policy: an unpaired or logged-out bridge exits 78. SIGTERM flushes
pending metadata and removes its PID. The spool retains both incoming and
outgoing messages received for the linked account. No sending or read receipts
are implemented. This is an unofficial linked-device client; service behavior
and available history depend on the upstream service and paired device.

`refresh_before_sync: true` calls `drain` before writing an archive. A live
bridge PID skips the extra connection. Otherwise drain receives the offline
queue and waits for quiescence, with a 480-second hard limit. A failed refresh
fails the archive run. The caller allows 540 seconds, then SIGTERM and a
30-second flush period before forced termination. Neither `--dry-run` nor the
read-only inspection modes refresh the bridge. `--doctor` validates configuration
only; it is not a connectivity or pairing check.

The Node entry can also run directly with explicit `WA_BRIDGE_DIR` and optional
`WHATSAPP_BRIDGE_DEPENDENCIES` (directory containing `node_modules`). The Python
entry supplies these from configuration and replaces itself for daemon mode,
so service signals reach the actual bridge process.

## Archive and state contract

`spool_dir/spool/*.ndjson` holds append-only UTF-8 JSON records terminated by
newlines. Version 1 records require `v: 1`, numeric `ts` in Unix seconds, nonempty
`chat_jid` and `msg_id`. Optional fields include chat/sender names and identifiers,
`from_me`, `text`, `type`, `media`, and `source` (`live` or `history`).
`chats.json` maps chat identifiers to name, type, and last-message timestamp.
The bridge keeps private `auth/`, `contacts.json`, `meta.json`, and `daemon.pid`
in the same store. Do not publish the device store.

The reader captures each spool file once per run and hashes those bytes. It
regenerates affected chat-months from the complete captured spool, deduplicates
by message ID (first occurrence wins), and sorts by timestamp then ID. Timestamps
are UTC. Corrupt records, unfinished appended records, corrupt state/index, or
missing previously archived spool days fail without advancing state. Retry
unfinished records after the writer finishes. Media placeholders preserve
captions and filenames; media bytes are not fetched.

State version 1 retains `files`, `chats` (pinned slugs), and `config_hash`.
Historical size-based file markers are accepted and replaced by content hashes
on the next successful run. Do not prune source spool files behind this state.
`--full` rebuilds selected months; `--dry-run` reads and plans without writing
archive/state or touching the bridge. `--allow-missing-spool` explicitly allows
an unscheduled non-owner to skip an absent spool; publication cannot use it.

## External publication

`--publish` invokes `publish.command` as an argument array without a shell,
then appends the archive writer command. Literal replacements are `{base_dir}`,
`{output_dir}` (relative subtree), `{state_dir}`, and `{utc}`. The publisher must
provide absolute worktree and staged-state directories in `base_env` and
`state_env`. The writer rejects the original output base or durable-state
directory as staging destinations. Configuration and spool paths remain fixed.

The external publisher owns locks, an isolated checkout, staging existing state,
commit validation, pushing, and copying staged state back only after successful
publication. It must pass through the reader's failure status. This package
contains no repository policy, scheduler, Git publisher, or machine service.
The command exits nonzero on reader/refresh failures and preserves the external
publisher's exit code. It refuses to combine publication with dry-run or other
operation modes. A successful direct reader run alone does not establish that
anything was published.

## Portable home paths

Use `~` for path settings and `{home}` inside `publish.command` arguments.
The home comes from the running user's environment, without a shell. Keep
repository paths relative to `base_dir` where possible. After moving a checkout,
regenerate external scheduler/service definitions that captured absolute paths.

`command_environment` values expand environment variables such as `$HOME` and
`${PATH}` from the caller's environment. This does not execute shell syntax.
`bridge.node` also expands `~` and environment variables. Use `node` when it
is on the configured PATH; an explicit interpreter path may stay in private
configuration when the deployment requires a particular installed version.
