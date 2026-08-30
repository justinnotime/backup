"""Consumer-configured filtering and normalization over decoded sessions."""

from __future__ import annotations

from pathlib import PurePath

from .manifest import Manifest, SourceSpec
from .model import (
    NORMALIZED_SCHEMA_VERSION,
    DecodedSession,
    Diagnostic,
    Event,
    Session,
)


class PolicyError(RuntimeError):
    pass


def _project(decoded: DecodedSession) -> str:
    if decoded.project_hint and decoded.project_hint.strip():
        return decoded.project_hint.strip()
    if decoded.cwd:
        name = PurePath(decoded.cwd).name
        if name:
            return name
    return "unknown"


def normalize_decoded(
    decoded: DecodedSession,
    *,
    manifest: Manifest,
    source: SourceSpec,
    source_ref: str,
) -> Session | None:
    policy = manifest.event_policy
    if (
        decoded.conversation_kind == "conversational-subagent"
        and not policy.retain_conversational_subagents
    ):
        return None

    kept = []
    user_like = []
    for event in decoded.events:
        text = event.text.strip()
        if not text:
            continue
        if event.role_hint == "user-like":
            if any(text.startswith(prefix) for prefix in policy.synthetic_prefixes):
                continue
            user_like.append(event)
        kept.append(event)

    # This decision precedes peer-agent relabeling. A policy change in role
    # attribution therefore cannot silently remove a session.
    if (
        len(user_like) < policy.min_direct_user_events
        and max((len(event.text) for event in user_like), default=0)
        < policy.min_user_chars
    ):
        return None

    project = manifest.project_policy.aliases.get(_project(decoded), _project(decoded))
    if project == "unknown":
        if manifest.project_policy.unknown == "drop":
            return None
        if manifest.project_policy.unknown == "fail":
            raise PolicyError("project is unknown")
    if (
        manifest.project_policy.mode == "allowlist"
        and project not in manifest.project_policy.allowlist
    ):
        return None
    if (
        manifest.project_policy.mode == "denylist"
        and project in manifest.project_policy.denylist
    ):
        return None

    events = []
    for event in kept:
        if event.role_hint == "assistant":
            role = "assistant"
        else:
            stripped = event.text.strip()
            is_peer = stripped in policy.peer_agent_exact or any(
                stripped.startswith(prefix) for prefix in policy.peer_agent_prefixes
            )
            role = "peer-agent" if is_peer else "user"
        events.append(
            Event(
                sequence=len(events),
                timestamp=event.timestamp,
                timestamp_quality=event.timestamp_quality,
                role=role,  # type: ignore[arg-type]
                text=event.text.strip(),
                raw_kind=event.raw_kind,
                metadata=event.metadata,
            )
        )
    if not events:
        return None
    timestamps = [event.timestamp for event in events if event.timestamp is not None]
    metadata = dict(decoded.metadata)
    metadata["conversation_kind"] = decoded.conversation_kind
    metadata["retention_user_events"] = len(user_like)
    return Session(
        NORMALIZED_SCHEMA_VERSION,
        source.harness,  # type: ignore[arg-type]
        decoded.session_id,
        source_ref,
        source.output_node,
        decoded.cwd,
        project,
        min(timestamps) if timestamps else None,
        max(timestamps) if timestamps else None,
        tuple(events),
        metadata,
    )


def deduplicate_sessions(
    sessions: list[Session],
) -> tuple[tuple[Session, ...], tuple[Diagnostic, ...]]:
    grouped: dict[tuple[str, str, str], list[Session]] = {}
    for session in sessions:
        grouped.setdefault(session.identity, []).append(session)
    selected = []
    diagnostics = []
    for identity in sorted(grouped):
        candidates = grouped[identity]
        candidates.sort(key=lambda item: (-len(item.events), item.source_ref))
        winner = candidates[0]
        selected.append(winner)
        signatures = {
            tuple((event.role, event.text, event.timestamp) for event in item.events)
            for item in candidates
        }
        if len(signatures) > 1:
            diagnostics.append(
                Diagnostic(
                    "DUPLICATE_SESSION_DIVERGENCE",
                    winner.source_ref.split("/", 1)[0],
                    winner.session_id,
                    len(candidates),
                )
            )
    return tuple(selected), tuple(diagnostics)
