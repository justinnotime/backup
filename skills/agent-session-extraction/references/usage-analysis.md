# Usage and activity analysis

Use this mode to reconcile visible token counters and investigate what preceded
model operations. LLM calls = 0. It does not modify sources, archives, Git,
schedules, or providers. The manifest's enabled sources and path restrictions
remain authoritative; backup labels grant no read access.

```bash
scripts/analyze-usage --manifest /private/manifest.json \
  --start 2026-02-01T00:00:00Z --end 2026-03-01T00:00:00Z \
  --output /private/new-analysis --config /private/analysis.json
```

The interval is half-open and requires timezone-aware timestamps. The output
must not exist. JSON and compressed JSONL are created inside a private directory.
Do not choose an output inside a raw source or an archive publisher's owned
subtree. These artifacts contain identifiers, timestamps, paths, usage and
configured labels: **they remain private even though prompt and tool text are
not emitted**. Never publish an actual configuration or generated result as a
package example. CLI error output suppresses arbitrary exception text.

`usage.jsonl.gz` contains `agent-usage/v1` observations. `activity.jsonl.gz`
contains `agent-activity/v1` inputs. `sources.jsonl.gz` records the read scope and
snapshot hashes. `summary.json` records totals, diagnostics, tariff coverage,
dimensions and cache-rule examples/counterexamples. A byte hash is unavailable
for direct immutable SQLite snapshots; use a byte snapshot for reproducible
content hashes. No output claims complete provider billing coverage.

## Decode and accounting contract

`decode_telemetry(snapshot, config=...)` in `agent_skills.sessions.api` consumes
an explicitly supplied snapshot. Prefer manifest-driven CLI source validation.
A direct immutable SQLite caller must use the source reader's validation and
revalidation contract. This API is a separate consumer of each harness decoder's
raw read step, before prompt cleanup, minimum-user thresholds, project retention,
or subagent exclusion. It does not run the transcript renderer or its schema
acceptance rules. Ordinary archive extraction is unchanged.

| Harness | Visible usage semantics |
| --- | --- |
| Claude Code | Input, read and write counts are disjoint. Keep the maximum output snapshot per message/request/iteration. A fallback's iteration list replaces its top-level usage. Preserve five-minute, one-hour and unspecified writes separately. |
| Codex | Input includes cached reads/writes, which are subtracted to obtain fresh input. Output already includes reasoning. Repeated cumulative snapshots are ignored; last usage is counted once per changed total/turn. Declared forks with older UUIDv7 turn identities, or uncertain ownership, are excluded and counted. |
| DeepSeek Harness | Count terminal assistant-message usage, never streaming chunks. Input/cache categories are disjoint; reasoning is an output detail. Plain JSONL and the existing decoder's compressed format are supported; compressed input requires Python 3.14's `compression.zstd`. |
| OpenCode | Read message rows once, including children and messages without text parts. Cache/input categories are disjoint. Visible output and reasoning are added once. Do not sum session totals or step-part costs again. |
| Cursor / OpenClaw | Usage is unsupported here and coverage is reported as unknown, never inferred as free. |

Missing counters, malformed records, unchanged counters and uncertain fork
ownership have diagnostic counts. A syntactically decoded source is not proof
that all calls, retries, inherited seed histories or harness versions are
accounted for. Validate a new producer/version against native counters before
using its totals for decisions. Streaming message rows are observations, not a
universal provider-request count. Several billed fallback iterations may belong
to one operation. Mirror identity collisions and changed token variants require
review; unchanged sources are deduplicated by bytes.

## Configuration

All fields are optional. No prices or personal vocabulary are bundled. Rates
are per million tokens; the example below is a **synthetic arithmetic scenario**.
Provide all six categories. Missing or ambiguous model/time matches produce
unknown cost, not zero. A caller-supplied tariff label should identify its source
and applicable dates; current reference rates do not establish a historical bill.

```json
{
  "tariff_label": "synthetic scenario, not provider prices",
  "peer_patterns": ["^\\[peer from "],
  "notification_patterns": ["^inbox-ready:"],
  "context_patterns": ["^runtime-context:"],
  "function_rules": {"review": "review|audit", "implementation": "implement"},
  "action_rules": {"coordinate-receive": "fetch_inbox"},
  "project_rules": {"example-project": "/example-project(?:/|$)"},
  "rates": [{
    "model": "example-model",
    "start": "2026-02-01T00:00:00Z",
    "end": "2026-03-01T00:00:00Z",
    "per_million": {
      "fresh": 2, "read": 0.2, "write5": 2.5,
      "write1": 4, "write_unknown": 2.5, "output": 10
    },
    "long_context": {
      "threshold": 200000, "input_multiplier": 2, "output_multiplier": 1.5
    }
  }]
}
```

Optional `harness` on a rate restricts its match. Unspecified cache duration has
its own category: selecting its price is an explicit scenario assumption.
Regional, speed, subscription, discount and service-tier adjustments are not
inferred. Provider-reported cost, when visible, is retained separately.

## Origins, functions and timing

- `task_id` / `task_origin` describe the most recent substantive input; they do
  not prove the original task owner or the final beneficiary of downstream work.
- `wake_id` / `wake_origin` describe the latest input, including notifications
  and injected context. `input_kind` also distinguishes subsequent tool results
  and inbox fetch results. Fetching an inbox does not automatically assign the
  fetched content to a new task; custom inbox payloads need explicit validation.
- Peer evidence overrides a user-shaped transport envelope. A user role alone
  remains `unknown`; child-session input is delegated or unknown. Native author
  fields and configured explicit markers supply stronger evidence.
- `function_candidates` are all matching prompt rules. Actions are visible tool
  candidates, potentially mixed, not validated intent or successful outcomes.
  Shell strings may contain quoted commands that never ran. Each dimension uses
  combinations of labels so mixed actions do not multiply costs.
- `ready_at` and `first_response_at` are observed preparation/input and response
  times. `request_start_at` is null unless actual request-start instrumentation
  is available. Response-to-response gaps are not request-start gaps.

The start-gap interval assumes input readiness precedes request start, which
precedes the first observed response. Under that assumption its lower bound is
current readiness minus the prior response; its upper bound is current response
minus prior readiness. Async injection, missing events or concurrency can break
that assumption. Invalid/missing bounds remain unknown. DSH request context and
OpenCode message creation are preparation evidence, not exact provider start.
The first observation in a selected window has no in-window predecessor; the
window is left-censored. Missing compaction records do not prove no compaction.

## Interpretable associations and causal limits

The built-in cache table compares transitions with the same model, prior input
of at least 100,000 tokens, input sizes within 20%, prior cache-read share at
least 80%, and no observed compaction-count change. Its outcome is current read
share below 20% and write share above 50%. It reports support, session count,
reset count, examples and counterexamples for observed gaps over 300 seconds or
at most 240 seconds, and the analogous conditional start-gap bounds. These
thresholds are descriptive hypotheses, not provider-independent cache policy.
Results are separated by harness/model and deterministic session groups. A group
split made after examining the corpus is a stability check, not an independent
prospective validation. Report cluster uncertainty and within-session contrasts
in a consumer analysis when drawing statistical conclusions.

Time ordering matters: context size and cache choice observed after a trigger
may be mediators. Tool actions and output length are post-call outcomes. Do not
control for them indiscriminately when estimating a trigger's causal effect.
Task difficulty can cause both more messages and higher expense. Long wall-clock
gaps can reflect tool latency, human pauses or provider delays. A cache reset
can reflect prefix changes, routing or model changes as well as expiration.

To test savings, specify an intervention and a task-level outcome before a new
run: for example, fetch queued messages before waking a model, or summarize a
session into a file at a predefined boundary. Randomize comparable tasks or
sessions, count all branches and summarization/retrieval calls, and measure total
cost plus completion quality, rework and elapsed time. Keep model/configuration
and workload comparable. External context saves money only if shorter future
inputs outweigh summary/retrieval costs and any extra work; cached context is
not automatically wasted context. Observational cost assigned to a candidate
pattern is neither its avoidable cost nor an estimated treatment effect.
