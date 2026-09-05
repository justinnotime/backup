# Normalized session contract

The shared API version is `agent-session/v1`. Its stable identity is the
three-part tuple `(harness, node_label, complete session_id)`.

## Session

| Field | Contract |
|---|---|
| `schema_version` | Exactly `agent-session/v1` |
| `harness` | `claude-code`, `codex`, `opencode`, `dsh`, `cursor`, or `openclaw` |
| `session_id` | Complete harness identifier, never a shortened filename suffix |
| `source_ref` | Manifest source ID plus candidate-relative reference; never absolute |
| `node_label` | Supplied by `sources[].output_node`, never inferred |
| `cwd` | Optional source metadata; it does not grant access or identify a node |
| `project` | Policy-normalized project name |
| `started_at`, `ended_at` | UTC-aware timestamps or null |
| `events` | Ordered normalized events |
| `metadata` | Decoder observations that contain no credential or transcript copy |

## Event

An event has a stable sequence number, UTC-aware or absent timestamp,
`exact`/`approximate`/`unknown` timestamp quality, `user`/`assistant`/
`peer-agent` role, text, raw record kind, and non-sensitive metadata.

Decoders emit `user-like` before policy. The engine makes the retention
decision first and only then relabels configured peer-agent messages. Changing
the label policy therefore cannot change whether a session is retained.

History and prompt views are rendered from this same value. Prompt extraction
must never reparse rendered Markdown.

When several declared sources contain the same normalized identity, an exact
role/text prefix is treated as an older append-only generation and the longest
generation wins. Event timestamps do not distinguish generations because a
harness may expose approximate assistant times. Any non-prefix role/text
difference is reported as `DUPLICATE_SESSION_DIVERGENCE` and blocks
publication through reconciliation.

## Decoder boundary

A decoder receives a stable `SourceSnapshot` and returns `DecodeBatch`.
It does not discover undeclared sources, decide cleanup, infer a consumer,
write files, or print transcript text. An unrecognized record whose shape or
name says it may carry conversation text (for example a Claude Code record with
a `message` envelope, or a Codex payload with a `message`, `item`, or `content`
key) appears in `FormatObservations.unknown_record_counts` and makes the batch
incomplete. Any other unrecognized record is session bookkeeping and is counted
under `recognized_record_counts` as `ignored.<type>`, so a harness adding a new
bookkeeping record does not stop extraction. Required malformed input makes the
batch incomplete or invalid and blocks publication.

`agent_skills.sessions.api.decode_source_snapshots` is the public parity
boundary for a caller that has already frozen byte-backed `SourceSnapshot`
values. It verifies source identity, decodes, applies manifest policy,
normalizes, deduplicates, and returns an `ExtractionSnapshot` with format
observations. It deliberately performs no discovery, rendering, cleanup, or
publication and rejects snapshots that require direct-path revalidation.
