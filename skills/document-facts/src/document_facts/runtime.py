"""LLM calls > 0 in extraction and thread modes; other modes are offline."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import ExtractionError, confined, load_config
from .content import (
    build_user_prompt,
    chunk_matches_theme,
    chunk_text_for_match,
    compact_chunk_for_theme,
    make_chunks,
)
from .provider import call_llm, make_client
from .reports import (
    frontmatter,
    relative_link,
    render_digest,
    render_thread_index,
    render_timeline,
)
from .storage import (
    infer_year_context,
    load_documents,
    parse_response,
    read_yaml,
    signature,
    validate_facts,
    write_text,
)


class Extractor:
    def __init__(self, settings, documents=None):
        self.settings = settings
        self.documents = load_documents(settings) if documents is None else documents
        self.chunks = {
            doc.slug: make_chunks(
                doc.slug,
                doc.content,
                settings.budget["max_chunk_chars"],
                settings.budget["soft_split_chars"],
            )
            for doc in self.documents
        }
        destinations = [
            settings.state_file,
            settings.timeline_file,
            confined(settings.threads_directory, "README.md"),
        ]
        destinations += [
            confined(settings.threads_directory, theme["slug"] + ".md")
            for theme in settings.threads
        ]
        for doc in self.documents:
            destinations.append(
                confined(settings.output_directory, Path(doc.slug) / "README.md")
            )
            chunk_paths = {
                self.chunk_path(doc, chunk) for chunk in self.chunks[doc.slug]
            }
            output = confined(settings.output_directory, doc.slug)
            chunk_paths.update(
                confined(output, path.name) for path in output.glob("*.yaml")
            )
            destinations.extend(chunk_paths)
        if len(destinations) != len(set(destinations)):
            raise ExtractionError("configured output files collide")
        if any(
            destination == protected or destination in protected.parents
            for destination in destinations
            for protected in settings.protected_paths
        ):
            raise ExtractionError(
                "configured output collides with configuration or credential input"
            )
        if any(
            path.is_dir()
            or any(path in other.parents for other in destinations if other != path)
            for path in destinations
        ):
            raise ExtractionError("configured output file overlaps an output directory")

    def chunk_path(self, document, chunk):
        return confined(
            self.settings.output_directory,
            Path(document.slug) / (chunk["chunk_id"] + ".yaml"),
        )

    def chunk_signature(self, document, chunk, index):
        return signature(
            self.settings.prompts["extract"],
            self.settings.llm.get("model"),
            self.settings.llm.get("base_url"),
            document.doc_id,
            document.manifest.get("title"),
            document.manifest.get("sourceUrl"),
            infer_year_context(document, self.settings.year_range),
            chunk["heading"],
            index,
            len(self.chunks[document.slug]),
            self.settings.budget["max_tokens"],
        )

    def saved_fact(self, document, path):
        value = read_yaml(path)
        validate_facts(value)
        if (
            value.get("doc_slug") not in {document.slug, *document.previous_slugs}
            or value.get("chunk_id") != path.stem
        ):
            raise ExtractionError("saved chunk identity does not match its path")
        if value.get("doc_id", document.doc_id) != document.doc_id:
            raise ExtractionError("saved chunk belongs to another document")
        input_hash = value.get("input_sha1", value.get("content_sha1"))
        if not isinstance(input_hash, str) or not re.fullmatch(
            r"[0-9a-f]{40}", input_hash
        ):
            raise ExtractionError("saved chunk requires a valid input hash")
        if not isinstance(value.get("heading"), str):
            raise ExtractionError("saved chunk requires a heading")
        return {**value, "doc_slug": document.slug}

    def saved(self, document, chunk, index, check_signature=True):
        path = self.chunk_path(document, chunk)
        if not path.exists():
            return None
        value = self.saved_fact(document, path)
        if value.get("input_sha1", value.get("content_sha1")) != chunk["sha1"]:
            return None
        # Old outputs already carry input_sha1. Adopt them without depending on
        # the old ignored cache; new outputs also track extraction semantics.
        if check_signature and value.get("extraction_signature") not in {
            None,
            self.chunk_signature(document, chunk, index),
        }:
            return None
        # Historical files may keep their old serialized slug after a directory
        # rename. Normalize only the in-memory report view; the saved YAML stays
        # untouched and the configured aliases do not relax any content checks.
        return {**value, "doc_slug": document.slug}

    def read_snapshot(self, document):
        """Read the saved extraction snapshot without consulting current text."""
        directory = confined(self.settings.output_directory, document.slug)
        return [
            self.saved_fact(document, confined(directory, path.name))
            for path in sorted(directory.glob("*.yaml"))
        ]

    def read_chunks(self, document):
        result = []
        for index, chunk in enumerate(self.chunks[document.slug]):
            saved = self.saved(document, chunk, index, check_signature=False)
            if saved is None:
                continue
            result.append(saved)
        return result

    def extraction(self, client=None, force=False, dry_run=False, limit=None):
        settings = self.settings
        state = {}
        if settings.state_file.exists():
            try:
                state = json.loads(settings.state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ExtractionError("cannot read extraction state") from None
            if not isinstance(state, dict) or any(
                not isinstance(v, dict) for v in state.values()
            ):
                raise ExtractionError("invalid extraction state")
        totals = {
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_input_tokens": 0,
            "maximum_output_tokens": 0,
        }
        for document in self.documents:
            chunks = self.chunks[document.slug]
            for index, chunk in enumerate(chunks):
                saved = self.saved(document, chunk, index)
                if saved is not None and not force:
                    totals["skipped"] += 1
                    continue
                if (
                    limit is not None
                    and totals["processed"] + totals["errors"] >= limit
                ):
                    break
                prompt = build_user_prompt(
                    document.slug,
                    document.manifest,
                    infer_year_context(document, settings.year_range),
                    chunk,
                    index,
                    len(chunks),
                )
                totals["estimated_input_tokens"] += (
                    len(prompt) + len(settings.prompts["extract"]) + 3
                ) // 4
                totals["maximum_output_tokens"] += settings.budget["max_tokens"]
                if dry_run:
                    totals["processed"] += 1
                    continue
                try:
                    text, usage = call_llm(
                        settings,
                        client,
                        settings.prompts["extract"],
                        prompt,
                        settings.budget["max_tokens"],
                    )
                    facts = parse_response(text)
                    value = {
                        "doc_slug": document.slug,
                        "doc_id": document.doc_id,
                        "chunk_id": chunk["chunk_id"],
                        "heading": chunk["heading"],
                        "chunk_index": index,
                        "chunk_total": len(chunks),
                        "input_sha1": chunk["sha1"],
                        "char_count": len(chunk["body"]),
                        "extracted_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "model": settings.llm["model"],
                        "extraction_signature": self.chunk_signature(
                            document, chunk, index
                        ),
                        **facts,
                    }
                    write_text(
                        self.chunk_path(document, chunk),
                        yaml.safe_dump(
                            value, allow_unicode=True, sort_keys=False, width=100
                        ),
                    )
                    state.setdefault(document.slug, {})[chunk["chunk_id"]] = {
                        "sha1": chunk["sha1"],
                        "extracted_at": value["extracted_at"],
                    }
                    write_text(
                        confined(
                            settings.root,
                            settings.state_file.relative_to(settings.root),
                        ),
                        json.dumps(state, indent=2, sort_keys=True) + "\n",
                    )
                    totals["processed"] += 1
                    totals["input_tokens"] += usage.get("input_tokens", 0)
                    totals["output_tokens"] += usage.get("output_tokens", 0)
                except ExtractionError as error:
                    print(f"ERROR: chunk {index + 1}: {error}", file=sys.stderr)
                    totals["errors"] += 1
            if not dry_run and not totals["errors"]:
                completed = self.read_chunks(document)
                if len(completed) == len(chunks):
                    self.digest(document, completed)
        return totals

    def digest(self, document, chunks):
        path = confined(
            self.settings.output_directory, Path(document.slug) / "README.md"
        )
        content = render_digest(self.settings, document, chunks, path)
        return bool(content) and write_text(path, content)

    def reports(self, timeline=False, dry_run=False):
        # Validate the entire selected set before overwriting any report.
        chunks = {doc.slug: self.read_snapshot(doc) for doc in self.documents}
        if dry_run:
            return {
                "documents": len(chunks),
                "chunks": sum(map(len, chunks.values())),
                "changed": 0,
            }
        if timeline:
            path = confined(
                self.settings.root,
                self.settings.timeline_file.relative_to(self.settings.root),
            )
            return {
                "changed": int(
                    write_text(
                        path, render_timeline(self.settings, self.documents, chunks)
                    )
                )
            }
        return {
            "changed": sum(self.digest(doc, chunks[doc.slug]) for doc in self.documents)
        }

    def thread_chunks(self, theme):
        selected = self.documents
        if theme.get("include_slugs"):
            selected = []
            for selector in theme["include_slugs"]:
                found = [
                    doc
                    for doc in self.documents
                    if selector
                    in {doc.slug, doc.source_slug, doc.doc_id, *doc.previous_slugs}
                    or doc.doc_id.startswith(selector)
                ]
                if len(found) != 1:
                    raise ExtractionError(
                        "thread includes an unknown or ambiguous document"
                    )
                if found[0] not in selected:
                    selected.append(found[0])
        return [
            chunk
            for doc in selected
            for chunk in self.read_snapshot(doc)
            if chunk_matches_theme(chunk_text_for_match(chunk), theme)
        ]

    def thread_prompt(self, theme, chunks, page):
        settings = self.settings
        parts = [
            f"Theme: {theme['title']}",
            f"What this theme is about:\n{theme['what_it_covers']}",
            "Full matching source-chunk list (a listing does not mean the content was included):",
        ]
        for chunk in chunks:
            target = (
                settings.output_directory
                / chunk["doc_slug"]
                / (chunk["chunk_id"] + ".yaml")
            )
            parts.append(
                f"- [{chunk['doc_slug']}/{chunk['chunk_id']}]({relative_link(target, page)})"
            )
        parts.append("Compacted evidence follows. Identify omitted evidence as unread.")
        # Preserve the original budget: it caps compacted evidence, not the
        # complete source listing and caller-supplied theme description.
        used = 0
        included = 0
        for chunk in chunks:
            compacted = compact_chunk_for_theme(
                chunk, theme, settings.budget["thread_chunk_chars"]
            ).get("_rendered")
            if not compacted:
                continue
            if used + len(compacted) > settings.budget["thread_prompt_chars"]:
                parts.append(
                    "Remaining matching chunk contents omitted by configured prompt budget."
                )
                break
            parts.append(compacted)
            used += len(compacted) + 1
            included += 1
        if not included:
            raise ExtractionError(
                "thread has no substantive evidence within the configured budget"
            )
        return "\n".join(parts), included

    def threads(self, client=None, force=False, dry_run=False, only=None):
        settings = self.settings
        themes = [
            theme for theme in settings.threads if not only or only in theme["slug"]
        ]
        if not themes:
            raise ExtractionError("no configured threads matched")
        totals = {
            "wrote": 0,
            "skipped": 0,
            "no_chunks": 0,
            "errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_input_tokens": 0,
            "maximum_output_tokens": 0,
        }
        for theme in themes:
            chunks = self.thread_chunks(theme)
            if not chunks:
                totals["no_chunks"] += 1
                continue
            page = confined(settings.threads_directory, theme["slug"] + ".md")
            prompt, included = self.thread_prompt(theme, chunks, page)
            digest = signature(
                theme,
                chunks,
                settings.prompts["thread"],
                settings.llm.get("model"),
                settings.budget,
            )
            if page.exists() and not force:
                text = page.read_text(encoding="utf-8")
                if text.startswith("---\n"):
                    try:
                        old = yaml.safe_load(text.split("---", 2)[1])
                    except yaml.YAMLError:
                        old = None
                    if isinstance(old, dict) and old.get("input_sha256") == digest:
                        totals["skipped"] += 1
                        continue
            totals["estimated_input_tokens"] += (
                len(prompt) + len(settings.prompts["thread"]) + 3
            ) // 4
            totals["maximum_output_tokens"] += settings.budget["thread_max_tokens"]
            if dry_run:
                totals["wrote"] += 1
                continue
            try:
                text, usage = call_llm(
                    settings,
                    client,
                    settings.prompts["thread"],
                    prompt,
                    settings.budget["thread_max_tokens"],
                )
                text = re.sub(r"\A```(?:markdown)?\s*|\s*```\Z", "", text.strip())
                if text.startswith("---\n"):
                    sections = text.split("---", 2)
                    if len(sections) != 3:
                        raise ExtractionError("thread output has invalid frontmatter")
                    text = sections[2].lstrip()
                if not text.startswith("# "):
                    raise ExtractionError("thread output requires a Markdown title")
                sources = [
                    str(
                        (
                            settings.output_directory
                            / c["doc_slug"]
                            / (c["chunk_id"] + ".yaml")
                        ).relative_to(settings.root)
                    )
                    for c in chunks
                ]
                metadata = frontmatter(
                    settings,
                    theme["title"],
                    "thematic-thread",
                    sources,
                    page,
                    sources_chunks=len(chunks),
                    included_chunks=included,
                    input_sha256=digest,
                )
                write_text(page, metadata + text.rstrip() + "\n")
                totals["wrote"] += 1
                totals["input_tokens"] += usage.get("input_tokens", 0)
                totals["output_tokens"] += usage.get("output_tokens", 0)
            except ExtractionError as error:
                print(f"ERROR: thread: {error}", file=sys.stderr)
                totals["errors"] += 1
        if not dry_run and not totals["errors"]:
            present = {
                t["slug"]
                for t in settings.threads
                if confined(settings.threads_directory, t["slug"] + ".md").is_file()
            }
            write_text(
                confined(settings.threads_directory, "README.md"),
                render_thread_index(settings, present),
            )
        return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", help="Remap repository paths into a worktree")
    parser.add_argument(
        "--only", help="Filter configured sources by output slug or document ID"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only estimate; no credentials, writes, or provider calls",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Validate configuration and local source selection without API calls",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--digests-only", action="store_true")
    modes.add_argument("--build-timeline", action="store_true")
    modes.add_argument("--build-threads", action="store_true")
    parser.add_argument("--only-thread")
    parser.add_argument("--limit-chunks", type=int)
    args = parser.parse_args(argv)
    client = None
    try:
        if args.limit_chunks is not None and args.limit_chunks < 1:
            raise ExtractionError("--limit-chunks must be positive")
        if args.only_thread and not args.build_threads:
            raise ExtractionError("--only-thread requires --build-threads")
        settings = load_config(args.config, args.root)
        snapshot_mode = args.digests_only or args.build_timeline or args.build_threads
        documents = load_documents(
            settings, read_content=not snapshot_mode or args.doctor
        )
        if args.only:
            documents = [
                d
                for d in documents
                if args.only in d.slug
                or args.only in d.doc_id
                or args.only in d.source_slug
            ]
            if not documents:
                raise ExtractionError("--only matched no configured source")
        engine = Extractor(settings, documents)
        if args.doctor:
            print(
                json.dumps(
                    {
                        "documents": len(documents),
                        "chunks": sum(map(len, engine.chunks.values())),
                        "offline": True,
                    }
                )
            )
            return 0
        if not (args.dry_run or args.digests_only or args.build_timeline):
            if args.build_threads:
                pending = engine.threads(
                    force=args.force, dry_run=True, only=args.only_thread
                )["wrote"]
            else:
                pending = engine.extraction(
                    force=args.force, dry_run=True, limit=args.limit_chunks
                )["processed"]
            if pending:
                client = make_client(settings)
                if client is None:
                    print(
                        "SKIP: explicitly optional credential is unavailable",
                        file=sys.stderr,
                    )
                    return 0
        if args.digests_only or args.build_timeline:
            result = engine.reports(timeline=args.build_timeline, dry_run=args.dry_run)
        elif args.build_threads:
            result = engine.threads(client, args.force, args.dry_run, args.only_thread)
        else:
            result = engine.extraction(
                client, args.force, args.dry_run, args.limit_chunks
            )
        if settings.cost and "estimated_input_tokens" in result:
            result["estimated_cost"] = (
                result["estimated_input_tokens"]
                * settings.cost.get("input_per_million", 0)
                + result["maximum_output_tokens"]
                * settings.cost.get("output_per_million", 0)
            ) / 1_000_000
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("errors") else 0
    except ExtractionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        print("ERROR: invalid local input or failed file operation", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
