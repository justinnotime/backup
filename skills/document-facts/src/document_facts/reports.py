"""Deterministic reports over saved extraction facts."""

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


def relative_link(target, page):
    import os
    from urllib.parse import quote

    return quote(os.path.relpath(target, page.parent), safe="/.-_")


def frontmatter(settings, title, kind, sources, existing=None, **extra):
    created = datetime.now(timezone.utc).date().isoformat()
    if existing and existing.is_file():
        text = existing.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            try:
                previous = yaml.safe_load(text.split("---", 2)[1])
                if isinstance(previous, dict) and previous.get("created"):
                    created = str(previous["created"])
            except (ValueError, yaml.YAMLError):
                pass
    meta = {
        k: v
        for k, v in settings.metadata.items()
        if k not in {"timeline_title", "generator_label", "threads_title"}
    }
    meta.update(title=title, type=kind, created=created, sources=sources, **extra)
    return (
        "---\n"
        + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
        + "\n---\n\n"
    )


def task_lines(task):
    lines = [f"- **[{task['status']}]** {task['task']}"]
    lines.extend(f"  - {value}" for value in task.get("subtasks", []))
    for label, key in (
        ("blockers", "blockers"),
        ("files", "files_touched"),
        ("related", "related_to"),
    ):
        if task.get(key):
            lines.append(f"  - {label}: {'; '.join(task[key])}")
    if task.get("solution"):
        lines.append(f"  - solution: {task['solution']}")
    return lines


def decision_lines(decision):
    lines = [f"- **{decision['decision']}** — {decision['rationale']}"]
    if decision.get("alternative_rejected"):
        lines.append(f"  - rejected: {decision['alternative_rejected']}")
    return lines


def blocker_lines(blocker):
    lines = [f"- Blocker: {blocker['blocker']}"]
    if blocker.get("solution"):
        lines.append(f"  - Solution: {blocker['solution']}")
    return lines


def render_digest(settings, document, chunks, page):
    if not chunks:
        return ""
    title = document.manifest.get("title") or document.slug
    source = (
        str(
            (settings.source_directory / document.source_slug).relative_to(
                settings.root
            )
        )
        + "/"
    )
    lines = [
        frontmatter(
            settings, f"Extraction: {title}", "extraction", [source], page
        ).rstrip(),
        "",
        f"# Extraction — {title}",
        "",
    ]
    source_url = document.manifest.get("sourceUrl", "")
    if source_url:
        lines.extend([f"Source: [{source_url}]({source_url})  ", ""])
    lines.extend(
        [
            f"Slug: `{document.slug}`  ",
            f"Chunks: {len(chunks)}  ",
            "",
            "_Mechanically aggregated from saved per-chunk model extractions. This snapshot may lag current source documents. Regenerate with `--digests-only`._",
            "",
        ]
    )
    for heading, values in (
        ("Date span", sorted({v for c in chunks for v in c["dates_found"]})),
        ("People", sorted({v for c in chunks for v in c["people"]})),
    ):
        if values:
            lines.extend([f"## {heading}", "", ", ".join(values), ""])
    for chunk in chunks:
        lines.extend(
            [
                f"## {chunk['heading']}",
                f"<sub>chunk: `{chunk['chunk_id']}` · {chunk.get('char_count', 0):,} chars</sub>",
                "",
            ]
        )
        if chunk["dates_found"]:
            lines.extend([f"**Dates:** {', '.join(chunk['dates_found'])}", ""])
        for title, field, formatter in (
            ("Tasks", "tasks", task_lines),
            ("Decisions", "decisions", decision_lines),
            ("Blockers & solutions", "blockers_solutions", blocker_lines),
        ):
            if chunk[field]:
                lines.extend([f"### {title}", ""])
                for entry in chunk[field]:
                    lines.extend(formatter(entry))
                lines.append("")
        if chunk["concepts"]:
            lines.extend(["### Concepts introduced", ""])
            for concept in chunk["concepts"]:
                aliases = (
                    f" (aliases: {', '.join(concept['aliases'])})"
                    if concept["aliases"]
                    else ""
                )
                lines.append(
                    f"- **{concept['term']}**{aliases} — {concept['definition']}"
                )
            lines.append("")
        if chunk["notable_quotes"]:
            lines.extend(["### Notable quotes", ""])
            for quote in chunk["notable_quotes"]:
                lines.extend(["> " + quote.replace("\n", " ").strip(), ""])
        if chunk["references"]:
            lines.extend(
                [
                    "**References:** "
                    + ", ".join(f"`{r}`" for r in chunk["references"]),
                    "",
                ]
            )
        lines.extend(["---", ""])
    return "\n".join(lines).rstrip() + "\n"


def collect_dated_events(documents, all_chunks):
    dated, undated = [], []
    for document in documents:
        last_date = None
        for chunk in all_chunks[document.slug]:
            dates = sorted(chunk["dates_found"])
            anchor = dates[0] if dates else last_date
            if dates:
                last_date = dates[-1]
            for kind, field in (
                ("task", "tasks"),
                ("decision", "decisions"),
                ("blocker", "blockers_solutions"),
            ):
                for payload in chunk[field]:
                    event = {
                        "kind": kind,
                        "date": anchor,
                        "doc_slug": document.slug,
                        "doc_title": document.manifest.get("title") or document.slug,
                        "chunk_id": chunk["chunk_id"],
                        "heading": chunk["heading"],
                        "payload": payload,
                    }
                    (dated if anchor else undated).append(event)
    dated.sort(key=lambda e: (e["date"], e["doc_slug"], e["chunk_id"]))
    return dated, undated


def render_timeline(settings, documents, all_chunks):
    title = settings.metadata.get(
        "timeline_title", "Document facts — chronological timeline"
    )
    sources = [str(settings.output_directory.relative_to(settings.root)) + "/"]
    lines = [
        frontmatter(
            settings, title, "extraction-timeline", sources, settings.timeline_file
        ).rstrip(),
        "",
        f"# {title}",
        "",
        "Tasks, decisions, and blockers are grouped by the earliest date in each chunk. "
        "This saved extraction snapshot may lag current source documents. "
        "Undated chunks inherit the most recent date from an earlier chunk in the same document; "
        "entries with no preceding date appear under Undated.",
        "",
    ]
    dated, undated = collect_dated_events(documents, all_chunks)
    by_date = defaultdict(list)
    for event in dated:
        by_date[event["date"]].append(event)
    if undated:
        by_date["Undated (no date evidence in chunk or document)"] = undated
    for date, events in by_date.items():
        lines.extend([f"## {date}", ""])
        by_doc = defaultdict(list)
        for event in events:
            by_doc[event["doc_slug"]].append(event)
        for slug, doc_events in sorted(by_doc.items()):
            lines.extend([f"### {doc_events[0]['doc_title']}", ""])
            links = []
            for chunk_id in sorted({event["chunk_id"] for event in doc_events}):
                target = settings.output_directory / slug / f"{chunk_id}.yaml"
                links.append(
                    f"[`{chunk_id}`]({relative_link(target, settings.timeline_file)})"
                )
            lines.extend([f"<sub>chunks: {', '.join(links)}</sub>", ""])
            for kind, label, formatter in (
                ("task", "Tasks", task_lines),
                ("decision", "Decisions", decision_lines),
                ("blocker", "Blockers & solutions", blocker_lines),
            ):
                selected = [event for event in doc_events if event["kind"] == kind]
                if selected:
                    lines.append(f"**{label}:**")
                    for event in selected:
                        lines.extend(formatter(event["payload"]))
                    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_thread_index(settings, present):
    path = settings.threads_directory / "README.md"
    title = settings.metadata.get("threads_title", "Document facts — thematic threads")
    sources = [str(settings.output_directory.relative_to(settings.root)) + "/"]
    lines = [
        frontmatter(settings, title, "thread-index", sources, path).rstrip(),
        "",
        f"# {title}",
        "",
        "Themes are defined in the extraction configuration. Each page cites the facts used to synthesize it.",
        "",
    ]
    for theme in settings.threads:
        if theme["slug"] in present:
            lines.extend(
                [
                    f"- [{theme['title']}]({relative_link(Path(theme['slug'] + '.md'), Path('README.md'))})",
                    f"  {theme['what_it_covers']}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"
