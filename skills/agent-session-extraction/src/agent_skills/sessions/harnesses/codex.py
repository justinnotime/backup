"""Pure decoder for Codex rollout JSONL snapshots."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
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
from .base import jsonl_lines

_DEFAULT_SYNTHETIC_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<hook_prompt",
    "<subagent_notification",
    "<turn_aborted",
    "# AGENTS.md instructions",
)
_TITLE_TRAILER_RE = re.compile(
    r"\n+Based on this message, call functions\.happy__change_title.*$", re.DOTALL
)
_IMAGE_PREFIX_RE = re.compile(
    r'\A(?:<image name=\[Image #[0-9]+\] path="[^"\r\n]+">\r?\n'
    r'(?:</image>\r?\n)?)+'
)
_KNOWN_IGNORED_EVENT_MESSAGES = {
    "task_started",
    "task_complete",
    "turn_aborted",
    "exec_command_end",
    "patch_apply_end",
    "token_count",
    "agent_reasoning",
    "reasoning",
    "error",
}
_STREAM_PRIORITY = {"item_completed": 0, "legacy": 1, "response_item": 2}


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


def _jsonl(payload: bytes) -> tuple[list[tuple[int, Mapping[str, Any]]], int]:
    records: list[tuple[int, Mapping[str, Any]]] = []
    malformed = 0
    for sequence, raw_line in enumerate(jsonl_lines(payload)):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append((sequence, value))
        else:
            malformed += 1
    return records, malformed


def _is_subagent_meta(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return source == "subagent" or (isinstance(source, dict) and "subagent" in source)


def _content_text(content: Any, allowed_types: set[str] | None = None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        block_type = block.get("type")
        if allowed_types is not None and block_type not in allowed_types:
            continue
        if block["text"].strip():
            parts.append(block["text"])
    return "\n".join(parts)


def _clean_user_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _TITLE_TRAILER_RE.sub("", _IMAGE_PREFIX_RE.sub("", value)).strip()


def _option_prefixes(options: Mapping[str, Any]) -> tuple[str, ...]:
    configured = options.get("synthetic_prompt_prefixes", ())
    if not isinstance(configured, (list, tuple)):
        return _DEFAULT_SYNTHETIC_PREFIXES
    return _DEFAULT_SYNTHETIC_PREFIXES + tuple(
        value for value in configured if isinstance(value, str) and value
    )


def _is_real_user_text(text: str, prefixes: tuple[str, ...]) -> bool:
    return bool(text) and not text.startswith(prefixes)


_CONVERSATION_NAME_TOKENS = ("user", "message")


def _may_carry_conversation(record_type: Any, record: Mapping[str, Any]) -> bool:
    """True when an unhandled top-level record could hold conversation text.

    A missing or non-string type, a non-empty string payload, a payload with a
    message/item/content key, or a type name that says user/message keeps the
    fail-closed behaviour: the record is reported as unknown and the source
    stays incomplete.
    """
    if not isinstance(record_type, str) or not record_type:
        return True
    payload = record.get("payload")
    if isinstance(payload, str):
        return bool(payload.strip())
    if isinstance(payload, dict) and any(
        key in payload for key in ("message", "item", "content")
    ):
        return True
    name = record_type.lower()
    return any(token in name for token in _CONVERSATION_NAME_TOKENS)


def _message_key(value: Mapping[str, Any]) -> str | None:
    for key in ("id", "item_id", "message_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _session_id(
    snapshot: SourceSnapshot, records: Sequence[tuple[int, Mapping[str, Any]]]
) -> str:
    for _, record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("id"), str)
            and payload["id"]
        ):
            return payload["id"]
    configured = snapshot.decoder_options.get("session_id")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    stem = PurePosixPath(snapshot.source_ref).name.removesuffix(".jsonl")
    return stem or snapshot.source_ref


def _fingerprint(event: DecodedEvent) -> tuple[str, str]:
    return (event.role_hint, event.text)


def _lcs_pairs(
    left: Sequence[DecodedEvent], right: Sequence[DecodedEvent]
) -> list[tuple[int, int]]:
    """Return deterministic index pairs for one longest common subsequence."""

    rows, columns = len(left), len(right)
    lengths = [[0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(columns - 1, -1, -1):
            if _fingerprint(left[i]) == _fingerprint(right[j]):
                lengths[i][j] = 1 + lengths[i + 1][j + 1]
            else:
                lengths[i][j] = max(lengths[i + 1][j], lengths[i][j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < rows and j < columns:
        if _fingerprint(left[i]) == _fingerprint(right[j]):
            pairs.append((i, j))
            i += 1
            j += 1
        elif lengths[i + 1][j] >= lengths[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _merge_stream(
    base: list[DecodedEvent], supplement: Sequence[DecodedEvent]
) -> tuple[int, int]:
    """Merge a provably compatible partial stream.

    Exact role/text matches are anchors.  If both streams contain unmatched
    events between the same pair of anchors, those events are competing
    versions at one logical position rather than a one-sided omission.  Keep
    the canonical stream unchanged and make that ambiguity visible instead of
    concatenating both versions into the transcript.
    """

    keyed: list[dict[str, tuple[str, str]]] = []
    duplicate_keys = 0
    for stream in (base, supplement):
        current: dict[str, tuple[str, str]] = {}
        for event in stream:
            if event.message_key is None:
                continue
            if event.message_key in current:
                duplicate_keys += 1
                continue
            current[event.message_key] = _fingerprint(event)
        keyed.append(current)
    shared_keys = set(keyed[0]).intersection(keyed[1])
    keyed_conflicts = sum(keyed[0][key] != keyed[1][key] for key in shared_keys)
    if duplicate_keys or keyed_conflicts:
        return 0, duplicate_keys + keyed_conflicts

    pairs = _lcs_pairs(base, supplement)
    boundaries = [(-1, -1), *pairs, (len(base), len(supplement))]
    conflicts = 0
    for index in range(len(boundaries) - 1):
        left, right = boundaries[index]
        next_left, next_right = boundaries[index + 1]
        left_gap = next_left - left - 1
        right_gap = next_right - right - 1
        if left_gap and right_gap:
            conflicts += max(left_gap, right_gap)
    if conflicts:
        return 0, conflicts

    additions = 0
    previous_right = 0
    offset = 0
    last_left = -1
    for left_index, right_index in pairs:
        missing = list(supplement[previous_right:right_index])
        if missing:
            insertion = left_index + offset
            base[insertion:insertion] = missing
            offset += len(missing)
            additions += len(missing)
        previous_right = right_index + 1
        last_left = left_index
    tail = list(supplement[previous_right:])
    if tail:
        insertion = last_left + 1 + offset if pairs else len(base)
        base[insertion:insertion] = tail
        additions += len(tail)
    return additions, 0


class CodexDecoder:
    """Decode all known Codex message generations and merge them once."""

    harness = "codex"

    def capabilities(self) -> tuple[str, ...]:
        return (
            "event-msg-legacy",
            "event-msg-item-completed",
            "response-item-message",
            "cross-stream-deduplication",
            "explicit-subagent-detection",
        )

    def decode(self, snapshot: SourceSnapshot, *, observer=None) -> DecodeBatch:
        if snapshot.harness != self.harness or snapshot.payload is None:
            return DecodeBatch(
                sessions=(),
                completeness="invalid",
                diagnostics=(Diagnostic("CODEX_INVALID_SNAPSHOT", snapshot.source_id),),
            )

        records, malformed = _jsonl(snapshot.payload)
        if observer is not None:
            observer.jsonl(records, malformed=malformed)
            return DecodeBatch(sessions=(), completeness="incomplete" if malformed else "complete")
        session_id = _session_id(snapshot, records)
        first_meta = next(
            (
                record.get("payload")
                for _, record in records
                if record.get("type") == "session_meta"
            ),
            None,
        )
        if _is_subagent_meta(first_meta):
            return DecodeBatch(
                sessions=(),
                rejected_sessions=(RejectedSession(session_id, "EXPLICIT_SUBAGENT"),),
            )

        cwd = first_meta.get("cwd") if isinstance(first_meta, dict) else None
        if not isinstance(cwd, str) or not cwd:
            cwd = None
        project_hint = snapshot.decoder_options.get("project_hint")
        if not isinstance(project_hint, str) or not project_hint:
            project_hint = None
        prefixes = _option_prefixes(snapshot.decoder_options)
        streams: dict[str, list[DecodedEvent]] = {
            "legacy": [],
            "item_completed": [],
            "response_item": [],
        }
        recognized: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        user_markers = 0

        def append_event(
            stream: str,
            sequence: int,
            record: Mapping[str, Any],
            role: str,
            text: str,
            raw_kind: str,
            key_source: Mapping[str, Any],
        ) -> None:
            nonlocal user_markers
            if role == "user-like":
                user_markers += 1
                text = _clean_user_text(text)
                if not _is_real_user_text(text, prefixes):
                    return
            else:
                if not isinstance(text, str):
                    return
                text = text.strip()
                if not text:
                    return
            timestamp = _parse_timestamp(record.get("timestamp"))
            streams[stream].append(
                DecodedEvent(
                    source_sequence=sequence,
                    timestamp=timestamp,
                    timestamp_quality="exact" if timestamp is not None else "unknown",
                    role_hint=role,  # type: ignore[arg-type]
                    text=text,
                    raw_kind=raw_kind,
                    message_key=_message_key(key_source),
                )
            )

        for sequence, record in records:
            record_type = record.get("type")
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if record_type == "session_meta":
                recognized["session_meta"] += 1
                continue
            if record_type == "event_msg":
                message_type = payload.get("type")
                if message_type == "user_message":
                    recognized["event_msg.user_message"] += 1
                    append_event(
                        "legacy",
                        sequence,
                        record,
                        "user-like",
                        payload.get("message", ""),
                        "codex.event_msg.user_message",
                        payload,
                    )
                elif message_type == "agent_message":
                    recognized["event_msg.agent_message"] += 1
                    append_event(
                        "legacy",
                        sequence,
                        record,
                        "assistant",
                        payload.get("message", ""),
                        "codex.event_msg.agent_message",
                        payload,
                    )
                elif message_type == "item_completed":
                    item = payload.get("item")
                    item = item if isinstance(item, dict) else {}
                    item_type = item.get("type")
                    if item_type == "UserMessage":
                        recognized["event_msg.item_completed.UserMessage"] += 1
                        append_event(
                            "item_completed",
                            sequence,
                            record,
                            "user-like",
                            _content_text(item.get("content")),
                            "codex.event_msg.item_completed.UserMessage",
                            item,
                        )
                    elif item_type == "AgentMessage":
                        recognized["event_msg.item_completed.AgentMessage"] += 1
                        append_event(
                            "item_completed",
                            sequence,
                            record,
                            "assistant",
                            _content_text(item.get("content")),
                            "codex.event_msg.item_completed.AgentMessage",
                            item,
                        )
                    elif item_type in {"CollabAgentToolCall", "SubAgentActivity"}:
                        recognized[f"event_msg.item_completed.ignored.{item_type}"] += 1
                    else:
                        recognized["event_msg.item_completed.ignored"] += 1
                        if any(
                            token in str(item_type or "").lower()
                            for token in ("user", "agent", "message")
                        ):
                            unknown[f"item_completed.{item_type}"] += 1
                elif message_type in _KNOWN_IGNORED_EVENT_MESSAGES:
                    recognized[f"event_msg.ignored.{message_type}"] += 1
                else:
                    # Codex adds lifecycle event kinds regularly. Only treat a
                    # new event as a possible message representation when its
                    # shape or name says it can carry conversation text.
                    message_like = (
                        any(key in payload for key in ("message", "item", "content"))
                        or "message" in str(message_type or "").lower()
                    )
                    target = unknown if message_like else recognized
                    target[f"event_msg.{message_type or 'missing-type'}"] += 1
                continue
            if record_type == "response_item":
                response_type = payload.get("type")
                role = payload.get("role")
                if response_type == "message" and role in {"user", "assistant"}:
                    block_types = {"input_text"} if role == "user" else {"output_text"}
                    text = _content_text(payload.get("content"), block_types)
                    recognized[f"response_item.message.{role}"] += 1
                    append_event(
                        "response_item",
                        sequence,
                        record,
                        "user-like" if role == "user" else "assistant",
                        text,
                        f"codex.response_item.message.{role}",
                        payload,
                    )
                elif (
                    response_type
                    in {
                        "reasoning",
                        "function_call",
                        "function_call_output",
                        "computer_call",
                    }
                    or role in {"developer", "system"}
                    or any(
                        token in str(response_type or "").lower()
                        for token in ("tool", "call", "reasoning")
                    )
                ):
                    recognized["response_item.ignored"] += 1
                elif role in {"user", "assistant"}:
                    unknown[f"response_item.{response_type or 'missing-type'}"] += 1
                else:
                    recognized["response_item.ignored.other"] += 1
                continue
            if record_type in {"compacted", "event_msg_delta"}:
                # Compaction summaries carry model-generated context under
                # "message", and deltas restate event_msg content; neither is
                # conversation, so they stay explicitly ignored.
                recognized[f"ignored.{record_type}"] += 1
            elif _may_carry_conversation(record_type, record):
                unknown[str(record_type or "missing-type")] += 1
            else:
                # Other top-level bookkeeping (turn context, world state,
                # token accounting, agent-communication metadata, ...) is
                # ignored by shape rather than by an allowlist of type names,
                # so a new bookkeeping record never stops an extraction.
                recognized[f"ignored.{record_type}"] += 1

        nonempty = [(name, events) for name, events in streams.items() if events]
        diagnostics: list[Diagnostic] = []
        merged: list[DecodedEvent] = []
        additions = 0
        divergences = 0
        if nonempty:
            nonempty.sort(key=lambda pair: (-len(pair[1]), _STREAM_PRIORITY[pair[0]]))
            canonical_name, canonical = nonempty[0]
            merged = list(canonical)
            for _, supplement in nonempty[1:]:
                added, conflicts = _merge_stream(merged, supplement)
                additions += added
                divergences += conflicts
            if additions:
                diagnostics.append(
                    Diagnostic(
                        "CODEX_STREAMS_COMPLEMENTED",
                        snapshot.source_id,
                        session_id,
                        additions,
                    )
                )
            if divergences:
                diagnostics.append(
                    Diagnostic(
                        "CODEX_STREAM_DIVERGENCE",
                        snapshot.source_id,
                        session_id,
                        divergences,
                    )
                )
        else:
            canonical_name = None

        accepted_users = sum(event.role_hint == "user-like" for event in merged)
        observations = FormatObservations(
            recognized_record_counts=dict(sorted(recognized.items())),
            unknown_record_counts={
                **({"malformed-jsonl": malformed} if malformed else {}),
                **dict(sorted(unknown.items())),
            },
            recognizable_user_markers=user_markers,
            accepted_direct_user_events=accepted_users,
        )
        if malformed:
            diagnostics.append(
                Diagnostic(
                    "CODEX_MALFORMED_RECORD", snapshot.source_id, session_id, malformed
                )
            )
        if unknown:
            diagnostics.append(
                Diagnostic(
                    "CODEX_UNKNOWN_MESSAGE_FORMAT",
                    snapshot.source_id,
                    session_id,
                    sum(unknown.values()),
                )
            )
        completeness = (
            "incomplete" if malformed or unknown or divergences else "complete"
        )
        if accepted_users == 0:
            if user_markers or unknown:
                diagnostics.append(
                    Diagnostic(
                        "CODEX_NO_DIRECT_INPUT",
                        snapshot.source_id,
                        session_id,
                        user_markers,
                    )
                )
            return DecodeBatch(
                sessions=(),
                observations=observations,
                rejected_sessions=(
                    RejectedSession(session_id, "NO_DIRECT_USER_EVENT"),
                ),
                completeness=completeness,
                diagnostics=tuple(diagnostics),
            )

        session = DecodedSession(
            session_id=session_id,
            cwd=cwd,
            project_hint=project_hint,
            conversation_kind="main",
            events=tuple(merged),
            metadata={
                "decoder": "codex/v1",
                "canonical_generation": canonical_name,
                "generations": tuple(name for name, _ in nonempty),
            },
        )
        return DecodeBatch(
            sessions=(session,),
            observations=observations,
            completeness=completeness,
            diagnostics=tuple(diagnostics),
        )


DECODER = CodexDecoder()

__all__ = ["DECODER", "CodexDecoder"]
