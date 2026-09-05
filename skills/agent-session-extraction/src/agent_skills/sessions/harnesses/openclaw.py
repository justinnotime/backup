"""Configurable decoder for OpenClaw JSONL conversations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..model import (
    DecodeBatch,
    DecodedEvent,
    DecodedSession,
    Diagnostic,
    FormatObservations,
    RejectedSession,
    SourceSnapshot,
)
from .base import jsonl_lines


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"].strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _looks_cron(header: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    if options.get("is_cron_session") is True:
        return True
    for field in ("channel", "kind", "sessionKey", "key"):
        value = header.get(field)
        if isinstance(value, str) and "cron" in value.lower():
            return True
    return False


class OpenClawDecoder:
    harness = "openclaw"

    def capabilities(self) -> tuple[str, ...]:
        return (
            "jsonl",
            "cron-policy",
            "operational-notification-policy",
            "channel-forward-policy",
        )

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch:
        if snapshot.payload is None:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("OPENCLAW_PAYLOAD_UNAVAILABLE", snapshot.source_id),
                ),
            )
        options = snapshot.decoder_options
        minimum = options.get("minimum_user_events", 1)
        minimum_total = options.get("minimum_total_events", 1)
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < 1
            or not isinstance(minimum_total, int)
            or isinstance(minimum_total, bool)
            or minimum_total < 1
        ):
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("OPENCLAW_INVALID_OPTIONS", snapshot.source_id),
                ),
            )
        synthetic_prefixes = _string_tuple(
            options.get(
                "synthetic_prompt_prefixes", options.get("synthetic_prefixes", ())
            )
        )
        operational_prefixes = _string_tuple(
            options.get("operational_notification_prefixes", ())
        )
        channel_prefixes = _string_tuple(
            options.get("channel_forward_prefixes", ("System:",))
        ) or ("System:",)
        filter_ops = options.get("exclude_operational_notifications", False) is True
        retain_forwarded = options.get("retain_channel_forwarded", True) is True

        header: dict[str, Any] = {}
        raw_messages: list[dict[str, Any]] = []
        malformed = 0
        unknown: dict[str, int] = {}
        recognized: dict[str, int] = {}
        for line in jsonl_lines(snapshot.payload):
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
            record_type = record.get("type")
            if record_type == "session" and not header:
                header = record
                recognized["session"] = 1
            elif record_type == "message":
                raw_messages.append(record)
            else:
                kind = record_type if isinstance(record_type, str) else "unknown"
                unknown[kind] = unknown.get(kind, 0) + 1

        session_id_value = header.get("id") or options.get("session_id")
        session_id = (
            session_id_value.strip()
            if isinstance(session_id_value, str) and session_id_value.strip()
            else snapshot.path.stem
        )
        if options.get("exclude_cron_sessions", False) is True and _looks_cron(
            header, options
        ):
            return DecodeBatch(
                sessions=(),
                observations=FormatObservations(
                    recognized_record_counts=recognized,
                    unknown_record_counts=unknown,
                ),
                rejected_sessions=(RejectedSession(session_id, "CRON_SESSION"),),
                completeness="incomplete" if malformed else "complete",
                diagnostics=(
                    (
                        Diagnostic(
                            "OPENCLAW_MALFORMED_RECORD",
                            snapshot.source_id,
                            count=malformed,
                        ),
                    )
                    if malformed
                    else ()
                ),
            )

        events: list[DecodedEvent] = []
        user_markers = 0
        accepted_users = 0
        for record in raw_messages:
            message = record.get("message")
            if not isinstance(message, dict):
                malformed += 1
                continue
            role = message.get("role")
            if role in {"toolResult", "tool", "system"}:
                recognized[f"dropped:{role}"] = recognized.get(f"dropped:{role}", 0) + 1
                continue
            if role not in {"user", "assistant"}:
                kind = role if isinstance(role, str) else "unknown-role"
                unknown[kind] = unknown.get(kind, 0) + 1
                continue
            text = _text_content(message.get("content"))
            if not text:
                continue
            metadata: dict[str, Any] = {}
            if role == "user":
                user_markers += 1
                if any(text.startswith(prefix) for prefix in synthetic_prefixes):
                    continue
            if filter_ops and any(
                text.startswith(prefix) for prefix in operational_prefixes
            ):
                continue
            if role == "user":
                is_forwarded = any(
                    text.startswith(prefix) for prefix in channel_prefixes
                )
                if is_forwarded and not retain_forwarded:
                    continue
                if is_forwarded:
                    metadata["channel_forwarded"] = True
                accepted_users += 1
            timestamp = _timestamp(record.get("timestamp"))
            events.append(
                DecodedEvent(
                    source_sequence=len(events),
                    timestamp=timestamp,
                    timestamp_quality="exact" if timestamp is not None else "unknown",
                    role_hint="user-like" if role == "user" else "assistant",
                    text=text,
                    raw_kind=f"openclaw.message.{role}",
                    message_key=(
                        record.get("id") if isinstance(record.get("id"), str) else None
                    ),
                    metadata=metadata,
                )
            )
            recognized[f"message:{role}"] = recognized.get(f"message:{role}", 0) + 1

        direct_count = sum(event.role_hint == "user-like" for event in events)
        rejected: tuple[RejectedSession, ...] = ()
        sessions: tuple[DecodedSession, ...] = ()
        if direct_count >= minimum and len(events) >= minimum_total:
            metadata: dict[str, Any] = {}
            if options.get("include_channel_metadata") is True:
                channel = header.get("channel") or options.get("channel")
                if isinstance(channel, str) and channel:
                    metadata["channel"] = channel
            if options.get("include_session_metadata") is True:
                for field in _string_tuple(options.get("session_metadata_fields", ())):
                    value = header.get(field)
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        metadata[field] = value
            project_hint = options.get("project_hint")
            sessions = (
                DecodedSession(
                    session_id=session_id,
                    cwd=header.get("cwd")
                    if isinstance(header.get("cwd"), str)
                    else None,
                    project_hint=(
                        project_hint
                        if isinstance(project_hint, str) and project_hint
                        else None
                    ),
                    conversation_kind="main",
                    events=tuple(events),
                    metadata=metadata,
                ),
            )
        else:
            reason = (
                "NO_DIRECT_USER"
                if direct_count < minimum
                else "BELOW_MINIMUM_TOTAL_EVENTS"
            )
            rejected = (RejectedSession(session_id, reason),)

        diagnostics = ()
        completeness = "complete"
        if malformed:
            completeness = "incomplete"
            diagnostics = (
                Diagnostic(
                    "OPENCLAW_MALFORMED_RECORD", snapshot.source_id, count=malformed
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


DECODER = OpenClawDecoder()

__all__ = ["DECODER", "OpenClawDecoder"]
