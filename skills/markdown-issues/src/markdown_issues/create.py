"""Create a new Markdown issue without staging, committing or publishing it."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigurationError, local_path

DEFAULT_BODY = """# {{title}}

{{context_heading}}

Describe the source event and link to supporting evidence.

{{acceptance_heading}}

- [ ] Describe a verifiable completion condition.

{{notes_heading}}

- {{now}} [{{actor}}] opened.
"""


def scalar(value: str) -> str:
    if any(character in value for character in "\r\n\0"):
        raise ConfigurationError("creation fields must be single-line text")
    return value


def create_issue(cfg: dict, args, *, now: datetime | None = None) -> tuple[str, str]:
    """Return path and complete proposed text; write only outside dry-run mode."""
    from . import tracker

    tracker.configure(cfg)
    parse_date = tracker.parse_date

    root = Path(cfg["repository_root"])
    title = scalar(args.title)
    actor = scalar(args.actor or cfg["default_actor"])
    assignee = scalar(args.assignee or cfg["default_assignee"])
    if (
        actor not in cfg["actors"]
        or actor == cfg["unassigned_actor"]
        or assignee not in cfg["actors"]
    ):
        raise ConfigurationError("unknown creation actor or assignee")
    if args.priority not in cfg["priorities"] or args.kind not in cfg["kinds"]:
        raise ConfigurationError("invalid priority or kind")
    review = args.review_after or ""
    if review and parse_date(review) is None:
        raise ConfigurationError("review-after must be YYYY-MM-DD")
    sub_state = args.sub_state or ("scheduled" if args.kind == "watch" and review else "")
    if sub_state and sub_state not in cfg["sub_states"]:
        raise ConfigurationError("invalid sub-state")
    if review and args.kind != "watch" and sub_state not in {"waiting-human", "scheduled"}:
        raise ConfigurationError(
            "review-after only schedules watch, waiting-human, or scheduled work"
        )
    project = scalar(args.project or "")
    sources = [scalar(value) for value in args.source]
    watched = [scalar(value) for value in args.watch_path]
    labels = [scalar(value.strip()) for value in (args.labels or "").split(",") if value.strip()]
    for value in sources:
        if not local_path(root, value).exists():
            raise ConfigurationError("creation source does not exist")
    for value in watched:
        local_path(root, value, glob=True)
        if not list(root.glob(value)):
            raise ConfigurationError("creation watched path does not exist")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = "-".join(slug.split("-")[:6])[:50].rstrip("-")
    if not slug:
        raise ConfigurationError("title must contain text suitable for a filename")
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = instant.strftime("%Y-%m-%dT%H:%M:%SZ")
    day = instant.strftime("%Y-%m-%d")
    identity_input = f"{day}_{slug}_{actor}_{stamp}"
    suffix = hashlib.md5(identity_input.encode(), usedforsecurity=False).hexdigest()[:8]
    identity = f"{day}_{slug}_{suffix}"
    fields = {
        "id": identity,
        "title": title,
        "created": stamp,
        "updated": stamp,
        "state": "open",
        "assignee": assignee,
        "priority": args.priority,
        "kind": args.kind,
    }
    if sub_state:
        fields["sub_state"] = sub_state
    if project:
        fields["project"] = project
    if review:
        fields["review_after"] = review
    lines = ["---"]
    for key, value in fields.items():
        encoded = json.dumps(value, ensure_ascii=False) if key == "title" else value
        lines.append(f"{key}: {encoded}")
    for key, values in (
        ("labels", labels),
        ("sources", sources),
        ("watch_paths", watched),
        ("related", []),
        ("external_refs", []),
        ("blocks", []),
        ("blocked_by", []),
    ):
        lines.append(key + ":" if values else key + ": []")
        lines.extend("  - " + json.dumps(value, ensure_ascii=False) for value in values)
    substitutions = {
        "title": title,
        "actor": actor,
        "now": stamp,
        **{key + "_heading": value for key, value in cfg["headings"].items()},
    }
    body = re.sub(
        r"\{\{([a-z_]+)\}\}",
        lambda match: substitutions.get(match[1], match[0]),
        cfg.get("body_template", DEFAULT_BODY),
    )
    text = "\n".join([*lines, "---", "", body.rstrip(), ""])
    relative = str(Path(cfg["open_directory"]) / f"{identity}.md")
    target = local_path(root, relative)
    proposal = tracker.parse_issue_text(text, target, root)
    if any(
        finding.level == "ERROR"
        for finding in tracker.audit_issues(root, [proposal], instant.date())
    ):
        raise ConfigurationError("creation template does not satisfy configured issue rules")
    if target.exists() or target.is_symlink():
        raise ConfigurationError("issue already exists; existing content is preserved")
    if not args.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".new-issue-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.chmod(temporary, 0o644)
            os.link(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return relative, text


def command_create(args) -> int:
    relative, text = create_issue(args.configuration, args)
    if args.dry_run:
        print(text, end="")
    else:
        print(relative)
    return 0
