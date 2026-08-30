"""Render history and prompt views from one normalized Session."""

from __future__ import annotations

from datetime import datetime

from .manifest import OutputSpec
from .model import Session

MANAGED_BY = "agent-session-extraction/v1"


def format_timestamp(value: datetime | None, quality: str = "exact") -> str:
    if value is None:
        return "unknown"
    rendered = value.strftime("%Y-%m-%d %H:%M:%SZ")
    return f"~{rendered}" if quality == "approximate" else rendered


def _quote(text: str) -> str:
    return "\n".join(
        f"> {line}" if line else ">" for line in text.rstrip().splitlines()
    )


def _title(session: Session) -> str:
    event = next(
        (item for item in session.events if item.role == "user"), session.events[0]
    )
    title = event.text.splitlines()[0].strip().lstrip("#").strip()
    return (title[:70] or "Untitled session").replace("`", "'")


def _headers(session: Session, output: OutputSpec, kind: str) -> list[str]:
    lines = [
        f"Managed-By: {MANAGED_BY}",
        f"Schema: {session.schema_version}",
        f"View: {kind}",
        f"Tool: {session.harness}",
        f"Host: {session.node_label}",
        f"Session: {session.session_id}",
        f"Source: {session.source_ref}",
        f"Project: {session.project}",
    ]
    for key in sorted(output.encryption_attributes):
        lines.append(f"{key}: {output.encryption_attributes[key]}")
    return lines


def render_history(session: Session, output: OutputSpec) -> str:
    lines = [f"# {_title(session)}", ""]
    lines.extend(f"- {header}" for header in _headers(session, output, "history"))
    if session.cwd:
        lines.append(f"- Cwd: {session.cwd}")
    lines.extend(
        [
            f"- Started: {format_timestamp(session.started_at)}",
            f"- Ended: {format_timestamp(session.ended_at)}",
            "",
            "---",
            "",
        ]
    )
    for event in session.events:
        lines.extend(
            [
                f"### {format_timestamp(event.timestamp, event.timestamp_quality)} — {event.role}",
                "",
                _quote(event.text) if event.role == "user" else event.text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def truncate_prompt(text: str, *, maximum: int, code_block_maximum: int) -> str:
    """Bound total text and each fenced block while keeping fences balanced."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    total = 0
    in_fence = False
    fence_chars = 0
    truncated = False
    for line in lines:
        is_fence = line.lstrip().startswith("```")
        if is_fence:
            addition = line
            in_fence = not in_fence
            if in_fence:
                fence_chars = 0
        elif in_fence:
            remaining_block = code_block_maximum - fence_chars
            if remaining_block <= 0:
                truncated = True
                continue
            addition = line[:remaining_block]
            fence_chars += len(addition)
            if len(addition) < len(line):
                truncated = True
        else:
            addition = line
        remaining = maximum - total
        if remaining <= 0:
            truncated = True
            break
        addition = addition[:remaining]
        out.append(addition)
        total += len(addition)
        if len(addition) < len(line) and not is_fence:
            truncated = True
            break
    result = "".join(out).rstrip()
    if in_fence:
        closing = "\n```"
        if len(result) + len(closing) <= maximum:
            result += closing
        else:
            truncated = True
    marker = "\n[truncated]"
    if truncated:
        result = result[: maximum - len(marker)].rstrip()
        if result.count("```") % 2:
            closing = "\n```"
            result = result[: maximum - len(marker) - len(closing)].rstrip()
            if result.count("```") % 2:
                result += closing
        result += marker
    return result


def render_prompts(session: Session, output: OutputSpec) -> str:
    prompts = [event for event in session.events if event.role == "user"]
    lines = [f"# Prompts — {_title(session)}", ""]
    lines.extend(f"- {header}" for header in _headers(session, output, "prompts"))
    lines.extend(["", "---", ""])
    for event in prompts:
        lines.extend(
            [
                f"### {format_timestamp(event.timestamp, event.timestamp_quality)}",
                "",
                truncate_prompt(
                    event.text,
                    maximum=output.prompt_max_chars,
                    code_block_maximum=output.prompt_code_block_max_chars,
                ),
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
