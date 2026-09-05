"""Transcript-free reconciliation over decoded sources and planned output."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .audit import OutputInventory
from .manifest import Manifest
from .model import (
    Diagnostic,
    ExtractionSnapshot,
    PublicationPlan,
    ReconcileReport,
    SourceSnapshot,
)
from .redact import Redactor

RECONCILIATION_CODES = {
    "ACCEPTED_SESSION_WITHOUT_OUTPUT",
    "RECOGNIZED_MARKER_WITHOUT_INPUT",
    "UNKNOWN_MESSAGE_FORMAT",
    "DECODER_CANARY_FAILED",
    "CODEX_STREAM_DIVERGENCE",
    "DUPLICATE_SESSION_DIVERGENCE",
    "SOURCE_INVALID_OR_UNREADABLE",
    "DECODER_FAILURE",
}

_CANARY = "gsk-SYNTHETIC000000CANARY"


def _jsonl(records: list[dict]) -> bytes:
    return b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in records)


def _snapshot(harness: str, payload: bytes, name: str) -> SourceSnapshot:
    return SourceSnapshot(
        f"canary-{harness}",
        harness,  # type: ignore[arg-type]
        "canary-node",
        f"canary-{harness}/{name}",
        Path(name),
        payload,
        {"session_id": f"canary-{harness}-{name}", "project_hint": "canary-project"},
    )


def _opencode_canary() -> bytes:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, time_created INTEGER, data TEXT);
            """
        )
        connection.execute(
            "INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
            ("canary-opencode", "Canary", "/canary/project", 1, 2),
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            ("message", "canary-opencode", 1, json.dumps({"role": "user"})),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?)",
            ("part", "message", 1, json.dumps({"type": "text", "text": _CANARY})),
        )
        connection.commit()
        if not hasattr(connection, "serialize"):
            raise RuntimeError("sqlite serialize unavailable")
        return connection.serialize()
    finally:
        connection.close()


def _dsh_canary() -> bytes:
    user = {
        "id": "canary-user",
        "role": "user",
        "content": [{"type": "text", "text": _CANARY}],
        "source": {"kind": "user"},
    }
    records = [
        {
            "type": "session",
            "version": 0,
            "id": "canary-dsh",
            "createdAt": 1,
            "cwd": "/canary/project",
            "seedLength": 0,
            "delegationDepth": 0,
        },
        {
            "type": "user/message",
            "seq": 0,
            "time": 1,
            "data": user,
            "surfaceOp": "append",
        },
    ]
    return _jsonl(records)


def decoder_canary_self_test(
    manifest: Manifest, redactor: Redactor
) -> tuple[Diagnostic, ...]:
    """Pass synthetic credential canaries through each enabled real decoder."""
    from .harnesses import decoder_for

    enabled = {source.harness for source in manifest.sources if source.enabled}
    candidates: dict[str, list[SourceSnapshot]] = {}
    if "claude-code" in enabled:
        candidates["claude-code"] = [
            _snapshot(
                "claude-code",
                _jsonl(
                    [
                        {
                            "type": "user",
                            "sessionId": "canary-claude",
                            "message": {"content": _CANARY},
                        }
                    ]
                ),
                "claude.jsonl",
            )
        ]
    if "codex" in enabled:
        meta = {
            "type": "session_meta",
            "payload": {"id": "canary-codex", "source": "cli"},
        }
        candidates["codex"] = [
            _snapshot(
                "codex",
                _jsonl(
                    [
                        meta,
                        {
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": _CANARY},
                        },
                    ]
                ),
                "legacy.jsonl",
            ),
            _snapshot(
                "codex",
                _jsonl(
                    [
                        meta,
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "item": {
                                    "type": "UserMessage",
                                    "content": [{"type": "text", "text": _CANARY}],
                                },
                            },
                        },
                    ]
                ),
                "items.jsonl",
            ),
            _snapshot(
                "codex",
                _jsonl(
                    [
                        meta,
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": _CANARY}],
                            },
                        },
                    ]
                ),
                "response.jsonl",
            ),
        ]
    if "opencode" in enabled:
        candidates["opencode"] = [
            _snapshot("opencode", _opencode_canary(), "opencode.db")
        ]
    if "dsh" in enabled:
        candidates["dsh"] = [_snapshot("dsh", _dsh_canary(), "session.jsonl")]
    if "cursor" in enabled:
        candidates["cursor"] = [
            _snapshot(
                "cursor",
                _jsonl(
                    [
                        {
                            "role": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"<user_query>{_CANARY}</user_query>",
                                    }
                                ]
                            },
                        }
                    ]
                ),
                "cursor.jsonl",
            )
        ]
    if "openclaw" in enabled:
        candidates["openclaw"] = [
            _snapshot(
                "openclaw",
                _jsonl(
                    [
                        {
                            "type": "session",
                            "id": "canary-openclaw",
                            "cwd": "/canary/project",
                        },
                        {
                            "type": "message",
                            "message": {"role": "user", "content": _CANARY},
                        },
                    ]
                ),
                "openclaw.jsonl",
            )
        ]
    failures = []
    for harness, snapshots in candidates.items():
        decoder = decoder_for(harness)
        for snapshot in snapshots:
            try:
                batch = decoder.decode(snapshot)
                texts = [
                    event.text for session in batch.sessions for event in session.events
                ]
                if batch.completeness != "complete" or _CANARY not in texts:
                    raise ValueError("canary did not survive decoding")
                for text in texts:
                    if text == _CANARY:
                        redacted, _counts = redactor.apply(text)
                        if _CANARY in redacted or "[REDACTED:" not in redacted:
                            raise ValueError("canary did not survive redaction")
            # A decoder is a plug-in boundary and may raise library-specific
            # exceptions. Only the synthetic source ID reaches the report.
            except Exception:  # noqa: BLE001
                failures.append(Diagnostic("DECODER_CANARY_FAILED", snapshot.source_id))
    return tuple(failures)


def reconcile_snapshot(
    snapshot: ExtractionSnapshot,
    inventory: OutputInventory,
    plan: PublicationPlan,
) -> ReconcileReport:
    effective_history_by_path = {
        entry.relative_path: entry.identity
        for entry in inventory.entries
        if entry.kind == "history" and entry.identity is not None
    }
    for removal in plan.removals:
        effective_history_by_path.pop(removal.relative_path, None)
    for planned in plan.writes:
        if planned.kind == "history" and planned.identity is not None:
            effective_history_by_path[planned.relative_path] = planned.identity
    effective_history = set(effective_history_by_path.values())
    diagnostics = []
    for session in snapshot.sessions:
        if session.identity not in effective_history:
            diagnostics.append(
                Diagnostic(
                    "ACCEPTED_SESSION_WITHOUT_OUTPUT",
                    session.source_ref.split("/", 1)[0],
                    session.session_id,
                )
            )
    marker_without_input = 0
    unknown_messages = 0
    for source_id, observation in snapshot.observations.items():
        if (
            observation.recognizable_user_markers
            and not observation.accepted_direct_user_events
        ):
            marker_without_input += 1
            diagnostics.append(
                Diagnostic(
                    "RECOGNIZED_MARKER_WITHOUT_INPUT",
                    source_id,
                    count=observation.recognizable_user_markers,
                )
            )
        count = sum(observation.unknown_record_counts.values())
        if count:
            unknown_messages += count
            diagnostics.append(
                Diagnostic("UNKNOWN_MESSAGE_FORMAT", source_id, count=count)
            )
    diagnostics.extend(
        diagnostic
        for diagnostic in snapshot.diagnostics
        if diagnostic.code in RECONCILIATION_CODES
    )
    # Remove exact duplicates so a decoder diagnostic and observation do not
    # make one failure appear as several failures.
    unique = {
        (item.code, item.source_id, item.session_id, item.count): item
        for item in diagnostics
    }
    result = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.code,
                item.source_id,
                item.session_id or "",
                item.count or 0,
            ),
        )
    )
    return ReconcileReport(
        ok=not result,
        checks={
            "accepted_sessions": len(snapshot.sessions),
            "missing_outputs": sum(
                item.code == "ACCEPTED_SESSION_WITHOUT_OUTPUT" for item in result
            ),
            "marker_without_input_sources": marker_without_input,
            "unknown_message_records": unknown_messages,
        },
        diagnostics=result,
    )


def write_failure_marker(path: Path, report: ReconcileReport) -> None:
    """Atomically write only status, identifiers, and counts."""
    data = {
        "schema_version": "agent-session-reconciliation/v1",
        "status": "failed",
        "checks": dict(report.checks),
        "diagnostics": [
            {
                "code": item.code,
                "source_id": item.source_id,
                "session_id": item.session_id,
                "count": item.count,
            }
            for item in report.diagnostics
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clear_failure_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
