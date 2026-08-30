"""Pure decoder for Claude Code JSONL session snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
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

_DEFAULT_SYNTHETIC_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<ide_opened_file>",
    "<local-command-stdout>",
    "<command-",
    "This session is being continued",
    "Please provide your complete findings",
    "Your task is to create a detailed summary",
)
_COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
_COMMAND_ARGS_RE = re.compile(r"<command-args>([^<]*)</command-args>")


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _jsonl(
    payload: bytes, grandfathered_malformed_sha256: frozenset[str]
) -> tuple[list[tuple[int, Mapping[str, Any]]], int, int]:
    records: list[tuple[int, Mapping[str, Any]]] = []
    malformed = 0
    grandfathered = 0
    for sequence, raw_line in enumerate(payload.splitlines()):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            digest = hashlib.sha256(raw_line).hexdigest()
            if digest in grandfathered_malformed_sha256:
                grandfathered += 1
            else:
                malformed += 1
            continue
        if isinstance(value, dict):
            records.append((sequence, value))
        else:
            malformed += 1
    return records, malformed, grandfathered


def _grandfathered_malformed_hashes(
    options: Mapping[str, Any],
) -> frozenset[str] | None:
    value = options.get("grandfathered_malformed_line_sha256", ())
    if not isinstance(value, (list, tuple)):
        return None
    if any(
        not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
        for item in value
    ):
        return None
    result = frozenset(value)
    return result if len(result) == len(value) else None


def _slash_command(text: str) -> str | None:
    stripped = text.lstrip()
    if not stripped.startswith(("<command-name>", "<command-message>")):
        return None
    name_match = _COMMAND_NAME_RE.search(stripped)
    if not name_match:
        return None
    name = name_match.group(1).strip()
    if not name.startswith("/"):
        name = f"/{name}"
    args_match = _COMMAND_ARGS_RE.search(stripped)
    args = args_match.group(1).strip() if args_match else ""
    return f"[slash] {name}" + (f" {args}" if args else "")


def _assistant_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    return "\n".join(parts).strip()


def _message_content(record: Mapping[str, Any]) -> Any:
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _user_text_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ).strip()


def _has_unknown_direct_text_shape(content: Any) -> bool:
    if isinstance(content, dict):
        return isinstance(content.get("text"), str) and bool(content["text"].strip())
    return isinstance(content, list) and any(
        isinstance(block, dict)
        and block.get("type") == "input_text"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in content
    )


def _message_key(record: Mapping[str, Any]) -> str | None:
    for key in ("uuid", "id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    message = record.get("message")
    if isinstance(message, dict):
        value = message.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _option_prefixes(options: Mapping[str, Any]) -> tuple[str, ...]:
    configured = options.get("synthetic_prompt_prefixes", ())
    if not isinstance(configured, (list, tuple)):
        return _DEFAULT_SYNTHETIC_PREFIXES
    return _DEFAULT_SYNTHETIC_PREFIXES + tuple(
        value for value in configured if isinstance(value, str) and value
    )


def _is_real_user_text(text: Any, prefixes: tuple[str, ...]) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return bool(stripped) and stripped != "Warmup" and not stripped.startswith(prefixes)


def _fallback_session_id(snapshot: SourceSnapshot) -> str:
    configured = snapshot.decoder_options.get("session_id")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    name = PurePosixPath(snapshot.source_ref).name
    name = name.removesuffix(".jsonl")
    return name or snapshot.source_ref


class ClaudeDecoder:
    """Decode one stable Claude Code JSONL candidate without filesystem I/O."""

    harness = "claude-code"

    def capabilities(self) -> tuple[str, ...]:
        return (
            "jsonl",
            "queued-prompts",
            "slash-command-normalization",
            "conversational-subagents",
        )

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch:
        if snapshot.harness != self.harness or snapshot.payload is None:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic("CLAUDE_INVALID_SNAPSHOT", snapshot.source_id),
                ),
            )

        options = snapshot.decoder_options
        grandfathered_hashes = _grandfathered_malformed_hashes(options)
        if grandfathered_hashes is None:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic(
                        "CLAUDE_INVALID_GRANDFATHERED_MALFORMED_HASHES",
                        snapshot.source_id,
                    ),
                ),
            )
        records, malformed, grandfathered = _jsonl(
            snapshot.payload, grandfathered_hashes
        )
        prefixes = _option_prefixes(options)
        recognized: Counter[str] = Counter()
        if grandfathered:
            recognized["ignored.grandfathered-malformed-line"] = grandfathered
        unknown: Counter[str] = Counter()
        session_id = next(
            (
                value
                for _, record in records
                for value in (record.get("sessionId"), record.get("session_id"))
                if isinstance(value, str) and value
            ),
            _fallback_session_id(snapshot),
        )
        cwd = next(
            (
                record.get("cwd")
                for _, record in records
                if isinstance(record.get("cwd"), str) and record.get("cwd")
            ),
            None,
        )
        project_hint = options.get("project_hint")
        if not isinstance(project_hint, str) or not project_hint:
            project_hint = None

        conversation_kind = options.get("conversation_kind")
        if conversation_kind is None:
            normalized_ref = f"/{snapshot.source_ref.strip('/')}"
            conversation_kind = (
                "conversational-subagent" if "/subagents/" in normalized_ref else "main"
            )
        if conversation_kind not in {"main", "conversational-subagent"}:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(
                    Diagnostic(
                        "CLAUDE_INVALID_CONVERSATION_KIND",
                        snapshot.source_id,
                        session_id,
                    ),
                ),
            )
        if conversation_kind == "conversational-subagent":
            # Claude records the parent session ID inside subagent rows. The
            # complete child identity is the stable transcript filename.
            session_id = PurePosixPath(snapshot.source_ref).name.removesuffix(".jsonl")
        main_session = conversation_kind == "main"

        # Claude may record one queued command both as queue-operation and as a
        # user-shaped echo. Every user string participates in this duplicate
        # check, including sidechain and metadata echoes; those echoes are not
        # retained, and their queued copy must not leak back into the transcript.
        direct_user_texts = {
            text
            for _, record in records
            if record.get("type") == "user"
            for content in (_message_content(record),)
            for text in (
                content.strip()
                if isinstance(content, str)
                else _user_text_blocks(content),
            )
            if text
        }
        events: list[DecodedEvent] = []
        queues: list[tuple[int, Mapping[str, Any], str]] = []
        user_markers = 0
        accepted_users = 0

        for sequence, record in records:
            record_type = record.get("type")
            timestamp = _parse_timestamp(record.get("timestamp"))
            quality = "exact" if timestamp is not None else "unknown"
            if record_type == "user":
                recognized["user"] += 1
                content = _message_content(record)
                raw_kind = "claude.user"
                if main_session and record.get("isSidechain"):
                    continue
                slash = _slash_command(content) if isinstance(content, str) else None
                if slash is not None:
                    user_markers += 1
                    text = slash
                elif main_session and record.get("isMeta"):
                    continue
                elif isinstance(content, str) and content.strip():
                    if _is_real_user_text(content, prefixes):
                        user_markers += 1
                        text = content.strip()
                    else:
                        recognized["user.synthetic"] += 1
                        continue
                elif block_text := _user_text_blocks(content):
                    if _is_real_user_text(block_text, prefixes):
                        user_markers += 1
                        text = block_text
                        raw_kind = "claude.user.text-blocks"
                    else:
                        recognized["user.synthetic"] += 1
                        continue
                elif _has_unknown_direct_text_shape(content):
                    user_markers += 1
                    unknown["user.unknown-text-content"] += 1
                    continue
                else:
                    continue
                events.append(
                    DecodedEvent(
                        source_sequence=sequence,
                        timestamp=timestamp,
                        timestamp_quality=quality,
                        role_hint="user-like",
                        text=text,
                        raw_kind=raw_kind,
                        message_key=_message_key(record),
                    )
                )
                accepted_users += 1
            elif record_type == "assistant":
                recognized["assistant"] += 1
                if main_session and (
                    record.get("isSidechain") or record.get("isMeta")
                ):
                    continue
                text = _assistant_text(_message_content(record))
                if text:
                    events.append(
                        DecodedEvent(
                            source_sequence=sequence,
                            timestamp=timestamp,
                            timestamp_quality=quality,
                            role_hint="assistant",
                            text=text,
                            raw_kind="claude.assistant.text",
                            message_key=_message_key(record),
                        )
                    )
            elif record_type == "queue-operation":
                if record.get("operation") != "enqueue":
                    recognized["queue-operation.ignored"] += 1
                    continue
                recognized["queue-operation.enqueue"] += 1
                content = record.get("content")
                if isinstance(content, str) and content.strip():
                    if _is_real_user_text(content, prefixes):
                        queues.append((sequence, record, content.strip()))
                    else:
                        recognized["queue-operation.synthetic"] += 1
            elif record_type in {
                "summary",
                "system",
                "progress",
                "attachment",
                "file-history-snapshot",
                "ai-title",
                "agent-setting",
                "atis-latch",
                "cost-state",
                "file-history-delta",
                "last-prompt",
                "mode",
                "permission-mode",
                "pr-link",
                "relocated",
                "worktree-state",
            }:
                recognized[f"ignored.{record_type}"] += 1
            else:
                unknown[str(record_type or "missing-type")] += 1

        for sequence, record, text in queues:
            if text in direct_user_texts:
                continue
            user_markers += 1
            timestamp = _parse_timestamp(record.get("timestamp"))
            events.append(
                DecodedEvent(
                    source_sequence=sequence,
                    timestamp=timestamp,
                    timestamp_quality="exact" if timestamp is not None else "unknown",
                    role_hint="user-like",
                    text=text,
                    raw_kind="claude.queue-operation.enqueue",
                    message_key=_message_key(record),
                )
            )
            accepted_users += 1
            direct_user_texts.add(text)

        events.sort(
            key=lambda event: (
                event.timestamp is None,
                event.timestamp or datetime.min.replace(tzinfo=UTC),
                event.source_sequence,
            )
        )
        observations = FormatObservations(
            recognized_record_counts=dict(sorted(recognized.items())),
            unknown_record_counts={
                **({"malformed-jsonl": malformed} if malformed else {}),
                **dict(sorted(unknown.items())),
            },
            recognizable_user_markers=user_markers,
            accepted_direct_user_events=accepted_users,
        )
        diagnostics: list[Diagnostic] = []
        if malformed:
            diagnostics.append(
                Diagnostic(
                    "CLAUDE_MALFORMED_RECORD", snapshot.source_id, session_id, malformed
                )
            )
        if unknown:
            diagnostics.append(
                Diagnostic(
                    "CLAUDE_UNKNOWN_RECORD",
                    snapshot.source_id,
                    session_id,
                    sum(unknown.values()),
                )
            )

        if conversation_kind == "conversational-subagent":
            retain = options.get(
                "retain_conversational_subagents",
                options.get("include_conversational_subagents", True),
            )
            minimum = options.get("conversational_subagent_min_user_events", 1)
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
                return DecodeBatch(
                    sessions=(),
                    observations=observations,
                    completeness="invalid",
                    diagnostics=tuple(diagnostics)
                    + (
                        Diagnostic(
                            "CLAUDE_INVALID_SUBAGENT_MINIMUM",
                            snapshot.source_id,
                            session_id,
                        ),
                    ),
                )
            if retain is not True or accepted_users < minimum:
                reason = (
                    "CONVERSATIONAL_SUBAGENT_DISABLED"
                    if retain is not True
                    else "CONVERSATIONAL_SUBAGENT_BELOW_MINIMUM"
                )
                return DecodeBatch(
                    sessions=(),
                    observations=observations,
                    rejected_sessions=(RejectedSession(session_id, reason),),
                    completeness="incomplete" if malformed or unknown else "complete",
                    diagnostics=tuple(diagnostics),
                )

        if accepted_users == 0:
            return DecodeBatch(
                sessions=(),
                observations=observations,
                rejected_sessions=(
                    RejectedSession(session_id, "NO_DIRECT_USER_EVENT"),
                ),
                completeness="incomplete" if malformed or unknown else "complete",
                diagnostics=tuple(diagnostics),
            )

        session = DecodedSession(
            session_id=session_id,
            cwd=cwd,
            project_hint=project_hint,
            conversation_kind=conversation_kind,
            events=tuple(events),
            metadata={"decoder": "claude-code/v1"},
        )
        return DecodeBatch(
            sessions=(session,),
            observations=observations,
            completeness="incomplete" if malformed or unknown else "complete",
            diagnostics=tuple(diagnostics),
        )


DECODER = ClaudeDecoder()

__all__ = ["DECODER", "ClaudeDecoder"]
