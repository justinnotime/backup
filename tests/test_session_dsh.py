from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from agent_skills.sessions.harnesses.dsh import DshDecoder
from agent_skills.sessions.model import SourceSnapshot

try:
    from compression import zstd
except ImportError:
    zstd = None


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


def rows(session_id: str = "fixture-session") -> list[dict]:
    return [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1_700_000_000_000,
            "cwd": "/fixture/project-alpha",
            "seedLength": 1,
            "delegationDepth": 0,
        },
        {
            "type": "user/message",
            "seq": 0,
            "time": 1_700_000_000_001,
            "data": message("seed", "user", "seed fixture", "user"),
            "surfaceOp": "append",
        },
        {
            "type": "user/message",
            "seq": 1,
            "time": 1_700_000_000_002,
            "data": message("synthetic", "user", "synthetic fixture", "plugin"),
            "surfaceOp": "append",
        },
        {
            "type": "user/message",
            "seq": 2,
            "time": 1_700_000_000_003,
            "data": message("direct", "user", "direct fixture", "user"),
            "surfaceOp": "append",
        },
        {
            "type": "reasoning-chunks",
            "seq0": 3,
            "time0": 1_700_000_000_004,
            "data": {
                "turn": 1,
                "step": 1,
                "index": 0,
                "dt": [],
                "texts": ["reasoning fixture"],
            },
        },
        {
            "type": "tool/call",
            "seq": 4,
            "time": 1_700_000_000_005,
            "data": {"fixture": True},
        },
        {
            "type": "assistant/message",
            "seq": 5,
            "time": 1_700_000_000_006,
            "data": {
                "turn": 1,
                "step": 1,
                "message": message(
                    "model-final", "assistant", "model fixture", "model"
                ),
            },
            "surfaceOp": "append",
        },
        {
            "type": "assistant/message",
            "seq": 6,
            "time": 1_700_000_000_007,
            "data": {
                "turn": 1,
                "step": 2,
                "message": message(
                    "replacement", "assistant", "replacement fixture", "model"
                ),
            },
            "surfaceOp": {"op": "replace", "start": 2, "end": 5},
            "sourceEventSeqs": [2, 5],
        },
    ]


def encode_jsonl(records: list[dict], *, final_newline: bool = True) -> bytes:
    result = "\n".join(json.dumps(record) for record in records)
    if final_newline:
        result += "\n"
    return result.encode()


def snapshot(
    payload: bytes, *, compressed: bool = False, options: dict | None = None
) -> SourceSnapshot:
    return SourceSnapshot(
        source_id="fixture-source",
        harness="dsh",
        node_label="fixture-origin",
        source_ref="fixture/session.jsonl" + (".zstd" if compressed else ""),
        path=Path("fixture/session.jsonl" + (".zstd" if compressed else "")),
        payload=payload,
        decoder_options=options or {},
    )


def diagnostic_codes(batch) -> set[str]:
    return {item.code for item in batch.diagnostics}


def test_plain_v0_keeps_direct_user_and_final_model_text_only() -> None:
    batch = DshDecoder().decode(snapshot(encode_jsonl(rows())))

    assert batch.completeness == "complete"
    assert len(batch.sessions) == 1
    session = batch.sessions[0]
    assert session.session_id == "fixture-session"
    assert session.project_hint == "project-alpha"
    assert [(event.role_hint, event.text) for event in session.events] == [
        ("user-like", "direct fixture"),
        ("assistant", "model fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def test_subagent_is_explicitly_rejected() -> None:
    records = rows()
    records[0]["origin"] = "subagent"
    records[0]["delegationDepth"] = 1

    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))

    assert batch.sessions == ()
    assert batch.rejected_sessions[0].reason_code == "DSH_SUBAGENT"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value[0].update(version=1), "DSH_HEADER_INVALID"),
        (lambda value: value[0].update(extra="unknown"), "DSH_HEADER_INVALID"),
        (lambda value: value[3].update(seq=9), "DSH_EVENT_INVALID"),
        (
            lambda value: value[4]["data"].update(dt=[1]),
            "DSH_PACKED_ROW_INVALID",
        ),
        (
            lambda value: value[6]["data"]["message"]["source"].update(model=""),
            "DSH_EVENT_INVALID",
        ),
        (
            lambda value: value[7].update(
                surfaceOp={"op": "replace", "start": 5, "end": 2}
            ),
            "DSH_SURFACE_OPERATION_INVALID",
        ),
    ],
)
def test_strict_format_validation(mutate, code: str) -> None:
    records = rows()
    mutate(records)

    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))

    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {code}


def test_unknown_required_event_invalidates_entire_batch() -> None:
    records = rows()
    records.insert(
        4,
        {
            "type": "future/required",
            "seq": 3,
            "time": 1_700_000_000_004,
            "data": {},
        },
    )
    records[5]["seq0"] = 4
    records[6]["seq"] = 5
    records[7]["seq"] = 6
    records[8]["seq"] = 7
    records[8]["surfaceOp"]["end"] = 6
    records[8]["sourceEventSeqs"] = [2, 6]

    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))

    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_UNKNOWN_REQUIRED_EVENT"}


def test_plain_torn_tail_recovers_only_complete_records() -> None:
    records = rows()
    payload = encode_jsonl(records[:-1]) + encode_jsonl([records[-1]])[:20]

    batch = DshDecoder().decode(
        snapshot(payload, options={"allow_torn_current_frame": True})
    )

    assert batch.completeness == "complete"
    assert diagnostic_codes(batch) == {"DSH_TORN_TAIL_RECOVERED"}
    assert [(event.role_hint, event.text) for event in batch.sessions[0].events] == [
        ("user-like", "direct fixture"),
        ("assistant", "model fixture"),
    ]


@pytest.mark.skipif(zstd is None, reason="compression.zstd unavailable")
def test_concatenated_zstd_frames_decode() -> None:
    records = rows()
    payload = zstd.compress(encode_jsonl(records[:1])) + zstd.compress(
        encode_jsonl(records[1:])
    )

    batch = DshDecoder().decode(snapshot(payload, compressed=True))

    assert batch.completeness == "complete"
    assert len(batch.sessions) == 1
    assert len(batch.sessions[0].events) == 2


def test_torn_frame_requires_explicit_current_frame_policy() -> None:
    payload = encode_jsonl(rows())[:-20]

    batch = DshDecoder().decode(snapshot(payload))

    assert batch.completeness == "invalid"
    assert diagnostic_codes(batch) == {"DSH_JSONL_FINAL_NEWLINE_MISSING"}


@pytest.mark.skipif(zstd is None, reason="compression.zstd unavailable")
def test_torn_current_zstd_frame_recovers_complete_records() -> None:
    records = rows()
    header = zstd.compress(encode_jsonl(records[:1]))
    repeated_ignorable = [
        {
            "type": "fixture/ignorable",
            "seq": index,
            "time": 1_700_000_000_000 + index,
            "data": {"padding": "x" * 80},
            "ignorable": True,
        }
        for index in range(2_000)
    ]
    current = zstd.compress(encode_jsonl(repeated_ignorable))

    batch = DshDecoder().decode(
        snapshot(
            header + current[:-100],
            compressed=True,
            options={"allow_torn_current_frame": True},
        )
    )

    assert batch.completeness == "incomplete"
    assert diagnostic_codes(batch) == {"DSH_TORN_TAIL_RECOVERED"}
    assert batch.rejected_sessions[0].reason_code == "DSH_NO_DIRECT_USER"
    assert batch.observations.unknown_record_counts["fixture/ignorable"] > 0


@pytest.mark.skipif(zstd is None, reason="compression.zstd unavailable")
def test_malformed_completed_zstd_frame_is_invalid() -> None:
    records = rows()
    header = zstd.compress(encode_jsonl(records[:1]))
    current = bytearray(zstd.compress(encode_jsonl(records[1:])))
    current[-1] ^= 0xFF

    batch = DshDecoder().decode(snapshot(header + bytes(current), compressed=True))

    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert "DSH_ZSTD_COMPLETED_FRAME_INVALID" in diagnostic_codes(batch)


def test_missing_zstd_support_is_reported_not_treated_as_empty() -> None:
    with mock.patch(
        "agent_skills.sessions.harnesses.dsh._zstd_module",
        side_effect=__import__(
            "agent_skills.sessions.harnesses.dsh", fromlist=["_ZstdUnavailable"]
        )._ZstdUnavailable,
    ):
        batch = DshDecoder().decode(snapshot(b"fixture", compressed=True))

    assert batch.completeness == "incomplete"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_ZSTD_CAPABILITY_MISSING"}


def test_capabilities_report_zstd_support_explicitly() -> None:
    decoder = DshDecoder()
    with mock.patch(
        "agent_skills.sessions.harnesses.dsh._zstd_module",
        side_effect=__import__(
            "agent_skills.sessions.harnesses.dsh", fromlist=["_ZstdUnavailable"]
        )._ZstdUnavailable,
    ):
        assert decoder.capabilities() == ("plain-jsonl",)

    if zstd is not None:
        assert decoder.capabilities() == ("plain-jsonl", "zstd")


def test_diagnostics_never_include_path_or_transcript() -> None:
    batch = DshDecoder().decode(snapshot(b"not-json\n"))
    rendered = repr(batch.diagnostics)

    assert "fixture/session" not in rendered
    assert "not-json" not in rendered
