from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .issue_refs import (
    allowed_ref_tokens,
    canonical,
    explicit_ref_tokens,
    required_refs,
    split_ref,
    summary_link_refs,
)

OPTIONS = {}
REQUIRED_HEADINGS = ("## Facts", "### Agent work", "## Projects", "## Commentary", "## Next")
AGENT_WORK_PATTERN = r"^### Agent work[^\n]*\n(.*?)(?=^#{2,3}(?!#)[ \t]+|\Z)"


def configure(options, section=None):
    global OPTIONS, REQUIRED_HEADINGS, AGENT_WORK_PATTERN, ISSUE_SECTION_RE
    OPTIONS = options
    REQUIRED_HEADINGS = options.get(
        "required_headings",
        ("## Facts", "### Agent work", "## Projects", "## Commentary", "## Next"),
    )
    AGENT_WORK_PATTERN = options.get(
        "agent_work_pattern", r"^### Agent work[^\n]*\n(.*?)(?=^#{2,3}(?!#)[ \t]+|\Z)"
    )
    heading = (section or {}).get("heading", "### PRs / Issues")
    ISSUE_SECTION_RE = re.compile(
        "^" + re.escape(heading) + r"\s*$\n.*?(?=^###\s|^##\s|\Z)", re.MULTILINE | re.DOTALL
    )


MARKDOWN_GITHUB_LINK_RE = re.compile(
    "\\[([^\\]\\n]+)\\]\\(https://github\\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/([1-9]\\d*)\\)"
)
ISSUE_SECTION_RE = re.compile(
    "^### PRs / Issues\\s*$\\n.*?(?=^###\\s|^##\\s|\\Z)", re.MULTILINE | re.DOTALL
)


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("file must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("frontmatter is not closed")
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.match("^([A-Za-z0-9_]+):\\s*(.*?)\\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip("\"'")
    return fields


def agent_work_body(text):
    match = re.search(AGENT_WORK_PATTERN, text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def validate_facts_coverage(text: str, facts: dict) -> list[str]:
    errors: list[str] = []
    required = required_refs(facts)
    strict_refs = set(required)
    section = ISSUE_SECTION_RE.search(text)
    if section:
        heading_end = text.find("\n", section.start()) + 1
        body = text[heading_end : section.end()]
        outside_section = text[: section.start()] + text[section.end() :]
    else:
        body = ""
        outside_section = text
    linked = summary_link_refs(body)
    if linked != required:
        errors.append(
            f"PRs / Issues links must equal the deterministic closed set ordered by (repository, number): expected {required}, got {linked}"
        )
    markdown_links = MARKDOWN_GITHUB_LINK_RE.findall(body)
    markdown_refs = [
        canonical(f"{owner}/{repo}", number) for _label, owner, repo, number in markdown_links
    ]
    if markdown_refs != linked:
        errors.append("every PRs / Issues identity must be a Markdown link")
    bad_labels = []
    for label, owner, repo, number in MARKDOWN_GITHUB_LINK_RE.findall(text):
        ref = canonical(f"{owner}/{repo}", number)
        full_repo, normalized_number = split_ref(ref)
        expected_label = f"{full_repo.rsplit('/', 1)[-1]}#{normalized_number}"
        if label != expected_label:
            bad_labels.append({"ref": ref, "expected": expected_label, "got": label})
    if bad_labels:
        errors.append(
            f"PRs / Issues link labels must be repository-qualified as repo#number: {bad_labels}"
        )
    external_urls = sorted(
        {ref for ref in summary_link_refs(text) if ref not in strict_refs}, key=split_ref
    )
    if external_urls:
        errors.append(
            f"all GitHub issue/PR URLs in the summary must belong to gh_touched_today; outside strict set: {external_urls}"
        )
    allowed_tokens = allowed_ref_tokens(required)
    external_tokens = sorted(
        {token for token in explicit_ref_tokens(outside_section) if token not in allowed_tokens}
    )
    if external_tokens:
        errors.append(
            f"qualified GitHub issue/PR identities outside PRs / Issues must belong to gh_touched_today; outside strict set: {external_tokens}"
        )
    agent_work = agent_work_body(text)
    missing_times = [
        str(cluster.get("time", ""))
        for cluster in facts.get("session_clusters", [])
        if cluster.get("kind") == "human"
        and int(cluster.get("n_real_prompts", 0)) > 0
        and (str(cluster.get("time", ""))[:5] not in agent_work)
    ]
    if missing_times:
        errors.append(f"Agent work is missing human cluster time(s): {missing_times}")
    return errors


def validate(path: Path, target: str, facts_sha256: str, facts: dict | None = None) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"cannot read summary: {exc}"]
    if path.is_symlink() or not path.is_file():
        errors.append("summary must be a regular file, not a symlink")
    if len(text) < OPTIONS.get("min_chars", 800):
        errors.append("summary is shorter than the configured minimum")
    if re.search("</?markdown>", text, re.IGNORECASE):
        errors.append("summary must not contain markdown wrapper tags")
    if re.search("^```(?:markdown)?\\s*$", text, re.MULTILINE | re.IGNORECASE):
        errors.append("summary must not contain Markdown code fences")
    try:
        fields = parse_frontmatter(text)
    except ValueError as exc:
        return [str(exc)]
    target_date = datetime.strptime(target, "%Y-%m-%d").date()
    expected_window = f"{target_date - timedelta(days=2)}..{target_date}"
    substitutions = {
        "target": target,
        "start": str(target_date - timedelta(days=2)),
        "end": target,
        "input_hash": facts_sha256,
    }
    expected = {
        "title": f"Daily summary {target}",
        "date": target,
        "created": target,
        "type": "summary",
        "window": expected_window,
        "generator": "daily-summary",
        "facts_sha256": facts_sha256,
        "sources": "deterministic activity facts and referenced local mirrors",
    }
    expected.update(
        {
            key: value.format(**substitutions)
            for key, value in OPTIONS.get("frontmatter", {}).items()
        }
    )
    for field, value in expected.items():
        if fields.get(field) != value:
            errors.append(f"frontmatter {field} must be {value!r}, got {fields.get(field)!r}")
    try:
        datetime.strptime(fields.get("updated", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        errors.append("frontmatter updated must be a UTC ISO timestamp")
    title = OPTIONS.get("title_heading", "# {target}").format(**substitutions)
    if not re.search(f"^{re.escape(title)}\\s*$", text, re.MULTILINE):
        errors.append(f"missing title heading {title!r}")
    positions: list[int] = []
    for heading in REQUIRED_HEADINGS:
        match = re.search(f"^{re.escape(heading)}\\s*$", text, re.MULTILINE)
        if not match:
            errors.append(f"missing required heading {heading!r}")
        else:
            positions.append(match.start())
    if len(positions) == len(REQUIRED_HEADINGS) and positions != sorted(positions):
        errors.append("required headings are out of order")
    commentary_heading = OPTIONS.get("commentary_heading", "## Commentary")
    overall = re.search(
        "^" + re.escape(commentary_heading) + r"\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if overall:
        body = overall.group(1)
        first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
        pattern = OPTIONS.get("commentary_first_line_pattern")
        if pattern and not re.match(pattern, first_line):
            errors.append("commentary opening does not match the configured pattern")
        if re.search(r"#\d+", body):
            errors.append("commentary must not contain issue/PR numbers")
        if re.search(r"github\.com/.+?/(?:issues|pull)/\d+", body):
            errors.append("commentary must not contain issue/PR links")
    if facts is not None:
        errors.extend(validate_facts_coverage(text, facts))
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("target")
    parser.add_argument("facts_sha256")
    parser.add_argument("facts_json", nargs="?", type=Path)
    from .config import DEFAULT_CONFIG, activate, load

    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    activate(load(args.config))
    try:
        datetime.strptime(args.target, "%Y-%m-%d")
    except ValueError:
        parser.error("target must be YYYY-MM-DD")
    if not re.fullmatch("[0-9a-f]{64}", args.facts_sha256):
        parser.error("facts_sha256 must be 64 lowercase hex characters")
    facts = None
    if args.facts_json is not None:
        try:
            facts = json.loads(args.facts_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"[ERROR] cannot read facts JSON: {exc}", file=sys.stderr)
            return 1
    errors = validate(args.path, args.target, args.facts_sha256, facts)
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
