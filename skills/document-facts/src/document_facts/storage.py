"""Source identity, safe writes, and committed extraction checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .config import ExtractionError, confined, name

FIELDS = {
    "dates_found",
    "tasks",
    "decisions",
    "concepts",
    "blockers_solutions",
    "notable_quotes",
    "people",
    "references",
}
STRINGS = {"dates_found", "notable_quotes", "people", "references"}
OBJECT_FIELDS = {
    "tasks": (
        {"task", "status", "solution"},
        {"subtasks", "blockers", "files_touched", "related_to"},
    ),
    "decisions": ({"decision", "rationale", "alternative_rejected"}, set()),
    "concepts": ({"term", "definition"}, {"aliases"}),
    "blockers_solutions": ({"blocker", "solution"}, set()),
}


def validate_facts(value: object) -> dict:
    if not isinstance(value, dict) or not FIELDS <= value.keys():
        raise ExtractionError("extracted JSON is missing required fields")
    facts = {key: value[key] for key in FIELDS}
    for key, entries in facts.items():
        if not isinstance(entries, list):
            raise ExtractionError("extracted fields must be arrays")
        if key in STRINGS:
            if any(not isinstance(item, str) for item in entries):
                raise ExtractionError("extracted text fields must contain strings")
            continue
        scalar, arrays = OBJECT_FIELDS[key]
        for item in entries:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(k), str) for k in scalar
            ):
                raise ExtractionError("extracted fact has invalid fields")
            if any(
                not isinstance(item.get(k), list)
                or any(not isinstance(v, str) for v in item[k])
                for k in arrays
            ):
                raise ExtractionError("extracted fact has invalid list fields")
            if key == "tasks" and item["status"] not in {
                "shipped",
                "in_progress",
                "deferred",
                "abandoned",
                "investigated",
                "blocked",
            }:
                raise ExtractionError("extracted task has invalid status")
    for item in facts["dates_found"]:
        try:
            if date.fromisoformat(item).isoformat() != item:
                raise ValueError
        except ValueError:
            raise ExtractionError(
                "extracted dates must be valid YYYY-MM-DD dates"
            ) from None
    return facts


def parse_response(text: str) -> dict:
    text = re.sub(r"\A```(?:json)?\s*|\s*```\Z", "", text.strip())
    try:
        value = json.loads(text)
    except ValueError:
        from json_repair import repair_json

        try:
            value = json.loads(repair_json(text))
        except ValueError:
            raise ExtractionError("provider returned invalid JSON") from None
    if not isinstance(value, dict) or value.keys() != FIELDS:
        raise ExtractionError("provider JSON does not match extraction schema")
    return validate_facts(value)


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        raise ExtractionError("cannot read source or extraction YAML") from None
    if not isinstance(data, dict):
        raise ExtractionError("expected YAML object")
    return data


def write_text(path: Path, text: str) -> bool:
    if path.is_symlink():
        raise ExtractionError("refusing to replace a symbolic link")
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".document-facts-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


@dataclass(frozen=True)
class Document:
    slug: str
    source_slug: str
    doc_id: str
    manifest: dict
    content: str
    previous_slugs: tuple[str, ...] = ()


def load_documents(settings, *, read_content=True) -> list[Document]:
    if not settings.source_directory.is_dir():
        raise ExtractionError("source_directory does not exist")
    candidates = []
    for path in sorted(settings.source_directory.iterdir()):
        if path.is_symlink() or not path.is_dir():
            continue
        manifest_path = confined(
            settings.source_directory, Path(path.name) / "manifest.yaml"
        )
        if manifest_path.exists():
            manifest = read_yaml(manifest_path)
            candidates.append((path, manifest))
    documents, used, outputs = [], set(), set()
    for selection in settings.documents:
        identifier = selection.get("id")
        matches = (
            [(p, m) for p, m in candidates if m.get("docId") == identifier]
            if identifier
            else []
        )
        if not identifier:
            slug = selection["slug"]
            matches = [(p, m) for p, m in candidates if p.name == slug]
            if not matches:
                # Legacy id-prefix--title selectors remain usable after the
                # mirror changes directory naming or the document is renamed.
                short_id = slug.split("--")[0]
                matches = [
                    (p, m)
                    for p, m in candidates
                    if str(m.get("docId", "")).startswith(short_id)
                ]
        if len(matches) != 1:
            raise ExtractionError("selected document is missing or ambiguous")
        path, manifest = matches[0]
        doc_id = manifest.get("docId")
        if not isinstance(doc_id, str) or not doc_id:
            raise ExtractionError("selected manifest requires docId")
        out_slug = name(
            selection.get("output_slug") or selection.get("slug") or doc_id,
            "output slug",
        )
        if doc_id in used or out_slug in outputs:
            raise ExtractionError("duplicate document or extraction output")
        used.add(doc_id)
        outputs.add(out_slug)
        if not isinstance(manifest.get("title", ""), str) or not isinstance(
            manifest.get("sourceUrl", ""), str
        ):
            raise ExtractionError("manifest title and sourceUrl must be text")
        if not read_content:
            content = ""
        elif manifest.get("layout") == "tabs":
            tabs = manifest.get("tabs")
            if not isinstance(tabs, list) or not tabs:
                raise ExtractionError("tabbed manifest requires tabs")
            parts, paths = [], set()
            for tab in tabs:
                if not isinstance(tab, dict) or not isinstance(tab.get("path"), str):
                    raise ExtractionError("tab requires a path")
                tab_path = confined(path, tab["path"])
                if tab_path in paths or not tab_path.is_file():
                    raise ExtractionError("duplicate or missing tab file")
                paths.add(tab_path)
                parts.append(
                    re.sub(
                        r"\A<!--.*?-->\s*",
                        "",
                        tab_path.read_text(encoding="utf-8"),
                        flags=re.DOTALL,
                    )
                )
            content = "\n".join(parts)
        elif manifest.get("layout") in {None, "single"}:
            try:
                content = confined(path, "README.md").read_text(encoding="utf-8")
            except OSError:
                raise ExtractionError("selected document content is missing") from None
        else:
            raise ExtractionError("unsupported document layout")
        documents.append(
            Document(
                out_slug,
                path.name,
                doc_id,
                manifest,
                content,
                tuple(selection.get("previous_slugs", [])),
            )
        )
    return documents


def infer_year_context(document, year_range) -> str:
    title = document.manifest.get("title") or document.slug
    hints = [
        int(m.group(1)) for m in re.finditer(r"(20\d{2})(?:\d{2})(?:\d{2})?", title)
    ]
    hints += [int(v) for v in re.findall(r"\b(20\d{2})\b", title)]
    hints += [
        int(v)
        for v in re.findall(
            r"\b(20\d{2})(?:[-/.]\d{1,2}[-/.]\d{1,2}|年)", document.content
        )
    ]
    if year_range:
        hints = [v for v in hints if year_range[0] <= v <= year_range[1]]
    if not hints:
        return (
            f"{year_range[0]}–{year_range[1]} (configured corpus window)"
            if year_range
            else "unknown; omit ambiguous dates"
        )
    return str(min(hints)) if min(hints) == max(hints) else f"{min(hints)}–{max(hints)}"


def signature(*values) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
