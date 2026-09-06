"""Stable output naming and collision handling."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime

from .model import Session


def safe_component(value: str, *, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (cleaned or "unknown")[:limit]


def identity_digest(identity: tuple[str, str, str], length: int = 12) -> str:
    return hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()[:length]


def date_for(session: Session) -> str:
    if session.day is not None:
        return session.day
    return (
        session.started_at.strftime("%Y-%m-%d") if session.started_at else "0000-00-00"
    )


def short_session_id(session_id: str) -> str:
    cleaned = safe_component(session_id, limit=80)
    return cleaned[-12:] if len(cleaned) > 12 else cleaned


def base_filename(session: Session, strategy: str = "project-session-suffix") -> str:
    if strategy == "session-date-prefix-8":
        source_date = None
        if session.day is None:
            value = session.metadata.get("timestamp")
            if isinstance(value, str):
                try:
                    source_date = datetime.fromisoformat(value).strftime("%Y-%m-%d")
                except ValueError:
                    pass
        selected_date = source_date or (
            date_for(session) if session.started_at or session.day else "unknown-date"
        )
        return f"session-{selected_date}_{safe_component(session.session_id, limit=80)[:8]}.md"
    if strategy == "project-session-suffix":
        component = (
            f"{safe_component(session.project)}_{short_session_id(session.session_id)}"
        )
    elif strategy == "session-prefix-8":
        component = safe_component(session.session_id, limit=80)[:8]
    elif strategy == "session-last-component-prefix-8":
        component = safe_component(session.session_id.rsplit("-", 1)[-1])[:8]
    elif strategy == "session-suffix-8":
        component = safe_component(session.session_id, limit=80)[-8:]
    elif strategy == "node-session-sha256-12":
        component = hashlib.sha256(
            f"{session.node_label}\0{session.session_id}".encode()
        ).hexdigest()[:12]
    else:
        raise ValueError("unsupported filename strategy")
    return f"{date_for(session)}_{component or 'unknown'}.md"


def allocate_filenames(
    sessions: tuple[Session, ...],
    *,
    strategies: dict[tuple[str, str, str], str] | None = None,
    destinations: dict[tuple[str, str, str], str] | None = None,
) -> dict[tuple[str, str, str], str]:
    """Allocate names from the complete set, independent of scan order."""
    strategies = strategies or {}
    destinations = destinations or {}
    by_base: dict[tuple[str, str], list[Session]] = defaultdict(list)
    for session in sessions:
        base = base_filename(
            session, strategies.get(session.identity, "project-session-suffix")
        )
        by_base[(destinations.get(session.identity, ""), base)].append(session)
    allocated: dict[tuple[str, str, str], str] = {}
    used: set[tuple[str, str]] = set()
    for destination, base in sorted(by_base):
        group = sorted(by_base[(destination, base)], key=lambda item: item.identity)
        stem = base[:-3]
        harness_counts: dict[str, int] = defaultdict(int)
        for session in group:
            harness_counts[session.harness] += 1
        for session in group:
            if len(group) == 1:
                candidate = base
            elif harness_counts[session.harness] == 1:
                candidate = f"{stem}--{safe_component(session.harness)}.md"
            else:
                candidate = (
                    f"{stem}--{safe_component(session.harness)}-"
                    f"{safe_component(session.node_label, limit=20)}.md"
                )
            if (destination, candidate) in used:
                candidate = f"{stem}--{identity_digest(session.identity)}.md"
            used.add((destination, candidate))
            allocated[session.identity] = candidate
    return allocated


def relative_output_path(
    directory: str, layout: str, session: Session, filename: str
) -> str:
    if layout == "monthly":
        if filename.startswith("session-"):
            date_match = re.match(r"session-(\d{4}-\d{2})-\d{2}_", filename)
            bucket = date_match.group(1) if date_match else "unknown"
            return f"{directory}/{bucket}/{filename}"
        return f"{directory}/{date_for(session)[:7]}/{filename}"
    return f"{directory}/{filename}"
