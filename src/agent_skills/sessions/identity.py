"""Stable output naming and collision handling."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .model import Session


def safe_component(value: str, *, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (cleaned or "unknown")[:limit]


def identity_digest(identity: tuple[str, str, str], length: int = 12) -> str:
    return hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()[:length]


def date_for(session: Session) -> str:
    return (
        session.started_at.strftime("%Y-%m-%d") if session.started_at else "0000-00-00"
    )


def short_session_id(session_id: str) -> str:
    cleaned = safe_component(session_id, limit=80)
    return cleaned[-12:] if len(cleaned) > 12 else cleaned


def base_filename(session: Session) -> str:
    return f"{date_for(session)}_{safe_component(session.project)}_{short_session_id(session.session_id)}.md"


def allocate_filenames(
    sessions: tuple[Session, ...],
) -> dict[tuple[str, str, str], str]:
    """Allocate names from the complete set, independent of scan order."""
    by_base: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        by_base[base_filename(session)].append(session)
    allocated: dict[tuple[str, str, str], str] = {}
    used: set[str] = set()
    for base in sorted(by_base):
        group = sorted(by_base[base], key=lambda item: item.identity)
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
            if candidate in used:
                candidate = f"{stem}--{identity_digest(session.identity)}.md"
            used.add(candidate)
            allocated[session.identity] = candidate
    return allocated


def relative_output_path(
    directory: str, layout: str, session: Session, filename: str
) -> str:
    if layout == "monthly":
        return f"{directory}/{date_for(session)[:7]}/{filename}"
    return f"{directory}/{filename}"
