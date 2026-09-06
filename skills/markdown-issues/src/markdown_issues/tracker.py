"""Parse, audit and summarize caller-selected Markdown issue records."""

from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import ConfigurationError, load, local_path, validate_ref

KNOWN_ACTORS = {"owner", "unassigned"}
PRIORITIES = {"P0", "P1", "P2"}
KINDS = {"action", "watch", "external"}
SUB_STATES = {"in-progress", "blocked", "waiting-review", "waiting-human", "scheduled"}
KNOWN_FIELDS = {
    "id",
    "title",
    "created",
    "updated",
    "state",
    "sub_state",
    "assignee",
    "priority",
    "kind",
    "project",
    "review_after",
    "labels",
    "sources",
    "related",
    "external_refs",
    "blocks",
    "blocked_by",
    "watch_paths",
}
LIST_FIELDS = {
    "labels",
    "sources",
    "related",
    "external_refs",
    "blocks",
    "blocked_by",
    "watch_paths",
}
NOTE_RE = re.compile("^- (\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z) \\[([a-z0-9-]+)\\] (.+)$")
DATE_RE = re.compile("^\\d{4}-\\d{2}-\\d{2}$")
TIMESTAMP_RE = re.compile("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$")
CHECKBOX_RE = re.compile("^- \\[([ xX])\\] ")
SCALAR_RE = re.compile("^([a-z_]+):(?:\\s*(.*))?$")


OPEN_DIRECTORY = "records/active"
CLOSED_DIRECTORY = "records/resolved"
RELATED_PATH_PREFIXES = ("records/", "documents/")
BASE_REFS = ("origin/main", "HEAD")
DEFAULT_ACTOR = "owner"
DEFAULT_ASSIGNEE = "unassigned"
UNASSIGNED_ACTOR = "unassigned"
PRIORITY_ORDER = ["P0", "P1", "P2"]
URGENT_PRIORITIES = {"P0", "P1"}
STALE_DAYS = 30
IDLE_DAYS = 14
CONTEXT_HEADING = "## Context"
ACCEPTANCE_HEADING = "## Acceptance"
NOTES_HEADING = "## Notes"
CONFIGURATION = {}


def configure(cfg):
    global OPEN_DIRECTORY, CLOSED_DIRECTORY, RELATED_PATH_PREFIXES, BASE_REFS
    global KNOWN_ACTORS, DEFAULT_ACTOR, DEFAULT_ASSIGNEE, UNASSIGNED_ACTOR
    global PRIORITIES, PRIORITY_ORDER, URGENT_PRIORITIES, KINDS, SUB_STATES
    global STALE_DAYS, IDLE_DAYS, CONTEXT_HEADING, ACCEPTANCE_HEADING, NOTES_HEADING
    global CONFIGURATION
    CONFIGURATION = cfg
    OPEN_DIRECTORY, CLOSED_DIRECTORY = cfg["open_directory"], cfg["closed_directory"]
    RELATED_PATH_PREFIXES = tuple(cfg["related_path_prefixes"])
    BASE_REFS = tuple(cfg.get("base_refs", ["origin/main", "HEAD"]))
    KNOWN_ACTORS = set(cfg["actors"])
    DEFAULT_ACTOR, DEFAULT_ASSIGNEE = cfg["default_actor"], cfg["default_assignee"]
    UNASSIGNED_ACTOR = cfg["unassigned_actor"]
    PRIORITY_ORDER = cfg["priorities"]
    PRIORITIES, URGENT_PRIORITIES = set(PRIORITY_ORDER), set(PRIORITY_ORDER[:2])
    KINDS, SUB_STATES = set(cfg["kinds"]), set(cfg["sub_states"])
    STALE_DAYS, IDLE_DAYS = cfg["stale_days"], cfg["idle_days"]
    CONTEXT_HEADING = cfg["headings"]["context"]
    ACCEPTANCE_HEADING = cfg["headings"]["acceptance"]
    NOTES_HEADING = cfg["headings"]["notes"]


@dataclass
class Issue:
    path: Path
    relpath: str
    fields: dict[str, object]
    text: str
    notes: list[str] = field(default_factory=list)
    note_heading_count: int = 0
    acceptance_done: int = 0
    acceptance_total: int = 0

    @property
    def issue_id(self) -> str:
        return str(self.fields.get("id", ""))

    @property
    def title(self) -> str:
        return str(self.fields.get("title", self.issue_id))

    @property
    def priority(self) -> str:
        return str(self.fields.get("priority", "P2"))

    @property
    def assignee(self) -> str:
        return str(self.fields.get("assignee", DEFAULT_ASSIGNEE))

    @property
    def state(self) -> str:
        return str(self.fields.get("state", ""))

    @property
    def sub_state(self) -> str:
        return str(self.fields.get("sub_state", ""))

    @property
    def kind(self) -> str:
        return str(self.fields.get("kind", "action"))

    @property
    def review_after(self) -> date | None:
        raw = self.fields.get("review_after")
        if not raw:
            return None
        return parse_date(raw)

    @property
    def updated_at(self) -> datetime | None:
        return parse_timestamp(str(self.fields.get("updated", "")))

    @property
    def close_candidate(self) -> bool:
        return self.acceptance_total > 0 and self.acceptance_done == self.acceptance_total


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


def strip_quotes(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded, str):
                return decoded
    if len(value) >= 2 and value[0] == value[-1] and (value[0] in {'"', "'"}):
        return value[1:-1]
    return value


def parse_inline_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
            return decoded
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    return [strip_quotes(item.strip()) for item in inner.split(",") if item.strip()]


def parse_frontmatter(text: str) -> tuple[dict[str, object], int]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return ({}, 0)
    try:
        end = lines.index("---", 1)
    except ValueError:
        return ({}, 0)
    fields: dict[str, object] = {}
    current_list: str | None = None
    for raw in lines[1:end]:
        match = SCALAR_RE.match(raw)
        if match:
            key, value = match.groups()
            value = (value or "").strip()
            current_list = None
            if key in LIST_FIELDS:
                if value.startswith("[") and value.endswith("]"):
                    fields[key] = parse_inline_list(value)
                elif value:
                    fields[key] = [strip_quotes(value)]
                else:
                    fields[key] = []
                    current_list = key
            else:
                fields[key] = strip_quotes(value)
            continue
        if current_list and re.match("^\\s+-\\s+", raw):
            item = re.sub("^\\s+-\\s+", "", raw).strip()
            cast = fields[current_list]
            assert isinstance(cast, list)
            cast.append(strip_quotes(item))
    return (fields, end + 1)


def frontmatter_diagnostics(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return ["frontmatter must start with ---"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return ["frontmatter is missing closing ---"]
    diagnostics: list[str] = []
    seen: set[str] = set()
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith(("#", "-")) or raw[0].isspace():
            continue
        match = re.match("^([^:]+):", raw)
        if not match:
            diagnostics.append(f"malformed frontmatter line: {raw[:80]}")
            continue
        key = match.group(1)
        if not re.fullmatch("[a-z_]+", key):
            diagnostics.append(f"invalid frontmatter key: {key}")
        if key in seen:
            diagnostics.append(f"duplicate frontmatter key: {key}")
        seen.add(key)
    return diagnostics


def section_lines(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line == heading]
    result: list[str] = []
    for start in starts:
        for line in lines[start + 1 :]:
            if line.startswith("## "):
                break
            result.append(line)
    return result


def note_entries(text: str) -> list[str]:
    entries: list[str] = []
    for body in note_sections(text):
        current: list[str] = []
        for line in body:
            if line.startswith("- "):
                if current:
                    entries.append("\n".join(current))
                current = [line]
            elif line.strip() and current:
                current.append(line)
            elif line.strip():
                entries.append(line)
        if current:
            entries.append("\n".join(current))
    return entries


def note_sections(text: str) -> list[list[str]]:
    lines = text.splitlines()
    sections: list[list[str]] = []
    for index, line in enumerate(lines):
        if line != NOTES_HEADING:
            continue
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.startswith("## "):
                break
            body.append(candidate)
        sections.append(body)
    return sections


def parse_issue(path: Path, repo: Path) -> Issue:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("issue must be a regular file")
    text = path.read_text(encoding="utf-8")
    return parse_issue_text(text, path, repo)


def parse_issue_text(text: str, path: Path, repo: Path) -> Issue:
    fields, _ = parse_frontmatter(text)
    notes = note_entries(text)
    acceptance = [
        line for line in section_lines(text, ACCEPTANCE_HEADING) if CHECKBOX_RE.match(line)
    ]
    done = sum(1 for line in acceptance if CHECKBOX_RE.match(line).group(1).lower() == "x")
    return Issue(
        path=path,
        relpath=path.relative_to(repo).as_posix(),
        fields=fields,
        text=text,
        notes=notes,
        note_heading_count=sum(1 for line in text.splitlines() if line == NOTES_HEADING),
        acceptance_done=done,
        acceptance_total=len(acceptance),
    )


def load_issues(repo: Path) -> list[Issue]:
    if not any(
        local_path(repo, directory).is_dir() for directory in (OPEN_DIRECTORY, CLOSED_DIRECTORY)
    ):
        raise ConfigurationError("configured issue directories are missing")
    paths = sorted(local_path(repo, OPEN_DIRECTORY).glob("*.md"))
    paths += sorted(local_path(repo, CLOSED_DIRECTORY).glob("*.md"))
    return [parse_issue(path, repo) for path in paths]


def parse_timestamp(value: str) -> datetime | None:
    if not TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def parse_date(value: object) -> date | None:
    text = str(value)
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def git_output(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else None


def base_issue_paths(repo: Path, base_ref: str) -> dict[str, str]:
    output = git_output(
        repo, "ls-tree", "-r", "--name-only", base_ref, OPEN_DIRECTORY, CLOSED_DIRECTORY
    )
    if output is None:
        return {}
    return {Path(line).stem: line for line in output.splitlines() if line.endswith(".md")}


def base_issue(repo: Path, base_ref: str, issue: Issue, path_map: dict[str, str]) -> Issue | None:
    old_path = path_map.get(issue.issue_id)
    if not old_path:
        return None
    old_text = git_output(repo, "show", f"{base_ref}:{old_path}")
    if old_text is None:
        return None
    return parse_issue_text(old_text, repo / old_path, repo)


def resolve_base_ref(repo: Path, requested: str | None) -> str | None:
    if requested:
        return requested
    for candidate in BASE_REFS:
        if git_output(repo, "rev-parse", "--verify", candidate) is not None:
            return candidate
    return None


def base_comparison_direction(repo: Path, base_ref: str) -> tuple[bool, str]:
    if git_output(repo, "rev-parse", "--verify", "HEAD") is None:
        return (False, "HEAD does not resolve")
    if git_output(repo, "merge-base", "--is-ancestor", base_ref, "HEAD") is not None:
        return (True, "")
    ahead = git_output(
        repo,
        "log",
        "--format=%h",
        base_ref,
        "--not",
        "HEAD",
        "--",
        OPEN_DIRECTORY,
        CLOSED_DIRECTORY,
    )
    if ahead is None or not ahead.strip():
        return (True, "")
    count = len(ahead.strip().splitlines())
    return (
        False,
        f"{count} commit(s) touching issue files are on {base_ref} but not in this checkout",
    )


def watch_signals(repo: Path, issue: Issue, ref: str = "HEAD") -> list[str]:
    watched = list_value(issue, "watch_paths")
    if issue.state != "open" or not watched:
        return []
    anchor = git_output(repo, "log", "-1", "--format=%H", ref, "--", issue.relpath)
    if not anchor or not anchor.strip():
        return []
    changed: set[str] = set()
    for range_arg in (f"{anchor.strip()}..{ref}", ref):
        args = ["diff", "--name-only"]
        if range_arg == ref:
            args.append(ref)
        else:
            args.append(range_arg)
        args.extend(["--", *watched])
        output = git_output(repo, *args)
        if output:
            changed.update(line for line in output.splitlines() if line)
    return sorted(changed)


def is_scheduled(
    issue: Issue, today: date, repo: Path | None = None, watch_ref: str = "HEAD"
) -> bool:
    review_after = issue.review_after
    eligible = issue.kind == "watch" or issue.sub_state in {"waiting-human", "scheduled"}
    if not eligible or review_after is None or review_after <= today:
        return False
    return not (repo and watch_signals(repo, issue, watch_ref))


def list_value(issue: Issue, field_name: str) -> list[str]:
    value = issue.fields.get(field_name, [])
    return [str(item) for item in value] if isinstance(value, list) else []


def expected_state(issue: Issue) -> str:
    return "open" if Path(issue.relpath).parent.as_posix() == OPEN_DIRECTORY else "closed"


def days_since(value: datetime | None, today: date) -> int | None:
    if value is None:
        return None
    return (today - value.date()).days


def latest_note_timestamp(issue: Issue) -> datetime | None:
    timestamps = []
    for line in issue.notes:
        match = NOTE_RE.match(line)
        if match:
            parsed = parse_timestamp(match.group(1))
            if parsed:
                timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def audit_issues(
    repo: Path, issues: list[Issue], today: date, base_ref: str | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    by_id = {issue.issue_id: issue for issue in issues if issue.issue_id}
    compare_ref = base_ref
    if base_ref and git_output(repo, "rev-parse", "--verify", base_ref) is None:
        findings.append(Finding("ERROR", f"base ref does not resolve: {base_ref}"))
        compare_ref = None
    elif base_ref:
        forwards, reason = base_comparison_direction(repo, base_ref)
        if not forwards:
            merge_base = git_output(repo, "merge-base", base_ref, "HEAD")
            compare_ref = merge_base.strip() if merge_base and merge_base.strip() else None
            covered = (
                f"verified against the divergence point {compare_ref[:12]} instead"
                if compare_ref
                else "no verdict was produced"
            )
            findings.append(
                Finding(
                    "ERROR",
                    f"cannot verify the issue append-only contract against {base_ref}: {reason}, so comparing against it would run backwards and report another commit's legal append as your own violation. {covered} — sound as far as it goes, but it does not cover those commit(s). Run 'git pull --rebase' and re-run this lint.",
                )
            )
    path_map = base_issue_paths(repo, compare_ref) if compare_ref else {}
    current_stems = {issue.path.stem for issue in issues}
    for missing_id in sorted(set(path_map) - current_stems):
        findings.append(
            Finding(
                "ERROR",
                f"{path_map[missing_id]} — Issue disappeared; close/reopen with git mv, never delete or rename",
            )
        )
    seen_ids: set[str] = set()
    for issue in issues:
        if issue.issue_id and issue.issue_id in seen_ids:
            findings.append(Finding("ERROR", f"{issue.relpath} — duplicate id: {issue.issue_id}"))
        seen_ids.add(issue.issue_id)
    for issue in issues:
        prefix = issue.relpath
        for diagnostic in frontmatter_diagnostics(issue.text):
            findings.append(Finding("ERROR", f"{prefix} — {diagnostic}"))
        body_lines = issue.text.splitlines()
        section_positions: dict[str, int] = {}
        for heading in (CONTEXT_HEADING, ACCEPTANCE_HEADING, NOTES_HEADING):
            positions = [index for index, line in enumerate(body_lines) if line == heading]
            if not positions:
                findings.append(Finding("ERROR", f"{prefix} — missing {heading} section"))
            else:
                section_positions[heading] = positions[0]
                if heading != NOTES_HEADING and len(positions) > 1:
                    findings.append(Finding("ERROR", f"{prefix} — duplicate {heading} section"))
        if len(section_positions) == 3 and (
            not section_positions[CONTEXT_HEADING]
            < section_positions[ACCEPTANCE_HEADING]
            < section_positions[NOTES_HEADING]
        ):
            findings.append(
                Finding(
                    "ERROR", f"{prefix} — body sections must be Context, Acceptance, Notes in order"
                )
            )
        for heading in (CONTEXT_HEADING, ACCEPTANCE_HEADING):
            if heading in section_positions and (
                not any(line.strip() for line in section_lines(issue.text, heading))
            ):
                findings.append(Finding("ERROR", f"{prefix} — {heading} section is empty"))
        if issue.kind in {"action", "external"} and issue.acceptance_total == 0:
            findings.append(
                Finding("ERROR", f"{prefix} — action/external Acceptance needs a checkbox")
            )
        if issue.note_heading_count and (not issue.notes):
            findings.append(Finding("ERROR", f"{prefix} — Notes section has no entries"))
        required = {"id", "title", "created", "updated", "state", "assignee", "priority"}
        for field_name in sorted(required):
            if not issue.fields.get(field_name):
                findings.append(
                    Finding("ERROR", f"{prefix} — missing required field: {field_name}")
                )
        if issue.issue_id != issue.path.stem:
            findings.append(Finding("ERROR", f"{prefix} — id does not match filename"))
        if issue.state != expected_state(issue):
            findings.append(Finding("ERROR", f"{prefix} — state does not match directory"))
        if issue.assignee not in KNOWN_ACTORS:
            findings.append(Finding("ERROR", f"{prefix} — unknown assignee: {issue.assignee}"))
        if issue.priority not in PRIORITIES:
            findings.append(Finding("ERROR", f"{prefix} — invalid priority: {issue.priority}"))
        if issue.fields.get("kind") and issue.kind not in KINDS:
            findings.append(Finding("ERROR", f"{prefix} — invalid kind: {issue.kind}"))
        if issue.state == "open" and (not issue.fields.get("kind")):
            findings.append(
                Finding("WARN", f"{prefix} — open issue has no kind; defaults to action")
            )
        if issue.sub_state and issue.sub_state not in SUB_STATES:
            findings.append(Finding("ERROR", f"{prefix} — invalid sub_state: {issue.sub_state}"))
        if issue.state == "closed" and issue.sub_state:
            findings.append(Finding("ERROR", f"{prefix} — closed issue must not have sub_state"))
        unknown = sorted(set(issue.fields) - KNOWN_FIELDS)
        for field_name in unknown:
            findings.append(Finding("WARN", f"{prefix} — unknown frontmatter field: {field_name}"))
        timestamps: dict[str, datetime] = {}
        for field_name in ("created", "updated"):
            if (
                issue.fields.get(field_name)
                and parse_timestamp(str(issue.fields[field_name])) is None
            ):
                findings.append(
                    Finding("ERROR", f"{prefix} — {field_name} must be ISO-8601 with timezone")
                )
            elif issue.fields.get(field_name):
                parsed = parse_timestamp(str(issue.fields[field_name]))
                assert parsed is not None
                timestamps[field_name] = parsed
        if (
            timestamps.get("created")
            and timestamps.get("updated")
            and timestamps["updated"] < timestamps["created"]
        ):
            findings.append(Finding("ERROR", f"{prefix} — updated precedes created"))
        if issue.fields.get("review_after"):
            if parse_date(issue.fields["review_after"]) is None:
                findings.append(Finding("ERROR", f"{prefix} — review_after must be YYYY-MM-DD"))
            elif issue.kind != "watch" and issue.sub_state not in {"waiting-human", "scheduled"}:
                findings.append(
                    Finding(
                        "ERROR",
                        f"{prefix} — review_after only schedules watch, waiting-human, or scheduled work",
                    )
                )
        if (
            issue.kind == "watch"
            and issue.state == "open"
            and (not issue.fields.get("review_after"))
        ):
            findings.append(Finding("WARN", f"{prefix} — watch issue should set review_after"))
        for source in list_value(issue, "sources"):
            try:
                if not local_path(repo, source).exists():
                    findings.append(Finding("ERROR", f"{prefix} — source does not exist: {source}"))
            except ConfigurationError:
                findings.append(Finding("ERROR", f"{prefix} — invalid source path: {source}"))
        for related in list_value(issue, "related"):
            if related.startswith(RELATED_PATH_PREFIXES):
                try:
                    if not local_path(repo, related).exists():
                        findings.append(
                            Finding("ERROR", f"{prefix} — related path does not exist: {related}")
                        )
                except ConfigurationError:
                    findings.append(Finding("ERROR", f"{prefix} — invalid related path: {related}"))
        for watched in list_value(issue, "watch_paths"):
            try:
                path = local_path(repo, watched, glob=True)
                matches = glob.glob(str(path))
                if not matches and not path.exists():
                    findings.append(
                        Finding("ERROR", f"{prefix} — watch_path does not exist: {watched}")
                    )
                for match in matches:
                    local_path(repo, Path(match).relative_to(repo).as_posix())
            except ConfigurationError:
                findings.append(Finding("ERROR", f"{prefix} — invalid watch path: {watched}"))
        for field_name in ("blocks", "blocked_by"):
            for reference in list_value(issue, field_name):
                ref_id = Path(reference).stem if reference.endswith(".md") else reference
                if ref_id not in by_id:
                    findings.append(
                        Finding(
                            "ERROR", f"{prefix} — {field_name} id does not resolve: {reference}"
                        )
                    )
        old_issue = base_issue(repo, compare_ref, issue, path_map) if compare_ref else None
        old_notes = old_issue.notes if old_issue else None
        if old_notes is not None and issue.notes[: len(old_notes)] != old_notes:
            findings.append(
                Finding("ERROR", f"{prefix} — existing Notes entries changed or were reordered")
            )
        new_note_start = len(old_notes) if old_notes is not None else 0
        if issue.note_heading_count == 0:
            findings.append(Finding("ERROR", f"{prefix} — missing ## Notes section"))
        elif issue.note_heading_count > 1:
            level = (
                "WARN"
                if old_issue and issue.note_heading_count == old_issue.note_heading_count
                else "ERROR"
            )
            findings.append(Finding(level, f"{prefix} — duplicate ## Notes sections"))
        if old_issue and issue.note_heading_count != old_issue.note_heading_count:
            findings.append(Finding("ERROR", f"{prefix} — Notes heading count changed"))
        for index, note in enumerate(issue.notes):
            first_line = note.splitlines()[0]
            match = NOTE_RE.match(first_line)
            if not match:
                level = "ERROR" if index >= new_note_start else "WARN"
                findings.append(
                    Finding(level, f"{prefix} — malformed Notes entry: {first_line[:90]}")
                )
                continue
            if match.group(2) not in KNOWN_ACTORS - {UNASSIGNED_ACTOR}:
                level = "ERROR" if index >= new_note_start else "WARN"
                findings.append(Finding(level, f"{prefix} — unknown Notes actor: {match.group(2)}"))
            if parse_timestamp(match.group(1)) is None:
                level = "ERROR" if index >= new_note_start else "WARN"
                findings.append(
                    Finding(level, f"{prefix} — invalid Notes timestamp: {match.group(1)}")
                )
        if old_issue:
            if issue.fields.get("created") != old_issue.fields.get("created"):
                findings.append(Finding("ERROR", f"{prefix} — created is immutable"))
            changed = issue.text != old_issue.text or issue.relpath != old_issue.relpath
            if changed:
                old_updated = old_issue.updated_at
                if not issue.updated_at or not old_updated or issue.updated_at <= old_updated:
                    findings.append(
                        Finding("ERROR", f"{prefix} — meaningful edit must advance updated")
                    )
                if len(issue.notes) <= len(old_issue.notes):
                    findings.append(
                        Finding("ERROR", f"{prefix} — meaningful edit must append a Notes entry")
                    )
            if issue.state != old_issue.state and (
                issue.state not in {"open", "closed"}
                or old_issue.state
                not in {
                    "open",
                    "closed",
                }
            ):
                findings.append(Finding("ERROR", f"{prefix} — invalid state transition"))
        if issue.state == "open":
            review_after = issue.review_after
            scheduled = is_scheduled(issue, today, repo, base_ref or "HEAD")
            if review_after is not None and review_after <= today:
                findings.append(
                    Finding("WARN", f"{prefix} — review due since {review_after.isoformat()}")
                )
            signals = watch_signals(repo, issue, base_ref or "HEAD") if base_ref else []
            if signals:
                findings.append(
                    Finding("WARN", f"{prefix} — watch_path changed: {', '.join(signals[:5])}")
                )
            updated_age = days_since(issue.updated_at, today)
            if not scheduled and updated_age is not None and (updated_age > STALE_DAYS):
                findings.append(Finding("WARN", f"{prefix} — stale: updated {updated_age}d ago"))
            note_age = days_since(latest_note_timestamp(issue), today)
            if not scheduled and note_age is not None and (note_age > IDLE_DAYS):
                findings.append(
                    Finding("WARN", f"{prefix} — idle: last Notes entry {note_age}d ago")
                )
            if issue.close_candidate:
                findings.append(
                    Finding("WARN", f"{prefix} — close-candidate: all acceptance boxes checked")
                )
            if issue.priority in URGENT_PRIORITIES and issue.assignee == UNASSIGNED_ACTOR:
                findings.append(Finding("WARN", f"{prefix} — urgent issue is unassigned"))
        else:
            for blocker in list_value(issue, "blocked_by"):
                ref_id = Path(blocker).stem if blocker.endswith(".md") else blocker
                target = by_id.get(ref_id)
                if target and target.state == "open":
                    findings.append(
                        Finding("WARN", f"{prefix} — closed issue still blocked by open {ref_id}")
                    )
    return findings


def brief_lines(
    issues: Iterable[Issue],
    today: date,
    limit: int,
    assignee: str | None = None,
    repo: Path | None = None,
    watch_ref: str = "HEAD",
) -> list[str]:
    open_issues = [issue for issue in issues if issue.state == "open"]
    if assignee:
        open_issues = [issue for issue in open_issues if issue.assignee == assignee]
    future = [issue for issue in open_issues if is_scheduled(issue, today, repo, watch_ref)]
    actionable = [issue for issue in open_issues if issue not in future]
    priority_rank = {value: index for index, value in enumerate(PRIORITY_ORDER)}

    def sort_key(issue: Issue) -> tuple[object, ...]:
        due = issue.review_after is not None and issue.review_after <= today
        waiting = issue.sub_state in {"waiting-human", "scheduled"}
        updated = issue.updated_at or datetime.min.replace(tzinfo=timezone.utc)
        return (
            priority_rank.get(issue.priority, 9),
            0 if issue.close_candidate else 1,
            0 if due else 1,
            1 if waiting else 0,
            updated,
        )

    lines = [
        f"Issues: {len(open_issues)} open; {len(actionable)} actionable; {len(future)} scheduled"
    ]
    for issue in sorted(actionable, key=sort_key)[:limit]:
        flags = []
        if issue.close_candidate:
            flags.append("CLOSE?")
        if issue.review_after and issue.review_after <= today:
            flags.append("DUE")
        if issue.sub_state:
            flags.append(issue.sub_state)
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        lines.append(
            f"- {issue.priority}{flag_text} ({issue.assignee}) {issue.title} — {issue.relpath}"
        )
    if future:
        lines.append("Scheduled reviews:")
        for issue in sorted(
            future, key=lambda item: (item.review_after or date.max, item.priority)
        )[:3]:
            lines.append(f"- {issue.review_after} ({issue.assignee}) {issue.title}")
    return lines


def command_lint(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    base_ref = resolve_base_ref(repo, args.base_ref)
    findings = audit_issues(repo, load_issues(repo), date.fromisoformat(args.today), base_ref)
    for finding in findings:
        separator = "\t" if args.tsv else ": "
        print(f"{finding.level}{separator}{finding.message}")
    return 1 if any(finding.level == "ERROR" for finding in findings) else 0


def command_brief(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise ConfigurationError("brief limit must be positive")
    repo = Path(args.repo).resolve()
    for line in brief_lines(
        load_issues(repo),
        date.fromisoformat(args.today),
        args.limit,
        args.assignee,
        repo,
        args.watch_ref,
    ):
        print(line)
    return 0


def command_watch_signals(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if git_output(repo, "rev-parse", "--verify", args.ref) is None:
        print(f"ERROR: watch ref does not resolve: {args.ref}", file=sys.stderr)
        return 1
    for issue in load_issues(repo):
        for changed in watch_signals(repo, issue, args.ref):
            print(f"{issue.issue_id}\t{changed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--today", default=datetime.now(timezone.utc).date().isoformat())
    subparsers = parser.add_subparsers(dest="command", required=True)
    lint_parser = subparsers.add_parser("lint")
    lint_parser.add_argument("--base-ref", default=None)
    lint_parser.add_argument("--tsv", action="store_true")
    lint_parser.set_defaults(func=command_lint)
    brief_parser = subparsers.add_parser("brief")
    brief_parser.add_argument("--limit", type=int, default=8)
    brief_parser.add_argument("--assignee", default=None)
    brief_parser.add_argument("--watch-ref", default="HEAD")
    brief_parser.set_defaults(func=command_brief)
    signals_parser = subparsers.add_parser("watch-signals")
    signals_parser.add_argument("--ref", default="HEAD")
    signals_parser.set_defaults(func=command_watch_signals)
    from .create import command_create

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("title")
    create_parser.add_argument("--assignee")
    create_parser.add_argument("--actor")
    create_parser.add_argument("--priority", required=True)
    create_parser.add_argument("--kind", default="action")
    create_parser.add_argument("--project")
    create_parser.add_argument("--sub-state")
    create_parser.add_argument("--review-after")
    create_parser.add_argument("--labels")
    create_parser.add_argument("--source", action="append", default=[])
    create_parser.add_argument("--watch-path", action="append", default=[])
    create_parser.add_argument("--dry-run", action="store_true")
    create_parser.set_defaults(func=command_create)
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        cfg = load(args.config, args.repo)
        configure(cfg)
        args.configuration = cfg
        args.repo = cfg["repository_root"]
        if parse_date(args.today) is None:
            raise ConfigurationError("today must be YYYY-MM-DD")
        for name in ("base_ref", "watch_ref", "ref"):
            value = getattr(args, name, None)
            if value is not None:
                validate_ref(value)
        return args.func(args)
    except (ConfigurationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: tracker operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
