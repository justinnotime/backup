"""Usage and activity observed before transcript retention; no model calls.

Results are private data even though prompt and tool text are not retained.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field

from .harnesses import decoder_for
from .telemetry_features import (
    FeatureRules,
    Trace,
    call_features,
    content_text,
    digest,
    iso,
    seconds,
)

TOKENS = ("fresh", "read", "write5", "write1", "write_unknown", "output")
SUPPORTED = frozenset(("claude-code", "codex", "dsh", "opencode"))


def normalize_usage(harness, usage):
    """Return disjoint categories. Reasoning is included in output exactly once."""
    required = {
        "claude-code": ("input_tokens", "output_tokens"),
        "codex": ("input_tokens", "output_tokens"),
        "dsh": ("inputTokens", "outputTokens"),
        "opencode": ("input", "output"),
    }.get(harness)
    if (
        required is None
        or not isinstance(usage, dict)
        or any(k not in usage for k in required)
    ):
        raise ValueError("missing native usage fields")
    result = dict.fromkeys(TOKENS, 0)
    if harness == "claude-code":
        cache = usage.get("cache_creation") or {}
        result.update(
            fresh=usage.get("input_tokens", 0),
            read=usage.get("cache_read_input_tokens", 0),
            write5=cache.get("ephemeral_5m_input_tokens", 0),
            write1=cache.get("ephemeral_1h_input_tokens", 0),
            output=usage.get("output_tokens", 0),
        )
        result["write_unknown"] = (
            usage.get("cache_creation_input_tokens", 0)
            - result["write5"]
            - result["write1"]
        )
    elif harness == "codex":
        result.update(
            read=usage.get("cached_input_tokens", 0),
            write_unknown=usage.get("cache_write_input_tokens", 0),
            output=usage.get("output_tokens", 0),
        )
        result["fresh"] = (
            usage.get("input_tokens", 0) - result["read"] - result["write_unknown"]
        )
    elif harness == "dsh":
        result.update(
            fresh=usage.get("inputTokens", 0),
            read=usage.get("cacheReadTokens", 0),
            write_unknown=usage.get("cacheWriteTokens", 0),
            output=usage.get("outputTokens", 0),
        )
    elif harness == "opencode":
        cache = usage.get("cache") or {}
        result.update(
            fresh=usage.get("input", 0),
            read=cache.get("read", 0),
            write_unknown=cache.get("write", 0),
            output=usage.get("output", 0) + usage.get("reasoning", 0),
        )
    else:
        raise ValueError("unsupported usage format")
    if any(
        isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in result.values()
    ):
        raise ValueError("invalid token partition")
    return result


def uuid_ms(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.int >> 80 if parsed.version == 7 else None
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass
class TelemetryBatch:
    usage: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    decode_status: str = "not-decoded"


class _Observer:
    def __init__(self, snapshot, rules):
        self.snapshot, self.rules = snapshot, rules
        self.batch = TelemetryBatch()
        self.traces = {}
        self.rows = {}

    def trace(self, session, child=False):
        if session not in self.traces:
            self.traces[session] = Trace(
                self.snapshot.harness, session, self.rules, child
            )
        return self.traces[session]

    def add(self, key, trace, at, model, usage, line, cwd, call, **extra):
        try:
            tokens = normalize_usage(self.snapshot.harness, usage)
        except (ValueError, TypeError, AttributeError):
            self.batch.counts["invalid-usage"] += 1
            return
        row = dict(
            schema_version="agent-usage/v1",
            usage_key=digest(key),
            harness=self.snapshot.harness,
            session=trace.session,
            time=iso(seconds(at)),
            model=model or "unknown",
            node=self.snapshot.node_label,
            source_ref=self.snapshot.source_ref,
            line=line,
            project_candidates=self.rules.labels("project_rules", cwd or ""),
            child=trace.child,
            **tokens,
            **extra,
        )
        row["_call"] = call
        previous = self.rows.get(row["usage_key"])
        if previous:
            self.batch.counts["duplicate-usage"] += 1
            if row["harness"] != "claude-code" or row["output"] <= previous["output"]:
                return
        self.rows[row["usage_key"]] = row

    def jsonl(self, records, *, malformed=0):
        self.batch.counts["malformed-or-torn-records"] += malformed
        harness = self.snapshot.harness
        session = self.snapshot.source_ref
        model, cwd, turn = None, "", None
        rootmeta, previous_total, pending = {}, None, None
        own, child = True, False
        # Native UUID time distinguishes inherited Codex turns after forking;
        # unknown ownership is excluded and counted, never guessed billable.
        for sequence, record in records:
            line = sequence + 1
            typ = record.get("type")
            at = record.get("timestamp", record.get("time"))
            if harness == "claude-code":
                session = record.get("sessionId", session)
                if record.get("agentId"):
                    session += "/" + record["agentId"]
                child = (
                    bool(record.get("isSidechain"))
                    or "/subagents/" in self.snapshot.source_ref
                )
                trace = self.trace(session, child)
                cwd = record.get("cwd", cwd)
                msg = record.get("message") or {}
                blocks = msg.get("content", [])
                if typ == "user":
                    results = (
                        [
                            b
                            for b in blocks
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        ]
                        if isinstance(blocks, list)
                        else []
                    )
                    if results:
                        for b in results:
                            trace.result(at, b.get("tool_use_id"))
                    else:
                        native = record.get("origin") or {}
                        trace.input(
                            content_text(blocks),
                            at,
                            line,
                            native.get("kind", "") if isinstance(native, dict) else "",
                            system=bool(record.get("isMeta")),
                        )
                if typ == "system" and record.get("subtype") == "compact_boundary":
                    trace.compact()
                if typ != "assistant":
                    continue
                msgid = msg.get("id") or record.get("uuid")
                if not msgid:
                    msgid = (self.snapshot.source_ref, line)
                    self.batch.counts["missing-message-id"] += 1
                call = trace.call(digest((msgid, record.get("requestId"))), at)
                for b in blocks if isinstance(blocks, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        trace.tool(
                            call, b.get("id"), b.get("name", ""), b.get("input", {})
                        )
                usage = msg.get("usage")
                if not usage:
                    self.batch.counts["assistant-without-usage"] += 1
                    continue
                # Fallback iterations replace top-level totals, whose cache
                # partitions can describe a different model's attempt.
                iterations = usage.get("iterations") or [usage]
                for index, value in enumerate(iterations):
                    self.add(
                        (harness, msgid, record.get("requestId"), index),
                        trace,
                        at,
                        value.get("model", msg.get("model")),
                        value,
                        line,
                        cwd,
                        call,
                        iteration=index,
                        tier=usage.get("service_tier", "unknown"),
                        reasoning=(usage.get("output_tokens_details") or {}).get(
                            "thinking_tokens"
                        )
                        if len(iterations) == 1
                        else None,
                    )
            elif harness == "codex":
                p = record.get("payload") or {}
                if typ == "session_meta" and not rootmeta:
                    rootmeta = p
                    session, cwd = p.get("id", session), p.get("cwd", "")
                    child = (
                        isinstance(p.get("source"), dict) and "subagent" in p["source"]
                    )
                    own = not rootmeta.get("forked_from_id")
                trace = self.trace(session, child)
                if typ == "turn_context":
                    turn, model, cwd = (
                        p.get("turn_id"),
                        p.get("model", model),
                        p.get("cwd", cwd),
                    )
                    born, turnborn = uuid_ms(session), uuid_ms(turn)
                    own = not rootmeta.get("forked_from_id") or (
                        born is not None and turnborn is not None and turnborn >= born
                    )
                if not own:
                    if (
                        typ == "event_msg"
                        and p.get("type") == "token_count"
                        and p.get("info")
                    ):
                        previous_total = p["info"].get("total_token_usage")
                        self.batch.counts[
                            "inherited-or-unknown-fork-usage-excluded"
                        ] += 1
                    continue
                if typ == "compacted":
                    trace.compact()
                if typ == "event_msg" and p.get("type") == "user_message":
                    trace.input(
                        content_text(p.get("message", "")),
                        at,
                        line,
                        representation="event",
                    )
                if typ == "response_item":
                    pt = p.get("type")
                    if pt == "message" and p.get("role") in ("user", "developer"):
                        text = content_text(p.get("content", ""))
                        trace.input(
                            text,
                            at,
                            line,
                            system=p.get("role") == "developer",
                            representation="response",
                        )
                    elif pt in ("function_call_output", "custom_tool_call_output"):
                        trace.result(at, p.get("call_id"))
                    elif pt in ("function_call", "custom_tool_call", "reasoning") or (
                        pt == "message" and p.get("role") == "assistant"
                    ):
                        if pending is None:
                            pending = trace.call(("operation", line), at)
                        if pt in ("function_call", "custom_tool_call"):
                            trace.tool(
                                pending,
                                p.get("call_id"),
                                p.get("name", ""),
                                p.get("arguments", p.get("input", "")),
                            )
                if (
                    typ != "event_msg"
                    or p.get("type") != "token_count"
                    or not p.get("info")
                ):
                    continue
                info = p["info"]
                total, usage = (
                    info.get("total_token_usage"),
                    info.get("last_token_usage"),
                )
                if not usage or total == previous_total:
                    self.batch.counts["unchanged-or-empty-counter"] += 1
                    continue
                if (
                    previous_total
                    and total
                    and total.get("total_tokens", 0)
                    - previous_total.get("total_tokens", 0)
                    != usage.get(
                        "total_tokens",
                        usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    )
                ):
                    self.batch.counts["nonmatching-cumulative-delta"] += 1
                previous_total = total
                if not turn or total is None:
                    self.batch.counts["usage-without-turn-or-total"] += 1
                    continue
                call = (
                    pending
                    if pending is not None
                    else trace.call(("operation", line), at)
                )
                self.add(
                    (harness, turn, digest(total)),
                    trace,
                    at,
                    model,
                    usage,
                    line,
                    cwd,
                    call,
                    reasoning=usage.get("reasoning_output_tokens"),
                )
                pending = None
            elif harness == "dsh":
                p = record.get("data") or {}
                if typ == "session":
                    session, cwd = record.get("id", session), record.get("cwd", "")
                    child = (
                        bool(record.get("parentSession"))
                        or record.get("origin") == "subagent"
                    )
                trace = self.trace(session, child)
                if typ == "user/message":
                    native = p.get("source") or {}
                    trace.input(
                        content_text(p.get("content", "")),
                        at,
                        line,
                        native.get("kind", ""),
                    )
                if typ == "request/context":
                    model = p.get("model", model)
                    pending = trace.call(("operation", line), at)
                    # Request preparation is a lower bound, not proof of the
                    # provider's request-start or cache lookup time.
                    pending["ready_at"] = seconds(at)
                    pending["first_response_at"] = None
                if typ == "request/header":
                    model = (p.get("header", {}).get("config") or {}).get(
                        "model", model
                    )
                if typ in ("tool/result", "tool/response"):
                    trace.result(at, p.get("id"))
                if typ == "tool/call":
                    if pending is None:
                        pending = trace.call(("operation", line), at)
                    trace.tool(
                        pending, p.get("id"), p.get("name", ""), p.get("arguments", {})
                    )
                if typ == "assistant/message":
                    msg = p.get("message") or {}
                    usage = p.get("usage") or msg.get("usage")
                    if not usage:
                        self.batch.counts["assistant-without-usage"] += 1
                        continue
                    call = (
                        pending
                        if pending is not None
                        else trace.call(("operation", line), at)
                    )
                    call["first_response_at"] = seconds(at)
                    self.add(
                        (harness, session, record.get("seq", line)),
                        trace,
                        at,
                        model,
                        usage,
                        line,
                        cwd,
                        call,
                        reasoning=usage.get("reasoningTokens"),
                    )
                    pending = None

    def sqlite(self, db):
        sessions = {
            r["id"]: dict(r)
            for r in db.execute("SELECT id,directory,parent_id FROM session")
        }
        parts = {}
        for row in db.execute(
            "SELECT message_id,data FROM part ORDER BY time_created,id"
        ):
            try:
                parts.setdefault(row["message_id"], []).append(json.loads(row["data"]))
            except (ValueError, TypeError):
                self.batch.counts["malformed-parts"] += 1
        parents = {}
        for row in db.execute(
            "SELECT id,session_id,time_created,data FROM message ORDER BY time_created,id"
        ):
            mid, sid, at = row["id"], row["session_id"], row["time_created"]
            try:
                msg = json.loads(row["data"])
            except (ValueError, TypeError):
                self.batch.counts["malformed-messages"] += 1
                continue
            meta = sessions.get(sid, {})
            trace = self.trace(sid, bool(meta.get("parent_id")))
            blocks = parts.get(mid, [])
            if msg.get("role") == "user":
                trace.input(
                    "\n".join(
                        b.get("text", "")
                        for b in blocks
                        if isinstance(b, dict) and b.get("type") == "text"
                    ),
                    at,
                    mid,
                )
                parents[mid] = trace.task
            if msg.get("role") != "assistant":
                continue
            if not msg.get("tokens"):
                self.batch.counts["assistant-without-usage"] += 1
                continue
            trace.task = parents.get(msg.get("parentID"), trace.task)
            call = trace.call(mid, at)
            call["ready_at"] = seconds((msg.get("time") or {}).get("created", at))
            call["first_response_at"] = seconds(
                (msg.get("time") or {}).get("completed")
            )
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool":
                    trace.tool(
                        call,
                        b.get("callID"),
                        b.get("tool", ""),
                        (b.get("state") or {}).get("input", {}),
                    )
            self.add(
                ("opencode", mid),
                trace,
                at,
                msg.get("modelID"),
                msg["tokens"],
                mid,
                meta.get("directory", ""),
                call,
                reasoning=msg["tokens"].get("reasoning"),
                reported_cost=msg.get("cost"),
            )

    def finish(self):
        for row in self.rows.values():
            row.update(call_features(row.pop("_call")))
            self.batch.usage.append(row)
        for trace in self.traces.values():
            for event in trace.events:
                self.batch.events.append(
                    dict(
                        schema_version="agent-activity/v1",
                        harness=trace.harness,
                        session=trace.session,
                        source_ref=self.snapshot.source_ref,
                        **{k: v for k, v in event.items() if not k.startswith("_")},
                    )
                )
        return self.batch


def decode_telemetry(snapshot, *, config=None):
    """Decode a caller-frozen snapshot; direct paths need source revalidation.

    Archive filtering and archive schema diagnostics never silently erase usage.
    Unsupported or undecodable sources explicitly report unknown coverage.
    """
    if snapshot.harness not in SUPPORTED:
        return TelemetryBatch(counts=Counter({"unsupported-usage-harness": 1}))
    observer = _Observer(snapshot, FeatureRules(config))
    batch = decoder_for(snapshot.harness).decode(snapshot, observer=observer)
    result = observer.finish()
    result.decode_status = batch.completeness
    for diagnostic in batch.diagnostics:
        result.counts["decoder:" + diagnostic.code] += diagnostic.count or 1
    if not result.usage:
        result.counts["no-observed-usage"] += 1
    return result
