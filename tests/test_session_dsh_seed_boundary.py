from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_skills.sessions.harnesses.dsh import DshDecoder
from agent_skills.sessions.model import SourceSnapshot


def message(message_id: str, role: str, text: str, source_kind: str) -> dict:
    source = {"kind": source_kind}
    if source_kind == "model":
        source.update({"provider": "fixture-provider", "model": "fixture-model"})
    return {
        "id": message_id,
        "role": role,
        "content": [{"type": "text", "text": text}],
        "source": source,
    }


def event(record_type: str, sequence: int, data: dict, **extra) -> dict:
    return {
        "type": record_type,
        "seq": sequence,
        "time": 1_700_000_000_000 + sequence,
        "data": data,
        **extra,
    }


def header() -> dict:
    return {
        "type": "session",
        "version": 0,
        "id": "fixture-seed-boundary",
        "createdAt": 1_700_000_000_000,
        "cwd": "/fixture/project-alpha",
        "delegationDepth": 0,
    }


def snapshot(records: list[dict]) -> SourceSnapshot:
    payload = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
    return SourceSnapshot(
        source_id="fixture-source",
        harness="dsh",
        node_label="fixture-node",
        source_ref="fixture-source/session.jsonl",
        path=Path("fixture/session.jsonl"),
        payload=payload,
    )


def diagnostic_codes(batch) -> set[str]:
    return {diagnostic.code for diagnostic in batch.diagnostics}


def test_end_seed_without_rollback_excludes_all_inherited_events() -> None:
    records = [
        header(),
        event(
            "user/message",
            0,
            message("seed-user", "user", "inherited fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 1, {}),
        event(
            "user/message",
            2,
            message("direct-user", "user", "direct fixture", "user"),
            surfaceOp="append",
        ),
        event(
            "assistant/message",
            3,
            {
                "message": message(
                    "direct-assistant", "assistant", "answer fixture", "model"
                )
            },
            surfaceOp="append",
        ),
    ]

    batch = DshDecoder().decode(snapshot(records))

    assert batch.completeness == "complete"
    assert [(item.role_hint, item.text) for item in batch.sessions[0].events] == [
        ("user-like", "direct fixture"),
        ("assistant", "answer fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def test_multiple_end_seed_markers_each_start_a_new_inherited_boundary() -> None:
    records = [
        header(),
        event(
            "user/message",
            0,
            message("first-seed-user", "user", "first inherited fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 1, {}),
        event("subagent/descriptor", 2, {}),
        event(
            "user/message",
            3,
            message("second-seed-user", "user", "second inherited fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 4, {}),
        event(
            "user/message",
            5,
            message("direct-user", "user", "direct fixture", "user"),
            surfaceOp="append",
        ),
        event(
            "assistant/message",
            6,
            {
                "message": message(
                    "direct-assistant", "assistant", "answer fixture", "model"
                )
            },
            surfaceOp="append",
        ),
    ]

    batch = DshDecoder().decode(snapshot(records))

    assert batch.completeness == "complete"
    assert [(item.role_hint, item.text) for item in batch.sessions[0].events] == [
        ("user-like", "direct fixture"),
        ("assistant", "answer fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def test_explicit_seed_length_remains_authoritative_over_later_markers() -> None:
    records = [
        {**header(), "seedLength": 1},
        event(
            "user/message",
            0,
            message("seed-user", "user", "inherited fixture", "user"),
            surfaceOp="append",
        ),
        event(
            "user/message",
            1,
            message("direct-user", "user", "direct fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 2, {}),
        event(
            "assistant/message",
            3,
            {
                "message": message(
                    "direct-assistant", "assistant", "answer fixture", "model"
                )
            },
            surfaceOp="append",
        ),
    ]

    batch = DshDecoder().decode(snapshot(records))

    assert batch.completeness == "complete"
    assert [(item.role_hint, item.text) for item in batch.sessions[0].events] == [
        ("user-like", "direct fixture"),
        ("assistant", "answer fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def fallback_records() -> list[dict]:
    return [
        header(),
        event(
            "user/message",
            0,
            message("old-seed-user", "user", "older inherited fixture", "user"),
            surfaceOp="append",
        ),
        event(
            "assistant/message",
            1,
            {
                "message": message(
                    "old-seed-assistant",
                    "assistant",
                    "older inherited answer",
                    "model",
                )
            },
            surfaceOp="append",
        ),
        event(
            "user/message",
            2,
            message("boundary-user", "user", "current direct fixture", "user"),
            surfaceOp="append",
        ),
        event("step/end", 3, {}),
        event("turn/end", 4, {}),
        event("session/end-seed", 5, {}),
        event("assistant/chunk", 3, {"text": "non-final fixture"}),
        {
            "type": "tool-call-chunks",
            "seq0": 4,
            "time0": 1_700_000_000_004,
            "data": {
                "turn": 1,
                "step": 1,
                "index": 0,
                "dt": [],
                "args": ["synthetic argument"],
                "id": "fixture-call",
                "name": "fixture-tool",
            },
        },
        event(
            "assistant/message",
            5,
            {
                "message": message(
                    "final-assistant", "assistant", "final answer fixture", "model"
                )
            },
            surfaceOp="append",
        ),
    ]


def test_strict_end_seed_rollback_keeps_boundary_user_and_rebuilds_state() -> None:
    batch = DshDecoder().decode(snapshot(fallback_records()))

    assert batch.completeness == "complete"
    assert [
        (item.source_sequence, item.role_hint, item.text)
        for item in batch.sessions[0].events
    ] == [
        (2, "user-like", "current direct fixture"),
        (5, "assistant", "final answer fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def test_strict_rollback_does_not_promote_subagent_input() -> None:
    records = fallback_records()
    records[3]["data"]["source"]["kind"] = "subagent-settled"

    batch = DshDecoder().decode(snapshot(records))

    assert batch.completeness == "complete"
    assert batch.sessions == ()
    assert batch.rejected_sessions[0].reason_code == "DSH_NO_DIRECT_USER"
    assert batch.observations.recognizable_user_markers == 0
    assert batch.observations.accepted_direct_user_events == 0
    assert diagnostic_codes(batch) == set()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda records: records.pop(5),
        lambda records: records[7].update(type="tool/call"),
        lambda records: records[7].update(seq=4),
        lambda records: records[6].update(data={"unexpected": True}),
    ),
    ids=(
        "missing-turn-end",
        "first-replayed-record-is-not-assistant-chunk",
        "restart-sequence-is-not-boundary-user-plus-one",
        "end-seed-data-is-not-empty",
    ),
)
def test_other_sequence_rollbacks_remain_invalid(mutate) -> None:
    records = fallback_records()
    mutate(records)

    batch = DshDecoder().decode(snapshot(records))

    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_EVENT_INVALID"}
