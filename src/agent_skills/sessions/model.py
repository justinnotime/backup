"""Versioned values shared by decoders, policies, and publishers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

MANIFEST_SCHEMA_VERSION = "agent-session-extraction-manifest/v1"
NORMALIZED_SCHEMA_VERSION = "agent-session/v1"
RUN_REPORT_SCHEMA_VERSION = "agent-session-run-report/v1"
SUPPORTED_HARNESSES = frozenset(
    {"claude-code", "codex", "opencode", "dsh", "cursor", "openclaw"}
)

Harness = Literal["claude-code", "codex", "opencode", "dsh", "cursor", "openclaw"]
Role = Literal["user", "assistant", "peer-agent"]
RoleHint = Literal["user-like", "assistant"]
TimestampQuality = Literal["exact", "approximate", "unknown"]


def utc_timestamp(value: datetime | None) -> datetime | None:
    """Return an aware UTC timestamp and reject naive values."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    timestamp: datetime | None
    timestamp_quality: TimestampQuality
    role: Role
    text: str
    raw_kind: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_timestamp(self.timestamp))
        if self.sequence < 0:
            raise ValueError("event sequence must be non-negative")
        if self.role not in {"user", "assistant", "peer-agent"}:
            raise ValueError(f"invalid event role: {self.role}")
        if self.timestamp_quality not in {"exact", "approximate", "unknown"}:
            raise ValueError(f"invalid timestamp quality: {self.timestamp_quality}")
        if not self.text.strip():
            raise ValueError("event text must not be empty")


@dataclass(frozen=True, slots=True)
class Session:
    schema_version: str
    harness: Harness
    session_id: str
    source_ref: str
    node_label: str
    cwd: str | None
    project: str
    started_at: datetime | None
    ended_at: datetime | None
    events: tuple[Event, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != NORMALIZED_SCHEMA_VERSION:
            raise ValueError(f"unsupported session schema: {self.schema_version}")
        if self.harness not in SUPPORTED_HARNESSES:
            raise ValueError(f"unsupported harness: {self.harness}")
        for name, value in (
            ("session_id", self.session_id),
            ("source_ref", self.source_ref),
            ("node_label", self.node_label),
            ("project", self.project),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if Path(self.source_ref).is_absolute():
            raise ValueError("source_ref must not be an absolute path")
        object.__setattr__(self, "started_at", utc_timestamp(self.started_at))
        object.__setattr__(self, "ended_at", utc_timestamp(self.ended_at))

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.harness, self.node_label, self.session_id)


@dataclass(frozen=True, slots=True)
class DecodedEvent:
    source_sequence: int
    timestamp: datetime | None
    timestamp_quality: TimestampQuality
    role_hint: RoleHint
    text: str
    raw_kind: str
    message_key: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_timestamp(self.timestamp))


@dataclass(frozen=True, slots=True)
class DecodedSession:
    session_id: str
    cwd: str | None
    project_hint: str | None
    conversation_kind: Literal["main", "conversational-subagent"]
    events: tuple[DecodedEvent, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FormatObservations:
    recognized_record_counts: Mapping[str, int] = field(default_factory=dict)
    unknown_record_counts: Mapping[str, int] = field(default_factory=dict)
    recognizable_user_markers: int = 0
    accepted_direct_user_events: int = 0


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    source_id: str
    session_id: str | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True)
class RejectedSession:
    session_id: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class DecodeBatch:
    sessions: tuple[DecodedSession, ...]
    observations: FormatObservations = FormatObservations()
    rejected_sessions: tuple[RejectedSession, ...] = ()
    completeness: Literal["complete", "incomplete", "invalid"] = "complete"
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """A stable candidate presented to a decoder.

    ``path`` is private process state. Diagnostics and reports identify the
    candidate through ``source_ref`` only.
    """

    source_id: str
    harness: Harness
    node_label: str
    source_ref: str
    path: Path
    payload: bytes | None
    decoder_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    source_id: str
    node_label: str
    status: Literal["success", "unreadable", "invalid", "skipped"]
    candidate_count: int
    session_count: int
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionSnapshot:
    sessions: tuple[Session, ...]
    source_outcomes: tuple[SourceOutcome, ...]
    observations: Mapping[str, FormatObservations]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedFile:
    relative_path: str
    content: bytes
    identity: tuple[str, str, str] | None
    kind: Literal["history", "prompt", "index", "marker"]


@dataclass(frozen=True, slots=True)
class CleanupAction:
    relative_path: str
    identity: tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    writes: tuple[PlannedFile, ...]
    removals: tuple[CleanupAction, ...]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    ok: bool
    checks: Mapping[str, int]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class RunReport:
    schema_version: str
    status: Literal["ok", "failed"]
    dry_run: bool
    source_count: int
    session_count: int
    write_count: int
    removal_count: int
    diagnostic_codes: tuple[str, ...]
