"""Build indexes from preserved on-disk output plus the publication plan."""

from __future__ import annotations

import hashlib

from .audit import OutputInventory, entry_from_content
from .manifest import Manifest
from .model import PlannedFile, PublicationPlan
from .render import MANAGED_BY


def should_build_indexes(manifest: Manifest) -> bool:
    mode = manifest.indexes_mode
    if mode == "none":
        return False
    if mode == "every-node":
        return True
    if mode == "owner":
        return manifest.ownership_mode == "owner"
    return mode == "aggregator-only" and manifest.ownership_mode == "aggregator"


def _render_index(kind: str, directory: str, entries) -> bytes:
    title = "Agent session history" if kind == "history" else "Agent session prompts"
    lines = [
        f"# {title}",
        "",
        f"- Managed-By: {MANAGED_BY}",
        "- View: index",
        "",
        "| Date | Tool | Host | Project | Session | File |",
        "|---|---|---|---|---|---|",
    ]
    for entry in sorted(entries, key=lambda item: item.relative_path):
        headers = entry.headers
        filename = entry.relative_path.rsplit("/", 1)[-1]
        date = filename[:10] if len(filename) >= 10 else "unknown"
        relative_link = (
            entry.relative_path[len(directory) + 1 :]
            if entry.relative_path.startswith(directory + "/")
            else entry.relative_path
        )
        lines.append(
            "| {date} | {tool} | {host} | {project} | `{session}` | [{title}]({link}) |".format(
                date=date,
                tool=headers.get("Tool", "unknown"),
                host=headers.get("Host", "unknown"),
                project=headers.get("Project", "unknown"),
                session=headers.get("Session", "unknown"),
                title=entry.title or filename,
                link=relative_link,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def add_indexes(
    manifest: Manifest, inventory: OutputInventory, plan: PublicationPlan
) -> PublicationPlan:
    if not should_build_indexes(manifest):
        return plan
    effective = inventory.by_path()
    for removal in plan.removals:
        effective.pop(removal.relative_path, None)
    for planned in plan.writes:
        if planned.kind in {"history", "prompt"}:
            effective[planned.relative_path] = entry_from_content(
                planned.relative_path, planned.content
            )
    writes = [item for item in plan.writes if item.kind != "index"]
    views = [
        ("history", directory, "history")
        for directory in manifest.output.history_directories()
    ]
    if manifest.output.prompt_directory is not None:
        views.append(("prompts", manifest.output.prompt_directory, "prompts"))
    for kind, directory, entry_kind in views:
        entries = [
            entry
            for entry in effective.values()
            if entry.kind == entry_kind
            and (
                entry_kind != "history"
                or entry.relative_path.startswith(directory + "/")
            )
        ]
        relative_path = f"{directory}/README.md"
        content = _render_index(kind, directory, entries)
        prior = effective.get(relative_path)
        if prior is not None and prior.digest == hashlib.sha256(content).hexdigest():
            continue
        writes.append(PlannedFile(relative_path, content, None, "index"))
    return PublicationPlan(tuple(writes), plan.removals, plan.diagnostics)
