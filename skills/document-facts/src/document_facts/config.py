"""Private paths and extraction choices supplied by the caller."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ExtractionError(Exception):
    """A diagnostic safe to print without provider responses or credentials."""


def name(value: object, label: str = "name") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(c in value for c in "/\\\x00\r\n")
    ):
        raise ExtractionError(f"invalid {label}")
    return value


def expanded(value: str, base: Path) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ExtractionError("invalid configured path")
    result = os.path.expandvars(os.path.expanduser(value))
    if "$" in result:
        raise ExtractionError("unresolved variable in configured path")
    path = Path(result)
    return path if path.is_absolute() else base / path


def confined(base: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExtractionError("path must remain within its configured directory")
    base = base.resolve()
    target = base / relative
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ExtractionError("symbolic links are not allowed in data paths")
    if not target.resolve().is_relative_to(base):
        raise ExtractionError("path leaves its configured directory")
    return target


DEFAULT_EXTRACT_PROMPT = """Extract structured facts from the supplied document chunk.
Return one JSON object with arrays named dates_found, tasks, decisions, concepts,
blockers_solutions, notable_quotes, people, references. Do not add other fields.
tasks contain task, status, subtasks, blockers, solution, files_touched, related_to.
The status is shipped, in_progress, deferred, abandoned, investigated, or blocked.
decisions contain decision, rationale, alternative_rejected. concepts contain
term, definition, aliases. blockers_solutions contain blocker, solution.
dates_found uses YYYY-MM-DD. Resolve partial dates only from supplied document
year context; omit ambiguous and example/template dates. Keep original language.
Quote only actual source text. Include people doing the work, not names merely
mentioned as example content. Distinguish confirmed completion from proposals.
Do not invent activity from image links, page numbers, headings, or empty text.
Return empty arrays when evidence is absent. Output JSON only.
"""

DEFAULT_THREAD_PROMPT = """Synthesize a thematic thread using only the supplied
extracted facts. Describe earliest evidence, evolution, decisions, blockers and
resolutions, and the state at the latest evidence date. Preserve quoted language.
Identify uncertainty. Cite the supplied chunk identifiers and relative links.
Do not claim omitted chunks were read. Include a Source chunks section covering
the supplied evidence. Output a Markdown document, without a surrounding fence.
"""


@dataclass(frozen=True)
class Settings:
    root: Path
    source_directory: Path
    output_directory: Path
    state_file: Path
    timeline_file: Path
    threads_directory: Path
    documents: tuple[dict, ...]
    year_range: tuple[int, int] | None
    llm: dict
    prompts: dict
    threads: tuple[dict, ...]
    metadata: dict
    budget: dict
    cost: dict
    protected_paths: tuple[Path, ...]


def load_config(path: str | Path, root: str | Path | None = None) -> Settings:
    config_path = Path(path).expanduser().resolve()
    protected_paths = [config_path]
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExtractionError("cannot read configuration JSON") from None
    if not isinstance(data, dict) or data.get("schema") != "document-facts/v1":
        raise ExtractionError("configuration requires schema document-facts/v1")
    original_root = expanded(
        data.get("repository_root", "."), config_path.parent
    ).resolve()
    actual_root = (
        expanded(str(root), config_path.parent).resolve() if root else original_root
    )

    def repo_path(key: str, default: str | None = None) -> Path:
        value = data.get(key, default)
        if value is None:
            raise ExtractionError(f"configuration requires {key}")
        absolute = expanded(value, original_root)
        try:
            relative = absolute.relative_to(original_root)
        except ValueError:
            raise ExtractionError(f"{key} must be inside repository_root") from None
        return confined(actual_root, relative)

    sources = repo_path("source_directory")
    output = repo_path("output_directory")
    if output.is_relative_to(sources) or sources.is_relative_to(output):
        raise ExtractionError("source and output directories must be separate")
    output_rel = output.relative_to(actual_root)
    state = repo_path("state_file", str(output_rel / ".extract_state.json"))
    timeline = repo_path("timeline_file", str(output_rel.parent / "timeline.md"))
    threads_dir = repo_path("threads_directory", str(output_rel.parent / "threads"))
    for destination in (state, timeline, threads_dir):
        if destination.is_relative_to(sources):
            raise ExtractionError(
                "generated output cannot be placed in source_directory"
            )
    documents = data.get("documents", data.get("target_slugs"))
    if not isinstance(documents, list) or not documents:
        raise ExtractionError("documents must be a nonempty explicit selection")
    selections = []
    for item in documents:
        item = {"slug": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or not (item.get("id") or item.get("slug")):
            raise ExtractionError("each document requires id or slug")
        selection = {
            k: name(v, f"document {k}")
            for k, v in item.items()
            if k in {"id", "slug", "output_slug"}
        }
        previous = item.get("previous_slugs", [])
        if not isinstance(previous, list):
            raise ExtractionError("document previous_slugs must be an array")
        selection["previous_slugs"] = [
            name(value, "previous document slug") for value in previous
        ]
        selections.append(selection)
    year_range = data.get("year_range")
    if year_range is not None and (
        not isinstance(year_range, list)
        or len(year_range) != 2
        or any(type(y) is not int or not 1 <= y <= 9999 for y in year_range)
        or year_range[0] > year_range[1]
    ):
        raise ExtractionError("year_range requires ordered start and end years")
    llm = data.get("llm", {})
    if not isinstance(llm, dict):
        raise ExtractionError("llm must be an object")
    llm = dict(llm)
    for field in ("model", "base_url", "api_key_env", "credential_key"):
        if field in llm and (not isinstance(llm[field], str) or not llm[field].strip()):
            raise ExtractionError(f"llm.{field} must be nonempty text")
    if "base_url" in llm:
        try:
            url = urlsplit(llm["base_url"])
            url.port
        except ValueError:
            raise ExtractionError("llm.base_url is invalid") from None
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ExtractionError(
                "llm.base_url must be an HTTPS URL without credentials or query"
            )
    if "api_key_env" in llm and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", llm["api_key_env"]
    ):
        raise ExtractionError("invalid api_key_env")
    if "credential_file" in llm:
        llm["credential_file"] = expanded(llm["credential_file"], config_path.parent)
        protected_paths.append(llm["credential_file"].resolve())
    for field, default in (("timeout_seconds", 120), ("max_attempts", 3)):
        value = llm.get(field, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ExtractionError(f"llm.{field} must be positive")
        llm[field] = value
    if type(llm["max_attempts"]) is not int or llm["max_attempts"] > 10:
        raise ExtractionError("llm.max_attempts must be an integer from 1 to 10")
    if type(llm.get("required", True)) is not bool:
        raise ExtractionError("llm.required must be a boolean")
    prompts = data.get("prompts", {})
    if not isinstance(prompts, dict):
        raise ExtractionError("prompts must be an object")
    prompts = dict(prompts)
    for key, default in (
        ("extract", DEFAULT_EXTRACT_PROMPT),
        ("thread", DEFAULT_THREAD_PROMPT),
    ):
        if key in prompts and key + "_file" in prompts:
            raise ExtractionError(f"choose {key} text or file, not both")
        if key + "_file" in prompts:
            try:
                prompt_path = expanded(
                    prompts[key + "_file"], config_path.parent
                ).resolve()
                protected_paths.append(prompt_path)
                prompts[key] = prompt_path.read_text(encoding="utf-8")
            except OSError:
                raise ExtractionError("cannot read configured prompt file") from None
        prompts.setdefault(key, default)
        if not isinstance(prompts[key], str) or not prompts[key].strip():
            raise ExtractionError("prompts must contain nonempty text")
    budget = {
        "max_chunk_chars": 16000,
        "soft_split_chars": 12000,
        "max_tokens": 8000,
        "thread_max_tokens": 5000,
        "thread_prompt_chars": 30000,
        "thread_chunk_chars": 1800,
    }
    if not isinstance(data.get("budget", {}), dict):
        raise ExtractionError("budget must be an object")
    budget.update(data.get("budget", {}))
    if (
        any(type(v) is not int or v < 1 for v in budget.values())
        or budget["soft_split_chars"] > budget["max_chunk_chars"]
    ):
        raise ExtractionError(
            "budget values must be positive with soft_split_chars <= max_chunk_chars"
        )
    themes = data.get("threads", [])
    if not isinstance(themes, list):
        raise ExtractionError("threads must be an array")
    seen = set()
    for theme in themes:
        if not isinstance(theme, dict):
            raise ExtractionError("thread must be an object")
        slug = name(theme.get("slug"), "thread slug")
        if slug == "README" or slug in seen:
            raise ExtractionError("duplicate or reserved thread slug")
        seen.add(slug)
        if any(
            not isinstance(theme.get(k), str) or not theme[k].strip()
            for k in ("title", "what_it_covers")
        ):
            raise ExtractionError("thread requires title and what_it_covers")
        for key in ("search_terms", "exclude_terms", "include_slugs"):
            values = theme.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v for v in values
            ):
                raise ExtractionError(f"thread {key} must be an array of strings")
    metadata = data.get("metadata", {})
    cost = data.get("cost", {})
    if not isinstance(metadata, dict) or not isinstance(cost, dict):
        raise ExtractionError("metadata and cost must be objects")
    for field in ("timeline_title", "threads_title", "generator_label"):
        if field in metadata and (
            not isinstance(metadata[field], str) or not metadata[field].strip()
        ):
            raise ExtractionError(f"metadata.{field} must be nonempty text")
    if any(
        isinstance(v, bool) or not isinstance(v, (float, int)) or v < 0
        for v in cost.values()
    ):
        raise ExtractionError("cost values must be nonnegative")
    return Settings(
        actual_root,
        sources,
        output,
        state,
        timeline,
        threads_dir,
        tuple(selections),
        tuple(year_range) if year_range else None,
        llm,
        prompts,
        tuple(themes),
        metadata,
        budget,
        cost,
        tuple(protected_paths),
    )
