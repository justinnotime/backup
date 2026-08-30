"""Decoder for Cursor agent transcript JSONL files."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone

from ..model import (
    DecodeBatch,
    DecodedEvent,
    DecodedSession,
    Diagnostic,
    FormatObservations,
    RejectedSession,
    SourceSnapshot,
)

_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_TIMESTAMP_RE = re.compile(
    r"<timestamp>\s*\w+,\s+(\w+)\s+(\d+),\s+(\d{4}),\s+"
    r"(\d+):(\d+)\s+(AM|PM)\s+\(UTC([+-]\d+)(?::(\d+))?\)\s*</timestamp>",
    re.IGNORECASE,
)
_ISO_TIMESTAMP_RE = re.compile(r"<timestamp>\s*([^<]+?)\s*</timestamp>", re.DOTALL)
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        1,
    )
}


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    return "\n".join(
        block["text"]
        for block in value
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ).strip()


def _timestamp(text: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(text)
    if match:
        month, day, year, hour, minute, ampm, offset_hour, offset_minute = (
            match.groups()
        )
        month_number = _MONTHS.get(month[:3].title())
        if month_number is None:
            return None
        parsed_hour = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
        sign = -1 if offset_hour.startswith("-") else 1
        offset = timedelta(
            hours=int(offset_hour), minutes=sign * int(offset_minute or 0)
        )
        try:
            return datetime(
                int(year),
                int(month_number),
                int(day),
                parsed_hour,
                int(minute),
                tzinfo=timezone(offset),
            ).astimezone(UTC)
        except ValueError:
            return None
    iso_match = _ISO_TIMESTAMP_RE.search(text)
    if not iso_match:
        return None
    try:
        return datetime.fromisoformat(iso_match.group(1).strip()).astimezone(UTC)
    except ValueError:
        return None


class CursorDecoder:
    harness = "cursor"

    def capabilities(self) -> tuple[str, ...]:
        return ("jsonl", "user-query-blocks", "approximate-assistant-time")

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch:
        if snapshot.payload is None:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("CURSOR_PAYLOAD_UNAVAILABLE", snapshot.source_id),
                ),
            )
        prefixes_value = snapshot.decoder_options.get(
            "synthetic_prompt_prefixes",
            snapshot.decoder_options.get("synthetic_prefixes", ()),
        )
        prefixes = tuple(item for item in prefixes_value if isinstance(item, str))
        minimum = snapshot.decoder_options.get("minimum_user_events", 1)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(Diagnostic("CURSOR_INVALID_OPTIONS", snapshot.source_id),),
            )

        events: list[DecodedEvent] = []
        recognized: dict[str, int] = {}
        unknown: dict[str, int] = {}
        malformed = 0
        user_markers = 0
        accepted_users = 0
        last_user_timestamp: datetime | None = None

        lines = snapshot.payload.splitlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            role = record.get("role")
            if role not in {"user", "assistant"}:
                kind = role if isinstance(role, str) else "non-message"
                recognized[f"ignored-role:{kind}"] = (
                    recognized.get(f"ignored-role:{kind}", 0) + 1
                )
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            raw = _text_content(content)
            if role == "user":
                match = _QUERY_RE.search(raw)
                if not match:
                    recognized["ignored-user-context"] = (
                        recognized.get("ignored-user-context", 0) + 1
                    )
                    continue
                user_markers += 1
                text = match.group(1).strip()
                if not text or any(text.startswith(prefix) for prefix in prefixes):
                    continue
                accepted_users += 1
                last_user_timestamp = _timestamp(raw)
                events.append(
                    DecodedEvent(
                        source_sequence=len(events),
                        timestamp=last_user_timestamp,
                        timestamp_quality=(
                            "exact" if last_user_timestamp is not None else "unknown"
                        ),
                        role_hint="user-like",
                        text=text,
                        raw_kind="cursor.user_query",
                    )
                )
                recognized["user_query"] = recognized.get("user_query", 0) + 1
            elif raw:
                events.append(
                    DecodedEvent(
                        source_sequence=len(events),
                        timestamp=last_user_timestamp,
                        timestamp_quality=(
                            "approximate"
                            if last_user_timestamp is not None
                            else "unknown"
                        ),
                        role_hint="assistant",
                        text=raw,
                        raw_kind="cursor.assistant_text",
                    )
                )
                recognized["assistant_text"] = recognized.get("assistant_text", 0) + 1

        session_id_value = snapshot.decoder_options.get("session_id")
        session_id = (
            session_id_value.strip()
            if isinstance(session_id_value, str) and session_id_value.strip()
            else snapshot.path.stem
        )
        direct_count = sum(event.role_hint == "user-like" for event in events)
        rejected = ()
        sessions: tuple[DecodedSession, ...] = ()
        if direct_count >= minimum:
            project_hint = snapshot.decoder_options.get("project_hint")
            sessions = (
                DecodedSession(
                    session_id=session_id,
                    cwd=None,
                    project_hint=(
                        project_hint
                        if isinstance(project_hint, str) and project_hint
                        else None
                    ),
                    conversation_kind="main",
                    events=tuple(events),
                ),
            )
        else:
            rejected = (RejectedSession(session_id, "NO_DIRECT_USER"),)

        diagnostics = ()
        completeness = "complete"
        if malformed:
            completeness = "incomplete"
            diagnostics = (
                Diagnostic(
                    "CURSOR_MALFORMED_RECORD", snapshot.source_id, count=malformed
                ),
            )
        return DecodeBatch(
            sessions=sessions,
            observations=FormatObservations(
                recognized_record_counts=recognized,
                unknown_record_counts=unknown,
                recognizable_user_markers=user_markers,
                accepted_direct_user_events=accepted_users,
            ),
            rejected_sessions=rejected,
            completeness=completeness,
            diagnostics=diagnostics,
        )


DECODER = CursorDecoder()

__all__ = ["DECODER", "CursorDecoder"]
