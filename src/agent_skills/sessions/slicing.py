"""Per-UTC-day slicing of long sessions.

A session that stays alive for several days used to be one growing file, so
every hourly extraction rewrote the whole history of earlier days and every
consumer keyed on that file saw those days change. A slice is the same
normalized Session restricted to the events of one UTC day; it carries ``day``
and therefore its own identity, filename date, and ``Day`` header. The cut is
strictly at UTC midnight: the first event dated a later day starts the next
slice, whatever its role. (Cutting at the next user turn instead was measured
on real data on 2026-09-04: autonomous agent sessions kept replying for hours
without a user turn, so 19% of Claude Code day files still grew past midnight,
half of them for more than 1.8 hours and one in ten for more than 16 hours.
A day file must be final once its day is over.) Events without timestamps stay
with the slice that is open when they occur; sessions with no timestamps at all
cannot be anchored to a day and stay whole.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from .audit import OutputInventory
from .manifest import DAY_SPLIT_MODES
from .model import Session


def _day(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None else None


def split_session(session: Session) -> tuple[Session, ...]:
    """Return the per-UTC-day slices of ``session`` (one element when single-day)."""
    first_day = next(
        (_day(event.timestamp) for event in session.events if event.timestamp),
        None,
    ) or _day(session.started_at)
    if first_day is None:
        return (session,)
    slices: list[tuple[str, list]] = [(first_day, [])]
    for event in session.events:
        day = _day(event.timestamp)
        if day is not None and day > slices[-1][0] and slices[-1][1]:
            slices.append((day, []))
        slices[-1][1].append(event)
    result = []
    for day, events in slices:
        timestamps = [event.timestamp for event in events if event.timestamp]
        result.append(
            replace(
                session,
                events=tuple(events),
                ended_at=max(timestamps) if timestamps else session.ended_at,
                day=day,
            )
        )
    return tuple(result)


def slice_sessions_by_day(
    sessions: tuple[Session, ...],
    *,
    mode: str,
    inventory: OutputInventory | None = None,
    legacy_prompt_digest: Callable[[Session], str] | None = None,
) -> tuple[Session, ...]:
    """Apply the configured ``day_split`` mode to deduplicated sessions.

    ``hybrid`` keeps a session whole when the inventory already holds output
    for its undivided identity (any legacy or current-format file, or an
    identity-less legacy prompt file matched by semantic digest); every other
    session is sliced, which is what makes the rule stable run after run.
    """
    if mode not in DAY_SPLIT_MODES:
        raise ValueError("unsupported day split mode")
    if mode == "off":
        return tuple(sessions)
    keep_whole: set[tuple[str, str, str]] = set()
    legacy_digests: set[str] = set()
    if mode == "hybrid" and inventory is not None:
        for entry in inventory.entries:
            if entry.kind not in {"history", "prompts"}:
                continue
            if entry.identity is not None:
                if not entry.headers.get("Day"):
                    keep_whole.add(entry.identity)
            elif entry.kind == "prompts" and entry.semantic_digest is not None:
                legacy_digests.add(entry.semantic_digest)
    result: list[Session] = []
    for session in sessions:
        if session.identity in keep_whole:
            result.append(session)
            continue
        if (
            legacy_digests
            and legacy_prompt_digest is not None
            and legacy_prompt_digest(session) in legacy_digests
        ):
            result.append(session)
            continue
        result.extend(split_session(session))
    return tuple(result)
