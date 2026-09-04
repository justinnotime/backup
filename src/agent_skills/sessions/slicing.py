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

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .audit import InventoryEntry, OutputInventory
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


def _legacy_end_day(entry: InventoryEntry) -> str | None:
    """Last UTC day a whole-session file covers, from its Ended or Time range header."""
    for key in ("Ended", "Time range"):
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", entry.headers.get(key, ""))
        if dates:
            return max(dates)
    return None


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def slice_sessions_by_day(
    sessions: tuple[Session, ...],
    *,
    mode: str,
    inventory: OutputInventory | None = None,
    legacy_prompt_digest: Callable[[Session], str] | None = None,
    today: str | None = None,
) -> tuple[Session, ...]:
    """Apply the configured ``day_split`` mode to deduplicated sessions.

    ``hybrid``: a session that already has a whole-session history file keeps
    that file for the days it already covers, up to and including the last day
    before ``today``; every later day becomes its own day file. The boundary is
    ``min(end day of the existing file, yesterday)``, so on the cutover day the
    current day's events move out of the old file (which is rewritten once and
    then never changes) and on later days the boundary is simply the old file's
    end day, which no longer advances. A session with only an identity-less
    legacy prompt file (matched by semantic digest) stays whole. Every other
    session is sliced. ``all`` slices everything, ``off`` changes nothing.
    """
    if mode not in DAY_SPLIT_MODES:
        raise ValueError("unsupported day split mode")
    if mode == "off":
        return tuple(sessions)
    today = today or _today()
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    legacy_history: dict[tuple[str, str, str], InventoryEntry] = {}
    legacy_prompt_identities: set[tuple[str, str, str]] = set()
    legacy_digests: set[str] = set()
    if mode == "hybrid" and inventory is not None:
        for entry in inventory.entries:
            if entry.kind not in {"history", "prompts"} or entry.headers.get("Day"):
                continue
            if entry.identity is None:
                if entry.kind == "prompts" and entry.semantic_digest is not None:
                    legacy_digests.add(entry.semantic_digest)
            elif entry.kind == "history":
                legacy_history[entry.identity] = entry
            else:
                legacy_prompt_identities.add(entry.identity)
    result: list[Session] = []
    for session in sessions:
        entry = legacy_history.get(session.identity)
        if entry is not None:
            end_day = _legacy_end_day(entry)
            if end_day is None:
                result.append(session)
                continue
            boundary = min(end_day, yesterday)
            head = [
                event
                for event in session.events
                if _day(event.timestamp) is None or _day(event.timestamp) <= boundary
            ]
            tail = [
                event
                for event in session.events
                if _day(event.timestamp) is not None and _day(event.timestamp) > boundary
            ]
            if head:
                stamps = [event.timestamp for event in head if event.timestamp]
                result.append(
                    replace(
                        session,
                        events=tuple(head),
                        ended_at=max(stamps) if stamps else session.ended_at,
                    )
                )
            if tail:
                result.extend(split_session(replace(session, events=tuple(tail))))
            continue
        if session.identity in legacy_prompt_identities:
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
