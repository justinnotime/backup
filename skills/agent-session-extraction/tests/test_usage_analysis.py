"""Synthetic billing, trigger and source-boundary regression cases."""

import gzip
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.harnesses.claude import DECODER as CLAUDE
from agent_skills.sessions.model import SourceSnapshot
from agent_skills.sessions.telemetry import TOKENS, decode_telemetry
from agent_skills.sessions.telemetry_features import FeatureRules, Trace
from agent_skills.sessions.usage_analysis import enrich, price, run_analysis, summarize


def snapshot(harness, records, **options):
    return SourceSnapshot(
        "source-a",
        harness,
        "node-a",
        "source-a/example.jsonl",
        Path("/example.jsonl"),
        "".join(json.dumps(r) + "\n" for r in records).encode(),
        options,
    )


def assistant(output=4, **fields):
    return dict(
        type="assistant",
        sessionId="session-a",
        timestamp="2026-02-01T00:00:05Z",
        requestId="request-a",
        message={
            "id": "message-a",
            "model": "example-model",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-a",
                    "name": "Read",
                    "input": {"path": "private-sentinel"},
                }
            ],
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 8,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 5,
                    "ephemeral_1h_input_tokens": 3,
                },
                "output_tokens": output,
            },
        },
        **fields,
    )


def test_streaming_does_not_sum_and_ignores_prompt_retention():
    records = [assistant(1), assistant(4)]
    snap = snapshot("claude-code", records)
    assert not CLAUDE.decode(snap).sessions  # No retained user conversation.
    data = decode_telemetry(snap)
    assert len(data.usage) == 1
    row = data.usage[0]
    assert [row[k] for k in TOKENS] == [2, 10, 5, 3, 0, 4]
    assert row["tool_count"] == 1
    assert row["actions"] == ["inspect-files"]
    assert "private-sentinel" not in json.dumps(data.usage)


def test_fallback_uses_iterations_instead_of_stale_top_level():
    record = assistant()
    record["message"]["usage"].update(
        cache_creation_input_tokens=0,
        iterations=[
            {"model": "model-a", "input_tokens": 1, "output_tokens": 2},
            {"model": "model-b", "input_tokens": 3, "output_tokens": 4},
        ],
    )
    rows = decode_telemetry(snapshot("claude-code", [record])).usage
    assert [(r["model"], r["fresh"], r["output"], r["write5"]) for r in rows] == [
        ("model-a", 1, 2, 0),
        ("model-b", 3, 4, 0),
    ]


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1},
        {"output_tokens": True},
        {
            "cache_creation_input_tokens": 2,
            "cache_creation": {"ephemeral_1h_input_tokens": 3},
        },
    ],
)
def test_invalid_partitions_are_excluded_and_counted(usage):
    record = assistant()
    record["message"]["usage"].update(usage)
    data = decode_telemetry(snapshot("claude-code", [record]))
    assert not data.usage
    assert data.counts["invalid-usage"] == 1


def test_notification_is_separate_from_task_author_and_inbox_return():
    rules = FeatureRules({"action_rules": {"coordinate-receive": r"fetch_inbox"}})
    trace = Trace("claude-code", "session-a", rules)
    trace.input("implement example", "2026-02-01T00:00:00Z", 1, native="human")
    trace.input(
        "<task-notification>ready</task-notification>", "2026-02-01T00:06:00Z", 2
    )
    call = trace.call("a", "2026-02-01T00:06:02Z")
    trace.tool(call, "t", "Bash", {"command": "fetch_inbox"})
    trace.result("2026-02-01T00:06:03Z", "t")
    next_call = trace.call("b", "2026-02-01T00:06:05Z")
    assert call["task_origin"] == "human" and call["wake_origin"] == "notification"
    assert call["input_kind"] == "notification"
    assert next_call["input_kind"] == "inbox-result"
    # A fetched inbox is not automatically a new human or peer task.
    assert next_call["task_id"] == call["task_id"]
    assert (
        rules.origin("<teammate-message>task</teammate-message>", native="human")[0]
        == "peer"
    )


def codex_event(kind, **payload):
    return {
        "type": "event_msg",
        "timestamp": "2026-02-01T00:01:00Z",
        "payload": dict(type=kind, **payload),
    }


def test_codex_counters_reasoning_and_dual_user_streams():
    records = [
        {"type": "session_meta", "payload": {"id": "session-a"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-a", "model": "example-model"},
        },
        codex_event("user_message", message="private-sentinel"),
        {
            "type": "response_item",
            "timestamp": "2026-02-01T00:01:00.200Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "private-sentinel"}],
            },
        },
    ]
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "output_tokens": 10,
        "reasoning_output_tokens": 7,
        "total_tokens": 110,
    }
    records += [
        codex_event(
            "token_count", info={"total_token_usage": usage, "last_token_usage": usage}
        )
    ] * 2
    rows = decode_telemetry(snapshot("codex", records))
    assert len(rows.usage) == len(rows.events) == 1
    assert rows.usage[0]["fresh"] == 20 and rows.usage[0]["output"] == 10
    assert rows.usage[0]["reasoning"] == 7
    assert rows.counts["unchanged-or-empty-counter"] == 1
    assert "private-sentinel" not in json.dumps(rows.events)


def test_codex_fork_excludes_inherited_usage_and_keeps_child_work():
    parent_turn = "01950000-0000-7000-8000-000000000001"
    child = "01950000-0010-7000-8000-000000000001"
    child_turn = "01950000-0020-7000-8000-000000000001"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": child,
                "forked_from_id": "parent-a",
                "source": {"subagent": "example"},
            },
        }
    ]
    for turn, total in [(parent_turn, 110), (child_turn, 220)]:
        records += [
            {"type": "turn_context", "payload": {"turn_id": turn}},
            codex_event(
                "token_count",
                info={
                    "total_token_usage": {"total_tokens": total},
                    "last_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                },
            ),
        ]
    data = decode_telemetry(snapshot("codex", records))
    assert len(data.usage) == 1 and data.usage[0]["child"]
    assert data.counts["inherited-or-unknown-fork-usage-excluded"] == 1


def test_dsh_terminal_usage_is_disjoint_and_retains_subagent():
    records = [
        {"type": "session", "version": 0, "id": "child-a", "parentSession": "parent-a"},
        {
            "type": "assistant/chunk",
            "seq": 0,
            "time": 1000,
            "data": {"usage": {"outputTokens": 999}},
        },
        {
            "type": "assistant/message",
            "seq": 1,
            "time": 2000,
            "data": {
                "usage": {
                    "inputTokens": 10,
                    "cacheReadTokens": 30,
                    "cacheWriteTokens": 20,
                    "outputTokens": 8,
                    "reasoningTokens": 6,
                }
            },
        },
    ]
    rows = decode_telemetry(snapshot("dsh", records)).usage
    assert len(rows) == 1 and rows[0]["child"]
    assert [rows[0][k] for k in TOKENS] == [10, 30, 0, 0, 20, 8]


def test_opencode_message_usage_without_text_parts_and_child_session():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE session(id,directory,parent_id); CREATE TABLE message(id,session_id,time_created,data); CREATE TABLE part(id,message_id,time_created,data);"
    )
    db.execute(
        "INSERT INTO session VALUES (?,?,?)", ("child-a", "/synthetic", "parent-a")
    )
    msg = {
        "role": "assistant",
        "modelID": "example-model",
        "tokens": {
            "input": 3,
            "output": 5,
            "reasoning": 7,
            "cache": {"read": 11, "write": 13},
        },
    }
    db.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("message-a", "child-a", 1000, json.dumps(msg)),
    )
    snap = replace(snapshot("opencode", []), payload=db.serialize())
    db.close()
    rows = decode_telemetry(snap).usage
    assert len(rows) == 1 and rows[0]["child"]
    assert rows[0]["output"] == 12 and rows[0]["fresh"] == 3


def test_unsupported_is_unknown_not_zero_cost():
    batch = decode_telemetry(snapshot("cursor", []))
    assert not batch.usage and batch.counts["unsupported-usage-harness"] == 1


def test_rates_and_dimensions_conserve_cost_without_double_counting_actions():
    row = decode_telemetry(snapshot("claude-code", [assistant()])).usage[0]
    rates = [{"model": "example-model", "per_million": dict.fromkeys(TOKENS, 2)}]
    row["actions"] = ["edit", "test"]
    assert price(row, []) is None
    assert price(row, rates * 2) is None
    rows = enrich([row], rates)
    report = summarize(rows)
    expected = 24 * 2 / 1_000_000
    assert report["reference_usd"] == pytest.approx(expected, rel=1e-12, abs=1e-15)
    for dimension in {d["dimension"] for d in report["dimensions"]}:
        assert sum(
            d["reference_usd"]
            for d in report["dimensions"]
            if d["dimension"] == dimension
        ) == pytest.approx(expected, rel=1e-12, abs=1e-15)


def test_cache_comparison_uses_bounds_and_reports_counterexamples():
    template = decode_telemetry(snapshot("claude-code", [assistant()])).usage[0]
    rows = []
    for i in range(3):
        row = {
            **template,
            "usage_key": str(i),
            "line": i,
            "time": f"2026-02-01T00:{i * 6:02}:20+00:00",
            "fresh": 0,
            "read": 200_000,
            "write5": 0,
            "write1": 0,
            "write_unknown": 0,
            "ready_at": i * 360,
            "first_response_at": i * 360 + 20,
        }
        rows.append(row)
    rows[2].update(read=0, write5=200_000)
    report = summarize(enrich(rows))
    observed = next(
        v for v in report["cache_rules"] if v["condition"] == "observed_gap_gt_300"
    )
    assert observed["observations"] == 2 and observed["resets"] == 1
    assert observed["examples"] and observed["counterexamples"]
    assert rows[1]["start_gap_lower_seconds"] == 340
    assert rows[1]["start_gap_upper_seconds"] == 380


def test_manifest_cli_scope_private_outputs_and_retention_independence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    raw = snapshot("claude-code", [assistant()]).payload
    (source / "one.jsonl").write_bytes(raw)
    (source / "mirror.jsonl").write_bytes(raw)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(snapshot("claude-code", [assistant(99)]).payload)
    manifest = manifest_data(source, tmp_path / "archive")
    manifest["event_policy"]["min_user_chars"] = 1000
    manifest_path = write_manifest(tmp_path / "manifest.json", manifest)
    output = tmp_path / "analysis"
    result = run_analysis(
        manifest_path, output, start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"
    )
    assert result["observations"] == 1
    report = json.loads((output / "summary.json").read_text())
    assert report["diagnostics"]["duplicate-source-bytes"] == 1
    assert report["tokens"]["output"] == 4
    assert not (tmp_path / "archive").exists()
    assert (source / "one.jsonl").read_bytes() == raw
    with gzip.open(output / "usage.jsonl.gz", "rt") as stream:
        assert len(stream.readlines()) == 1
    assert (output.stat().st_mode & 0o777) == 0o700
    with pytest.raises(FileExistsError):
        run_analysis(
            manifest_path,
            output,
            start="2026-02-01T00:00:00Z",
            end="2026-03-01T00:00:00Z",
        )


def test_missing_usage_fields_cannot_become_zero_cost():
    record = assistant()
    record["message"]["usage"] = {"unknown_future_counter": 100}
    batch = decode_telemetry(snapshot("claude-code", [record]))
    assert not batch.usage and batch.counts["invalid-usage"] == 1


def test_analysis_output_cannot_enter_source_or_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manifest_path = write_manifest(
        tmp_path / "manifest.json", manifest_data(source, tmp_path / "archive")
    )
    for output in (source / "analysis", tmp_path / "archive/History/analysis"):
        with pytest.raises(ValueError, match="overlaps"):
            run_analysis(
                manifest_path,
                output,
                start="2026-02-01T00:00:00Z",
                end="2026-03-01T00:00:00Z",
            )
        assert not output.exists()
