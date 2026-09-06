"""Verify weekly provenance, source identities and configured document structure."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import DEFAULT_CONFIG, load
from .validation import parse_frontmatter

IDENTITY_RE = re.compile(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?#[0-9]+\b")
URL_RE = re.compile(r"https://github\.com/[^\s)\"'>]+")


def validate(path: Path, end: str, input_hash: str, inputs: str, missing: list[str], options=None):
    options = options or {}
    errors = []
    if path.is_symlink() or not path.is_file():
        return ["summary must be a regular file"]
    text = path.read_text(encoding="utf-8")
    try:
        fields = parse_frontmatter(text)
        end_date = date.fromisoformat(end)
    except ValueError:
        return ["invalid frontmatter or end date"]
    start = str(end_date - timedelta(days=6))
    substitutions = {"start": start, "end": end, "target": end, "input_hash": input_hash}
    expected = {
        "title": f"Weekly summary {start}..{end}",
        "type": "summary",
        "generator": "weekly-summary",
        "week": f"{start}..{end}",
        "inputs_sha256": input_hash,
    }
    expected.update(
        {
            key: value.format(**substitutions)
            for key, value in options.get("frontmatter", {}).items()
        }
    )
    for key, value in expected.items():
        if fields.get(key) != value:
            errors.append(f"incorrect frontmatter field: {key}")
    for key in ("created", "updated", "sources", "missing_inputs"):
        if key not in fields:
            errors.append(f"missing frontmatter field: {key}")
    # Source selection is part of the provenance, not model discretion.
    actual_missing = [
        item.strip().strip("\"'")
        for item in fields.get("missing_inputs", "").strip("[]").split(",")
        if item.strip()
    ]
    if actual_missing != missing:
        errors.append("incorrect missing input dates")
    if len(text) < options.get("min_chars", 0):
        errors.append("summary is shorter than the configured minimum")
    body = text[text.find("\n---\n", 4) + 5 :]
    headings = options.get("required_headings", ["## Headlines", "## Projects", "## Commentary"])
    positions = []
    for heading in headings:
        match = re.search("^" + re.escape(heading) + r"\s*$", body, re.MULTILINE)
        if match is None:
            errors.append("required heading missing")
        else:
            positions.append(match.start())
    if positions != sorted(positions):
        errors.append("required headings out of order")
    for identity in set(IDENTITY_RE.findall(body)):
        if identity not in inputs:
            errors.append("issue/PR identity absent from inputs")
    for url in set(URL_RE.findall(body)):
        if url.rstrip(".,;:)") not in inputs:
            errors.append("GitHub URL absent from inputs")
    heading = options.get("commentary_heading", "## Commentary")
    commentary = re.search(
        "^" + re.escape(heading) + r"\s*$\n(.*?)(?=^##\s|\Z)", body, re.MULTILINE | re.DOTALL
    )
    if commentary and (IDENTITY_RE.search(commentary[1]) or "github.com" in commentary[1]):
        errors.append("commentary contains an issue/PR reference")
    if any(value not in body for value in missing):
        errors.append("missing input dates not acknowledged")
    if missing and options.get("missing_label", "Missing inputs") not in body:
        errors.append("missing input declaration absent")
    return errors


def sanitize(markdown: str, options: dict) -> str:
    heading = options.get("commentary_heading", "## Commentary")
    match = re.search("^" + re.escape(heading) + r"\s*$", markdown, re.MULTILINE)
    if not match:
        return markdown
    tail = markdown[match.start() :]
    replacement = options.get("commentary_replacement", "related work")
    tail = re.sub(r"\[[^\]]*\]\(https://github\.com/[^)]+\)", replacement, tail)
    tail = re.sub(r"https://github\.com/[^\s)\"'>]+", replacement, tail)
    tail = re.sub(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?#[0-9]+\b", replacement, tail)
    return markdown[: match.start()] + tail


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("end")
    parser.add_argument("input_hash")
    parser.add_argument("inputs", type=Path)
    parser.add_argument("missing_csv")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    cfg = load(args.config)
    errors = validate(
        args.candidate,
        args.end,
        args.input_hash,
        args.inputs.read_text(encoding="utf-8"),
        [item for item in args.missing_csv.split(",") if item],
        cfg["weekly"].get("validation", {}),
    )
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
