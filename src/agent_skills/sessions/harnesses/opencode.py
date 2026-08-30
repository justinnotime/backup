"""Deterministic decoder for OpenCode SQLite session snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..model import (
    DecodeBatch,
    DecodedEvent,
    DecodedSession,
    Diagnostic,
    FormatObservations,
    RejectedSession,
    SourceSnapshot,
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _project_hint(directory: object) -> str | None:
    if not isinstance(directory, str) or not directory.strip():
        return None
    cleaned = directory.rstrip("/\\").replace("\\", "/")
    return cleaned.rsplit("/", 1)[-1] or None


def _is_synthetic(text: str, prefixes: tuple[str, ...]) -> bool:
    stripped = text.strip()
    return not stripped or any(stripped.startswith(prefix) for prefix in prefixes)


class OpenCodeDecoder:
    """Read only top-level OpenCode conversations from a SQLite snapshot."""

    harness = "opencode"

    def capabilities(self) -> tuple[str, ...]:
        return ("sqlite-read-only", "top-level-sessions", "text-parts")

    def _connect(self, snapshot: SourceSnapshot) -> sqlite3.Connection:
        if snapshot.payload is None:
            raise sqlite3.NotSupportedError("stable sqlite snapshot is required")
        connection = sqlite3.connect(":memory:")
        try:
            if not hasattr(connection, "deserialize"):
                raise sqlite3.NotSupportedError("sqlite deserialize unavailable")
            connection.deserialize(snapshot.payload)
        except Exception:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch:
        prefixes_value = snapshot.decoder_options.get(
            "synthetic_prompt_prefixes",
            snapshot.decoder_options.get("synthetic_prefixes", ()),
        )
        prefixes = tuple(item for item in prefixes_value if isinstance(item, str))
        minimum = snapshot.decoder_options.get("minimum_user_events", 1)
        excluded_value = snapshot.decoder_options.get("excluded_cwd_prefixes", ())
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("OPENCODE_INVALID_OPTIONS", snapshot.source_id),
                ),
            )
        if not isinstance(excluded_value, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in excluded_value
        ):
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("OPENCODE_INVALID_OPTIONS", snapshot.source_id),
                ),
            )
        excluded_cwd_prefixes = tuple(excluded_value)

        recognized: dict[str, int] = {}
        unknown: dict[str, int] = {}
        rejected: list[RejectedSession] = []
        sessions: list[DecodedSession] = []
        user_markers = 0
        accepted_users = 0
        malformed = 0

        try:
            connection = self._connect(snapshot)
            try:
                rows = connection.execute(
                    """
                    SELECT id, title, directory, time_created, time_updated
                    FROM session
                    WHERE parent_id IS NULL
                    ORDER BY time_created, id
                    """
                ).fetchall()
                recognized["top-level-session"] = len(rows)
                for row in rows:
                    session_id = row["id"]
                    if not isinstance(session_id, str) or not session_id:
                        malformed += 1
                        continue
                    directory = row["directory"]
                    if isinstance(directory, str) and directory.startswith(
                        excluded_cwd_prefixes
                    ):
                        recognized["excluded-cwd"] = (
                            recognized.get("excluded-cwd", 0) + 1
                        )
                        rejected.append(RejectedSession(session_id, "EXCLUDED_CWD"))
                        continue
                    parts = connection.execute(
                        """
                        SELECT m.id AS message_id, m.time_created AS message_time,
                               m.data AS message_data, p.id AS part_id,
                               p.time_created AS part_time, p.data AS part_data
                        FROM message AS m
                        JOIN part AS p ON p.message_id = m.id
                        WHERE m.session_id = ?
                        ORDER BY m.time_created, m.id, p.time_created, p.id
                        """,
                        (session_id,),
                    ).fetchall()
                    events: list[DecodedEvent] = []
                    for part_row in parts:
                        try:
                            message = json.loads(part_row["message_data"])
                            part = json.loads(part_row["part_data"])
                        except (json.JSONDecodeError, TypeError):
                            malformed += 1
                            continue
                        if not isinstance(message, dict) or not isinstance(part, dict):
                            malformed += 1
                            continue
                        role = message.get("role")
                        part_type = part.get("type")
                        if part_type != "text":
                            if isinstance(part_type, str):
                                recognized[f"ignored-part:{part_type}"] = (
                                    recognized.get(f"ignored-part:{part_type}", 0) + 1
                                )
                            else:
                                malformed += 1
                            continue
                        text = part.get("text")
                        if role not in {"user", "assistant"} or not isinstance(
                            text, str
                        ):
                            malformed += 1
                            continue
                        text = text.strip()
                        if not text:
                            continue
                        recognized[f"text:{role}"] = (
                            recognized.get(f"text:{role}", 0) + 1
                        )
                        if role == "user":
                            user_markers += 1
                            if _is_synthetic(text, prefixes):
                                continue
                            accepted_users += 1
                        raw_time = part_row["part_time"]
                        if raw_time is None:
                            raw_time = part_row["message_time"]
                        parsed_time = _timestamp(raw_time)
                        events.append(
                            DecodedEvent(
                                source_sequence=len(events),
                                timestamp=parsed_time,
                                timestamp_quality=(
                                    "exact" if parsed_time is not None else "unknown"
                                ),
                                role_hint="user-like"
                                if role == "user"
                                else "assistant",
                                text=text,
                                raw_kind="sqlite.text-part",
                                message_key=f"{part_row['message_id']}:{part_row['part_id']}",
                            )
                        )
                    direct_count = sum(
                        event.role_hint == "user-like" for event in events
                    )
                    if direct_count < minimum:
                        rejected.append(RejectedSession(session_id, "NO_DIRECT_USER"))
                        continue
                    metadata = {}
                    if isinstance(row["title"], str) and row["title"].strip():
                        metadata["title"] = row["title"].strip()
                    sessions.append(
                        DecodedSession(
                            session_id=session_id,
                            cwd=directory if isinstance(directory, str) else None,
                            project_hint=_project_hint(directory),
                            conversation_kind="main",
                            events=tuple(events),
                            metadata=metadata,
                        )
                    )
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as error:
            del error
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("OPENCODE_DATABASE_UNREADABLE", snapshot.source_id),
                ),
            )

        diagnostics = ()
        completeness = "complete"
        if malformed:
            completeness = "incomplete"
            diagnostics = (
                Diagnostic(
                    "OPENCODE_MALFORMED_RECORD", snapshot.source_id, count=malformed
                ),
            )
        return DecodeBatch(
            sessions=tuple(sessions),
            observations=FormatObservations(
                recognized_record_counts=recognized,
                unknown_record_counts=unknown,
                recognizable_user_markers=user_markers,
                accepted_direct_user_events=accepted_users,
            ),
            rejected_sessions=tuple(rejected),
            completeness=completeness,
            diagnostics=diagnostics,
        )


DECODER = OpenCodeDecoder()

__all__ = ["DECODER", "OpenCodeDecoder"]
