"""Derive a document-authority registry from caller-selected source records."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from .config import atomic_write, default_config, load

OUT_REPO = REGISTRY_PATH = MIRROR_DIR = None
SOURCE_LISTS = {}
FRONTMATTER_SCAN_DIRS = []
DOC_URL = "https://docs.google.com/document/d/{doc_id}"
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
VALID_MODES = {"mirror", "published", "handed-off"}
AUTHORITY = {"published": "vault", "handed-off": "google", "mirror": "google"}


def configure(settings):
    global OUT_REPO, REGISTRY_PATH, MIRROR_DIR, SOURCE_LISTS, FRONTMATTER_SCAN_DIRS
    registry = settings.get("registry")
    if registry is None:
        raise ValueError("registry-configuration-required")
    OUT_REPO = registry["repository_root"]
    REGISTRY_PATH = registry["output"]
    MIRROR_DIR = registry.get("mirror_directory")
    SOURCE_LISTS = registry["source_lists"]
    FRONTMATTER_SCAN_DIRS = registry["source_directories"]


def frontmatter_entries() -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    failures: list[str] = []
    for base in FRONTMATTER_SCAN_DIRS:
        root = OUT_REPO / base
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError as e:
                failures.append(f"unreadable {path}: {e}")
                continue
            m = FRONTMATTER_RE.match(head)
            if not m or "\ngdoc:" not in m.group(0):
                continue
            rel = str(path.relative_to(OUT_REPO))
            try:
                fm = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                failures.append(f"unparseable frontmatter in {rel}: {e}")
                continue
            gdoc = fm.get("gdoc") or {}
            doc_id = str(gdoc.get("id") or "").strip()
            mode = str(gdoc.get("mode") or "").strip()
            if not doc_id:
                failures.append(f"{rel}: gdoc block without id")
                continue
            if mode not in VALID_MODES:
                failures.append(
                    f"{rel}: gdoc mode {mode!r} not one of {sorted(VALID_MODES)}"
                )
                continue
            fingerprint = gdoc.get("fingerprint")
            published_at = gdoc.get("published_at")
            if fingerprint is not None and (not isinstance(fingerprint, str)):
                failures.append(f"{rel}: gdoc fingerprint must be a string or null")
                continue
            if mode == "published" and (not fingerprint):
                failures.append(f"{rel}: published gdoc block requires a fingerprint")
                continue
            if published_at is not None and (not isinstance(published_at, str)):
                failures.append(
                    f"{rel}: gdoc published_at must be a quoted string or null"
                )
                continue
            entries.append(
                {
                    "docId": doc_id,
                    "url": DOC_URL.format(doc_id=doc_id),
                    "mode": mode,
                    "authority": AUTHORITY[mode],
                    "path": rel,
                    "title": fm.get("title") or path.stem,
                    "fingerprint": fingerprint,
                    "published_at": published_at,
                }
            )
    return (entries, failures)


def mirror_entries() -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    warnings: list[str] = []
    config: dict[str, str] = {}
    for source_name, yaml_path in SOURCE_LISTS.items():
        if not yaml_path.exists():
            continue
        for doc in (yaml.safe_load(yaml_path.read_text()) or {}).get("docs") or []:
            if doc.get("id"):
                config[doc["id"]] = source_name
    manifests: dict[str, Path] = {}
    if MIRROR_DIR is not None and MIRROR_DIR.is_dir():
        for manifest in sorted(MIRROR_DIR.glob("*/manifest.yaml")):
            try:
                data = yaml.safe_load(manifest.read_text()) or {}
            except yaml.YAMLError:
                raise ValueError("mirror-manifest-invalid") from None
            doc_id = data.get("docId")
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("mirror-document-id-invalid")
            manifests[doc_id] = manifest
            entries.append(
                {
                    "docId": doc_id,
                    "url": DOC_URL.format(doc_id=doc_id),
                    "mode": "mirror",
                    "authority": "google",
                    "path": str(manifest.parent.relative_to(OUT_REPO) / "README.md"),
                    "title": data.get("title"),
                    "config": config.get(doc_id, "unregistered"),
                }
            )
    for doc_id, source_name in config.items():
        if doc_id not in manifests:
            warnings.append(
                f"configured ({source_name}) but not yet mirrored: {doc_id}"
            )
    return (entries, warnings)


def build() -> tuple[dict, list[str], list[str]]:
    fm_entries, failures = frontmatter_entries()
    mi_entries, warnings = mirror_entries()
    by_id: dict[str, list[dict]] = {}
    for e in fm_entries + mi_entries:
        by_id.setdefault(e["docId"], []).append(e)
    for doc_id, group in sorted(by_id.items()):
        published = [e for e in group if e["mode"] == "published"]
        if len(published) > 1:
            failures.append(
                f"doc {doc_id} claimed as published by more than one file: "
                + ", ".join((e["path"] for e in published))
            )
        if published and any((e["mode"] == "mirror" for e in group)):
            failures.append(
                f"doc {doc_id} is both published ({published[0]['path']}) and mirrored — publish + mirror of one doc means split authority; flip the file to handed-off or drop the mirror"
            )
        handed = [e for e in group if e["mode"] == "handed-off"]
        if handed and (not any((e["mode"] == "mirror" for e in group))):
            warnings.append(
                f"doc {doc_id} is handed-off ({handed[0]['path']}) but has no mirror yet"
            )
    registry = {
        "_generated_by": "google-docs-authority registry; derived view; do not hand-edit",
        "entries": sorted(
            (e for g in by_id.values() for e in g),
            key=lambda e: (e["docId"], e["path"]),
        ),
    }
    return (registry, failures, warnings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=default_config())
    ap.add_argument(
        "--root", type=Path, help="override only the registry repository root"
    )
    action = ap.add_mutually_exclusive_group()
    action.add_argument(
        "--check",
        action="store_true",
        help="validate and require the checked-in registry to be current",
    )
    action.add_argument(
        "--write",
        action="store_true",
        help="write the registry (also the default when no action is given)",
    )
    args = ap.parse_args(argv)
    try:
        configure(load(args.config, args.root))
        registry, failures, warnings = build()
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        print("FAIL registry-input-unreadable-or-invalid", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"WARN {w}", file=sys.stderr)
    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    if failures:
        return 1
    text = json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if not args.check:
        if REGISTRY_PATH.exists() and REGISTRY_PATH.read_text(encoding="utf-8") == text:
            print(f"OK registry unchanged ({len(registry['entries'])} entries)")
        else:
            REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(REGISTRY_PATH, text)
            print(
                f"OK registry written: {REGISTRY_PATH} ({len(registry['entries'])} entries)"
            )
    else:
        if not REGISTRY_PATH.exists():
            print(f"FAIL registry is missing: {REGISTRY_PATH}", file=sys.stderr)
            return 1
        if REGISTRY_PATH.read_text(encoding="utf-8") != text:
            print(
                "FAIL registry is stale; run google-docs-authority registry --write",
                file=sys.stderr,
            )
            return 1
        print(f"OK checked {len(registry['entries'])} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
