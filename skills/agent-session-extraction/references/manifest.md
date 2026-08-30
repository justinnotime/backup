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

A Claude source may list historical malformed JSONL records in
`decoder.grandfathered_malformed_line_sha256`. Each entry is the complete
lowercase SHA-256 of the raw record bytes, excluding its line terminator. Only
JSON parse failures with an exact match are ignored. Valid non-object JSON,
unlisted malformed records, invalid hashes, and duplicate hashes still fail
closed. Reports expose only ignored or malformed counts; they never print the
configured hashes, source paths, or record bytes. Keep non-empty lists in the
owning consumer's private manifest, not in the shared repository.

Each source may include an `event_policy` object that overrides any of
`min_direct_user_events`, `min_user_chars`,
`retain_conversational_subagents`, and `retention_mode` for that source only.
Missing values inherit the global policy. With `count-or-long` (the global
default), a session is retained when it meets the direct-user event count or
has a direct-user event at least `min_user_chars` long. `count-only` requires
the event count regardless of message length. This retention decision uses
synthetic-filtered user-like events before peer-agent relabeling.

OpenCode has two explicit read-only modes:

- `sqlite-readonly` validates and reads the database and any WAL sidecar
  without opening the source through SQLite. It rechecks both byte streams,
  recovers committed WAL content through a private mode-0600 temporary copy,
  and removes that copy before returning.
- `sqlite-immutable` is only for a checkpointed snapshot whose producer has
  made it immutable, such as a Backup-produced database. It rejects a
  non-empty WAL or rollback journal, opens the validated database with SQLite
  `mode=ro&immutable=1`, and revalidates its inode, size, modification time,
  and sidecar state after decoding.

A moving database, escaped sidecar, or mode precondition failure makes the
source unreadable; existing output is preserved and cleanup is blocked.

| Harness | Optional `decoder` fields |
|---|---|
| Claude Code | `session_id`, `project_hint`, `conversation_kind`, `conversational_subagent_min_user_events`, `grandfathered_malformed_line_sha256` |
| Codex | `session_id`, `project_hint` |
| OpenCode | `minimum_user_events`, `excluded_cwd_prefixes` |
| DeepSeek Harness | `compression`, `allow_torn_current_frame` |
| Cursor | `session_id`, `project_hint`, `minimum_user_events` |
| OpenClaw | session/project hints, `minimum_user_events`, `minimum_total_events`, cron/notification/channel filters, and optional channel/session metadata fields |

OpenClaw applies `minimum_user_events` and `minimum_total_events` after its
configured synthetic, operational-notification, and channel-forward filters;
the latter counts the retained user and assistant events together.

## Policies and output

- `ownership.mode`: `owner` or `aggregator`.
- `event_policy`: synthetic prefixes, peer-agent rules, retention thresholds,
  and conversational-subagent retention.
- `project_policy`: all/allowlist/denylist, aliases, and unknown handling.
  Optional `resolvers` run in declaration order for their listed source IDs.
  Each selects `cwd`, `source_ref`, or `project_hint` with a Python regular
  expression that must contain a named `project` group. The first non-empty
  match wins before the ordinary project hint/cwd fallback and alias mapping.
  Resolver patterns that encode consumer conventions belong only in the
  consumer manifest.
- `project_policy.prompt_by_harness`: optional per-harness
  all/allowlist/denylist policy for prompt rendering. Its `unknown` behavior
  and lists affect the prompt view only; an excluded prompt still retains its
  history view.
- `output.history_directory_by_harness`: optional strict map from a supported
  harness name to its history directory. Unlisted harnesses use
  `history_directory`; history and prompt directories must remain distinct,
  and every selected directory must be covered by `publisher.owned_subtrees`.
- `output.layout`: `flat` or `monthly`; moving flat managed files requires the
  explicit `flat-to-monthly` migration.
- `output.filename_strategy`: default deterministic basename strategy.
  `filename_strategy_by_harness` overrides it for named harnesses. Supported
  values are `project-session-suffix`, `session-prefix-8`,
  `session-last-component-prefix-8`, `session-suffix-8`, and
  `node-session-sha256-12`; collision suffixes are still assigned from the
  complete normalized identity set.
- `output.compatibility.rule_version`: `legacy-output/v1` grandfathers only
  exact SHA-256 values listed in `unchanged_sha256`.
  `legacy-agent-markdown/v1` instead recognizes the prior agent history,
  prompt, and README shapes, derives their ownership and semantic identity,
  and adopts unchanged content in place without a bulk rewrite. New or
  semantically changed output is rendered under the current contract.
- `cleanup.scope`: `none`, `owner`, or `aggregator`. A node is eligible only
  when every enabled source assigned to that output node succeeded. A source's
  `owner`/`mirror` label describes where bytes came from; it never grants
  cleanup authority by itself.
- `indexes.mode`: `none`, `owner`, `every-node`, or `aggregator-only`. Indexes
  are built from preserved disk inventory plus planned changes.
- `publisher.strategy`: `none`, `filesystem-atomic`, or `git-worktree`.
  Publication and Git staging are limited to `owned_subtrees`. Before a
  `git-worktree` run reads inventory, every owned subtree must match `HEAD`,
  and the check repeats immediately after that read. Tracked changes,
  untracked files, ignored files, `skip-worktree`, and `assume-unchanged` index
  state are all rejected. The same check runs again immediately before
  preparing the worktree. Dirt outside the owned subtrees does not block
  extraction.
- `publisher.encryption=git-crypt`: requires `strategy=git-worktree` and links,
  rather than copies, the configured key into the throwaway worktree. Every
  `key_link.target` is relative to that worktree's private Git directory and
  must be `git-crypt/keys/default` or `git-crypt/keys/<safe-name>`. The default
  target requires the `git-crypt` filter; a safe name begins with an ASCII
  letter or digit and then contains only letters, digits, hyphens, or
  underscores. A named target requires exactly `git-crypt-<safe-name>`. The
  publisher creates the worktree without checkout, loads `HEAD` into its
  private index without filters, validates cached filter attributes for every
  tracked owned file and planned write, then links the key before checkout.
  Checkout must yield plaintext. Every planned write's staged index blob must
  be git-crypt ciphertext without the planned plaintext.
  Missing attributes, mismatched key names, inactive filters, ciphertext
  checkouts, and plaintext index blobs fail publication and remove the
  throwaway worktree.
- `gates`: required source behavior plus mandatory redaction, output audit,
  reconciliation, and pre-publication scan controls.

Every custom redaction regex has a public synthetic canary. The regex and
canary may live in a private consumer manifest; do not encode a real
credential as either one. A required redactor with no working pattern fails
before sources are opened.

See `manifest.example.json` for placeholder structure. It is intentionally not
runnable until every `/absolute/...` path is replaced by a consumer-owned
location.
