# Manifest v1

The schema identifier is `agent-session-extraction-manifest/v1`. The file is
strict JSON: missing and unknown fields are errors. Every source entry must
state `enabled`; no source is selected because a Backup profile happens to
exist.

## Source authority

Each `sources[]` entry names an opaque ID, harness, explicit or explicitly
selected native-default path, `owner` or `mirror` authority, required status,
output node, stable-byte or read-only-SQLite snapshot mode, explicit file/glob
discovery, decoder options, and whether an empty source is authoritative.

`root_policy` checks four values separately: configured lexical path,
configured resolved path, every candidate lexical path, and every candidate
resolved path. `symlinks=confined` permits only targets that remain under both
the configured source and allowed resolved roots. `symlinks=reject` refuses
any traversal. Use `forbidden_components` and `required_suffixes` as additional
consumer defenses; they never replace root containment.

`native-default` uses only the explicitly supplied `HOME` value. It is not a
claim that the source belongs to a particular consumer.

DeepSeek Harness sources that may contain the actively appended final frame
must explicitly set `decoder.allow_torn_current_frame=true`. The default is
false, so a truncated completed snapshot fails instead of being treated as an
in-progress file.

Decoder options are strict and harness-specific; unknown names and wrong types
invalidate the manifest. Shared synthetic-prefix and conversational-subagent
retention rules live only in `event_policy` and are injected into decoders by
the runtime.

For OpenCode, `sqlite-readonly` validates and reads the database and any WAL
sidecar without opening the source through SQLite. It rechecks both byte
streams, recovers committed WAL content through a private mode-0600 temporary
copy, and removes that copy before returning. A moving database, escaped WAL
sidecar, or rollback journal is an unreadable source; existing output is then
preserved and cleanup is blocked.

| Harness | Optional `decoder` fields |
|---|---|
| Claude Code | `session_id`, `project_hint`, `conversation_kind`, `conversational_subagent_min_user_events` |
| Codex | `session_id`, `project_hint` |
| OpenCode | `minimum_user_events`, `excluded_cwd_prefixes` |
| DeepSeek Harness | `compression`, `allow_torn_current_frame` |
| Cursor | `session_id`, `project_hint`, `minimum_user_events` |
| OpenClaw | session/project hints, minimum events, cron/notification/channel filters, and optional channel/session metadata fields |

## Policies and output

- `ownership.mode`: `owner` or `aggregator`.
- `event_policy`: synthetic prefixes, peer-agent rules, retention thresholds,
  and conversational-subagent retention.
- `project_policy`: all/allowlist/denylist, aliases, and unknown handling.
- `output.layout`: `flat` or `monthly`; moving flat managed files requires the
  explicit `flat-to-monthly` migration.
- `output.compatibility`: exact SHA-256 values under `legacy-output/v1` may
  grandfather unchanged historical files. New or changed files always use v1.
- `cleanup.scope`: `none`, `owner`, or `aggregator`. A node is eligible only
  when every enabled source assigned to that output node succeeded. A source's
  `owner`/`mirror` label describes where bytes came from; it never grants
  cleanup authority by itself.
- `indexes.mode`: `none`, `owner`, `every-node`, or `aggregator-only`. Indexes
  are built from preserved disk inventory plus planned changes.
- `publisher.strategy`: `none`, `filesystem-atomic`, or `git-worktree`.
  Publication and Git staging are limited to `owned_subtrees`.
- `gates`: required source behavior plus mandatory redaction, output audit,
  reconciliation, and pre-publication scan controls.

Every custom redaction regex has a public synthetic canary. The regex and
canary may live in a private consumer manifest; do not encode a real
credential as either one. A required redactor with no working pattern fails
before sources are opened.

See `manifest.example.json` for placeholder structure. It is intentionally not
runnable until every `/absolute/...` path is replaced by a consumer-owned
location.
