#!/usr/bin/env python3
"""Mirror configured GitHub issues, pull requests, and comments to Markdown.

Read-only GitHub operations use the caller's authenticated ``gh`` command.
Configuration, output, and incremental state are caller-owned. This program
never commits, pushes, modifies GitHub, or calls a language model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import yaml  # PyYAML
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────


@dataclass
class RepoConfig:
    owner: str
    name: str
    seed_all: bool = False
    seed_labels: list[str] = field(default_factory=list)
    seed_authors: list[str] = field(default_factory=list)
    seed_involves: list[str] = field(default_factory=list)
    closure_depth: int = 1
    closure_max_total: int = 300
    exclude_labels: list[str] = field(default_factory=list)
    exclude_closed_before: str | None = None
    directory: str | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def dir_slug(self) -> str:
        return self.directory or f"{self.owner}_{self.name}"


@dataclass
class GlobalConfig:
    repos: list[RepoConfig]
    state_file: str | None
    output_dir: str | None
    filename_template: str = "{number}.md"

    def filename(self, number: int) -> str:
        return self.filename_template.replace("{number}", str(number))


def load_config(path: Path) -> GlobalConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("repos"), list) or not raw["repos"]:
        raise ValueError("configuration requires a non-empty repos list")
    repos: list[RepoConfig] = []
    for r in raw["repos"]:
        if not isinstance(r, dict):
            raise ValueError("each repository must be a mapping")
        seeds = r.get("seeds", {})
        closure = r.get("closure", {})
        exclude = r.get("exclude", {})
        if not all(isinstance(section, dict) for section in (seeds, closure, exclude)):
            raise ValueError("seeds, closure, and exclude must be mappings")
        if not isinstance(seeds.get("all", False), bool):
            raise ValueError("seeds.all must be a boolean")
        for section, keys in ((seeds, ("labels", "authors", "involves")), (exclude, ("labels",))):
            for key in keys:
                values = section.get(key, [])
                if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                    raise ValueError("seed and exclusion filters must be lists of non-empty strings")
        if exclude.get("closed_before") is not None and not isinstance(exclude["closed_before"], str):
            raise ValueError("exclude.closed_before must be a quoted timestamp")
        if "directory" in r and (not isinstance(r["directory"], str) or not r["directory"]):
            raise ValueError("repository directory must be a non-empty string")
        repos.append(
            RepoConfig(
                owner=r["owner"],
                name=r["name"],
                seed_all=bool(seeds.get("all", False)),
                seed_labels=list(seeds.get("labels", [])),
                seed_authors=list(seeds.get("authors", [])),
                seed_involves=list(seeds.get("involves", [])),
                closure_depth=int(closure.get("depth", 1)),
                closure_max_total=int(closure.get("max_total", 300)),
                exclude_labels=list(exclude.get("labels", [])),
                exclude_closed_before=exclude.get("closed_before"),
                directory=r.get("directory"),
            )
        )
    directories: set[str] = set()
    identities: set[str] = set()
    for repo in repos:
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
               for value in (repo.owner, repo.name)):
            raise ValueError("repository owner and name must be single GitHub path components")
        if (not isinstance(repo.dir_slug, str) or repo.dir_slug in {".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", repo.dir_slug)):
            raise ValueError("repository directory must be a single safe path component")
        if repo.slug.casefold() in identities or repo.dir_slug.casefold() in directories:
            raise ValueError("repository identities and output directories must be unique")
        identities.add(repo.slug.casefold())
        directories.add(repo.dir_slug.casefold())
        if repo.closure_depth < 0 or repo.closure_max_total < 0:
            raise ValueError("closure limits must be nonnegative")
    template = raw.get("filename_template", "{number}.md")
    if (not isinstance(template, str) or template.count("{number}") != 1
            or not re.fullmatch(r"[A-Za-z0-9_.-]*\{number\}[A-Za-z0-9_.-]*\.md", template)):
        raise ValueError("filename_template must be a Markdown filename containing one {number}")
    for key in ("state_file", "output_dir"):
        if raw.get(key) is not None and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise ValueError("configured output and state paths must be non-empty strings")
    return GlobalConfig(repos, raw.get("state_file"), raw.get("output_dir"), template)


# ────────────────────────────────────────────────────────────────────────
# `gh api` wrapper
# ────────────────────────────────────────────────────────────────────────


def gh_api(path: str, *, paginate: bool = False, method: str = "GET") -> Any:
    """Call `gh api <path>`, optionally paginated. Returns parsed JSON."""
    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    cmd.extend(["-H", "Accept: application/vnd.github+json"])
    if method != "GET":
        cmd.extend(["--method", method])
    cmd.append(path)
    try:
        out = subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=120
        ).stdout
    except subprocess.CalledProcessError:
        raise RuntimeError("GitHub API command failed; check authentication and access") from None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("GitHub API command timed out") from None

    # When --paginate hits multiple pages, gh concatenates JSON arrays
    # into one JSON-array-per-page sequence. Normalize.
    if paginate:
        items: list[Any] = []
        decoder = json.JSONDecoder()
        idx = 0
        out = out.strip()
        try:
            while idx < len(out):
                obj, end = decoder.raw_decode(out, idx)
                if isinstance(obj, list):
                    items.extend(obj)
                else:
                    items.append(obj)
                idx = end
                while idx < len(out) and out[idx] in " \n\r\t":
                    idx += 1
        except json.JSONDecodeError as e:
            raise RuntimeError(f"gh api {path} returned invalid JSON") from e
        return items
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh api {path} returned invalid JSON") from e


# ────────────────────────────────────────────────────────────────────────
# Seeds + closure
# ────────────────────────────────────────────────────────────────────────


MAX_ISSUE_NUMBER = 999_999_999


def valid_issue_number(value: Any) -> int | None:
    """Return a normalized positive GitHub issue number, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        return None
    if number < 1 or number > MAX_ISSUE_NUMBER:
        return None
    return number


def issue_updates(items: Any) -> dict[int, str | None]:
    """Normalize GitHub issue-list payloads to number -> updated_at."""
    if not isinstance(items, list):
        raise RuntimeError("GitHub issue listing returned a non-list payload")
    updates: dict[int, str | None] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        number = valid_issue_number(item.get("number"))
        if number is None:
            continue
        updated = item.get("updated_at") or item.get("updatedAt")
        updates[number] = updated if isinstance(updated, str) else None
    return updates


def gh_issue_list(repo: str, *flags: str) -> dict[int, str | None]:
    """Use `gh issue list` (including caller-selected filters).
    Returns issue-number -> updated-at for open + closed issues."""
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", "1000",
        "--json", "number,updatedAt",
        *flags,
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                             timeout=60).stdout
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "GitHub issue listing failed; check authentication and access"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("GitHub issue listing timed out") from None
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"gh issue list {repo} {flags} returned invalid JSON"
        ) from e
    return issue_updates(data)


def gh_all_items(repo: str, *, since: str | None = None) -> dict[int, str | None]:
    """List every issue and PR through GitHub's paginated Issues endpoint."""
    path = f"/repos/{repo}/issues?state=all&per_page=100"
    if since:
        path += f"&since={quote(since, safe=':-TZ')}"
    return issue_updates(gh_api(path, paginate=True))


def collect_seeds(rc: RepoConfig) -> dict[int, str | None]:
    seeds: dict[int, str | None] = {}
    repo = rc.slug

    def merge(updates: dict[int, str | None]) -> None:
        for number, updated in updates.items():
            # Prefer a known timestamp when the same seed matched two queries.
            if updated or number not in seeds:
                seeds[number] = updated

    if rc.seed_all:
        updates = gh_all_items(repo)
        print(f"  all → {len(updates)} issues/PRs", file=sys.stderr)
        merge(updates)

    for label in rc.seed_labels:
        updates = gh_issue_list(repo, "--label", label)
        print(f"  label:{label} → {len(updates)} issues", file=sys.stderr)
        merge(updates)

    for author in rc.seed_authors:
        updates = gh_issue_list(repo, "--author", author)
        print(f"  author:{author} → {len(updates)} issues", file=sys.stderr)
        merge(updates)

    for involve in rc.seed_involves:
        # `gh issue list` doesn't have --involves; use --mention as best-effort
        updates = gh_issue_list(repo, "--mention", involve)
        print(f"  mention:{involve} → {len(updates)} issues", file=sys.stderr)
        merge(updates)

    return dict(sorted(seeds.items()))


# Match GitHub cross-references in body / comments
# Patterns: #1234   owner/repo#1234   /owner/repo/issues/1234   /owner/repo/pull/1234
_CROSSREF_RE = re.compile(
    r"""
    (?:                                             # full repo form?
       (?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)\#(?P<n1>[1-9]\d*)
     | github\.com/(?P<owner2>[\w.-]+)/(?P<repo2>[\w.-]+)/(?:issues|pull)/(?P<n2>[1-9]\d*)
     | (?<![\w/])\#(?P<n3>[1-9]\d*)                  # bare #1234 (not part of word/url)
    )
    """,
    re.VERBOSE,
)


def extract_crossrefs(text: str, default_repo: str) -> set[tuple[str, int]]:
    """Find (repo, num) tuples referenced from text. Bare #N => default_repo."""
    refs: set[tuple[str, int]] = set()
    for m in _CROSSREF_RE.finditer(text or ""):
        if m.group("n1"):
            repo = f"{m.group('owner')}/{m.group('repo')}"
            number = valid_issue_number(m.group("n1"))
        elif m.group("n2"):
            repo = f"{m.group('owner2')}/{m.group('repo2')}"
            number = valid_issue_number(m.group("n2"))
        elif m.group("n3"):
            repo = default_repo
            number = valid_issue_number(m.group("n3"))
        else:
            continue
        if number is not None:
            refs.add((repo, number))
    return refs


# ────────────────────────────────────────────────────────────────────────
# Fetch + render
# ────────────────────────────────────────────────────────────────────────


def fetch_issue(repo: str, num: int) -> dict | None:
    try:
        issue = gh_api(f"/repos/{repo}/issues/{num}")
    except RuntimeError as e:
        print(f"  fetch issue {repo}#{num} failed: {e}", file=sys.stderr)
        return None
    if not isinstance(issue, dict):
        print(f"  fetch issue {repo}#{num} failed: non-object payload",
              file=sys.stderr)
        return None
    # Pull all comments (paginated)
    try:
        comments = gh_api(f"/repos/{repo}/issues/{num}/comments?per_page=100",
                          paginate=True)
    except RuntimeError as e:
        print(f"  fetch comments {repo}#{num} failed: {e}", file=sys.stderr)
        return None
    if not isinstance(comments, list):
        print(f"  fetch comments {repo}#{num} failed: non-list payload",
              file=sys.stderr)
        return None
    issue["_comments"] = comments
    return issue


def yaml_dump_block(d: dict) -> str:
    """Emit deterministic YAML for frontmatter (sorted keys, block style)."""
    return yaml.safe_dump(d, sort_keys=True, allow_unicode=True,
                          default_flow_style=False).rstrip() + "\n"


def render_issue_md(issue: dict, repo: str, related: list[int]) -> str:
    """Render issue → markdown. Deterministic: same issue → same bytes.

    `synced` timestamp is intentionally NOT in the frontmatter — it would
    cause every cron run to rewrite every file even when content is
    unchanged, exploding git diffs. The `updated` field (from GitHub) is
    the source of truth for "when did this issue last change".
    Incremental progress is kept in the separately configured state file.
    """
    labels = sorted([lbl["name"] for lbl in issue.get("labels", [])])
    assignees = sorted([a["login"] for a in issue.get("assignees", [])])
    milestone = (issue.get("milestone") or {}).get("title")
    is_pr = "pull_request" in issue
    merged_at = (issue.get("pull_request") or {}).get("merged_at")

    fm = {
        "type": "gh-pull-request" if is_pr else "gh-issue",
        "repo": repo,
        "number": issue["number"],
        "title": issue.get("title", ""),
        "state": issue.get("state"),
        "author": (issue.get("user") or {}).get("login"),
        "created": issue.get("created_at"),
        "updated": issue.get("updated_at"),
        "closed": issue.get("closed_at"),
        "merged": merged_at,
        "labels": labels,
        "assignees": assignees,
        "milestone": milestone,
        "url": issue.get("html_url"),
        "related": sorted(related),
        "comment_count": len(issue.get("_comments", [])),
    }
    fm = {k: v for k, v in fm.items() if v not in (None, [], "")}

    parts = ["---", yaml_dump_block(fm).rstrip(), "---", ""]

    title = issue.get("title", "(no title)")
    parts.append(f"# {title}")
    parts.append("")

    body = (issue.get("body") or "").rstrip()
    if body:
        author = (issue.get("user") or {}).get("login", "?")
        created = issue.get("created_at", "?")
        parts.append(f"## Body — @{author} · {created}")
        parts.append("")
        parts.append(body)
        parts.append("")

    if issue.get("_comments"):
        parts.append("## Comments")
        parts.append("")
        for c in issue["_comments"]:
            cauthor = (c.get("user") or {}).get("login", "?")
            ccreated = c.get("created_at", "?")
            cupdated = c.get("updated_at")
            edited = ""
            if cupdated and cupdated != ccreated:
                edited = f" · edited {cupdated}"
            parts.append(f"### @{cauthor} · {ccreated}{edited}")
            parts.append("")
            cbody = (c.get("body") or "").rstrip()
            parts.append(cbody if cbody else "_(empty)_")
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def issue_crossrefs(issue: dict, repo: str) -> set[tuple[str, int]]:
    """Extract cross-references from a fetched issue and all its comments."""
    refs = extract_crossrefs(issue.get("body") or "", repo)
    for comment in issue.get("_comments", []):
        refs |= extract_crossrefs(comment.get("body") or "", repo)
    return refs


def normalized_state_numbers(value: Any) -> set[int]:
    """Read positive target numbers from old or current state formats."""
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for raw in value:
        number = valid_issue_number(raw)
        if number is not None:
            result.add(number)
    return result


def existing_target_numbers(repo_dir: Path, template: str = "{number}.md") -> set[int]:
    """Recover target membership from mirrors written before state tracked it."""
    if not repo_dir.is_dir():
        return set()
    result: set[int] = set()
    pattern = re.compile(re.escape(template).replace(re.escape("{number}"), r"([1-9]\d*)"))
    for path in repo_dir.glob("*.md"):
        match = pattern.fullmatch(path.name)
        number = valid_issue_number(match.group(1)) if match else None
        if number is not None:
            result.add(number)
    return result


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--base-dir", type=Path, help="Base for relative configured paths; defaults to config directory")
    ap.add_argument("--output-dir", type=Path, help="Override the archive output directory")
    ap.add_argument("--state-file", type=Path, help="Override the incremental state file")
    ap.add_argument("--repo", help="owner/name; restrict to one configured repo")
    ap.add_argument("--dry-run", action="store_true",
                    help="Read seeds and associated records as needed, without writing output or state")
    ap.add_argument("--full", action="store_true",
                    help="Ignore last_sync and refetch every known target")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        base_dir = (args.base_dir or args.config.resolve().parent).expanduser().resolve()
        output_value = args.output_dir or cfg.output_dir
        state_value = args.state_file or cfg.state_file
        if output_value is None or state_value is None:
            raise ValueError("output_dir and state_file are required in configuration or command arguments")
        out_root = (base_dir / Path(output_value).expanduser()).resolve()
        state_path = (base_dir / Path(state_value).expanduser()).resolve()
        package_root = Path(__file__).resolve().parents[1]
        if (out_root == package_root or package_root in out_root.parents
                or state_path == package_root or package_root in state_path.parents):
            raise ValueError("output and state must be outside this Skill package")
        if state_path == out_root or out_root in state_path.parents:
            raise ValueError("state_file must be outside the archive output directory")
        if args.repo and args.repo not in {repo.slug for repo in cfg.repos}:
            raise ValueError("selected repository is not in configuration")
        for repo in cfg.repos:
            configured_directory = out_root / repo.dir_slug
            if configured_directory.is_symlink():
                raise ValueError("repository directory must not be a symbolic link")
            directory = configured_directory.resolve()
            if directory.parent != out_root:
                raise ValueError("repository directory resolves outside the archive")
            if directory == package_root or package_root in directory.parents:
                raise ValueError("repository output must be outside this Skill package")
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        print("ERROR: invalid or unreadable configuration, or unsafe output/state paths", file=sys.stderr)
        return 2

    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            print("ERROR: unreadable or invalid state file", file=sys.stderr)
            return 2
        if not isinstance(state, dict):
            print("ERROR: state must be an object", file=sys.stderr)
            return 2

    sync_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    any_failure = False

    for rc in cfg.repos:
        if args.repo and args.repo != rc.slug:
            continue
        print(f"=== {rc.slug} ===", file=sys.stderr)

        existing_repo_state = state.get(rc.slug, {})
        if not isinstance(existing_repo_state, dict):
            existing_repo_state = {}
        last_sync_raw = existing_repo_state.get("last_sync")
        last_sync = (last_sync_raw if isinstance(last_sync_raw, str)
                     and not args.full else None)
        repo_dir = out_root / rc.dir_slug
        known_targets = (
            normalized_state_numbers(existing_repo_state.get("target_numbers"))
            | existing_target_numbers(repo_dir, cfg.filename_template)
        )
        excluded_numbers = normalized_state_numbers(
            existing_repo_state.get("excluded_numbers")
        )

        # 1. Discover seeds and their lightweight updated_at metadata. A seed
        # listing failure is fatal for this repository: advancing the cursor
        # would otherwise turn a transient empty result into permanent loss.
        try:
            seeds = collect_seeds(rc)
        except RuntimeError as e:
            print(f"  ERROR: seed discovery failed: {e}", file=sys.stderr)
            any_failure = True
            continue
        print(f"  seeds: {len(seeds)} unique issues/PRs", file=sys.stderr)

        all_targets = known_targets | set(seeds)

        # Determine work before fetching any comments. `seeds.all` already
        # returned updated_at for every issue/PR. Filtered seed repositories
        # additionally ask the Issues endpoint for changes to known closure
        # targets, avoiding one comments request per archived target.
        if last_sync is None:
            work_targets = set(all_targets)
        else:
            work_targets: set[int] = {
                number for number in known_targets
                if not (repo_dir / cfg.filename(number)).exists() and number not in excluded_numbers
            }
            for number, updated_at in seeds.items():
                target = repo_dir / cfg.filename(number)
                is_missing = not target.exists() and number not in excluded_numbers
                if (number not in known_targets or is_missing
                        or updated_at is None or updated_at >= last_sync):
                    work_targets.add(number)
            if not rc.seed_all and known_targets:
                try:
                    updated_targets = gh_all_items(rc.slug, since=last_sync)
                except RuntimeError as e:
                    print(f"  ERROR: incremental listing failed: {e}",
                          file=sys.stderr)
                    any_failure = True
                    continue
                work_targets |= known_targets & set(updated_targets)

        # 2. Expand closure from items that actually changed. Fetched issue
        # payloads are cached and reused by rendering, so each processed item
        # incurs at most one issue request and one paginated comments request.
        issue_cache: dict[int, dict | None] = {}
        repo_failed = False

        def get_issue(number: int) -> dict | None:
            nonlocal repo_failed
            if number not in issue_cache:
                issue_cache[number] = fetch_issue(rc.slug, number)
                if issue_cache[number] is None:
                    repo_failed = True
            return issue_cache[number]

        process_targets = set(work_targets)
        frontier = set(work_targets)
        # Seeds and pre-existing archive members are never discarded by the
        # closure cap. New cross-reference expansion may fill only the room up
        # to max_total; if an old state already exceeds it, expansion is frozen.
        target_cap = max(max(0, rc.closure_max_total), len(all_targets))
        for hop in range(rc.closure_depth):
            if not frontier:
                break
            print(f"  closure hop {hop+1}: expanding from {len(frontier)} issues",
                  file=sys.stderr)
            new_refs: set[int] = set()
            for num in sorted(frontier):
                issue = get_issue(num)
                if issue is None:
                    continue
                for ref_repo, ref_num in issue_crossrefs(issue, rc.slug):
                    if ref_repo == rc.slug and ref_num not in all_targets:
                        new_refs.add(ref_num)

            candidates = sorted(new_refs - all_targets)
            room = max(0, target_cap - len(all_targets))
            accepted = set(candidates[:room])
            dropped = len(candidates) - len(accepted)
            frontier = accepted
            all_targets |= frontier
            process_targets |= frontier
            print(f"    + {len(frontier)} new (total {len(all_targets)})",
                  file=sys.stderr)
            if dropped:
                print(f"  WARN: closure cap {rc.closure_max_total} dropped "
                      f"{dropped} new references", file=sys.stderr)
            if len(all_targets) >= target_cap:
                if frontier and hop + 1 < rc.closure_depth:
                    print(f"  WARN: hit closure_max_total "
                          f"{rc.closure_max_total}, stopping",
                          file=sys.stderr)
                break

        print(f"  total targets: {len(all_targets)}; "
              f"processing: {len(process_targets)}", file=sys.stderr)

        if args.dry_run:
            if repo_failed:
                any_failure = True
            print(f"  [dry-run] would render {len(process_targets)} issues/PRs",
                      file=sys.stderr)
            continue

        # 3. fetch + render
        repo_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        skipped = len(all_targets - process_targets)
        excluded = 0
        for num in sorted(process_targets):
            issue = get_issue(num)
            if issue is None:
                continue

            # Exclude rules
            issue_labels = {lbl["name"] for lbl in issue.get("labels", [])}
            if issue_labels & set(rc.exclude_labels):
                excluded += 1
                excluded_numbers.add(num)
                continue
            if rc.exclude_closed_before and issue.get("closed_at"):
                if issue["closed_at"] < rc.exclude_closed_before:
                    excluded += 1
                    excluded_numbers.add(num)
                    continue
            excluded_numbers.discard(num)

            # Compute related = closure refs from THIS issue
            refs = issue_crossrefs(issue, rc.slug)
            related_in_repo = sorted(
                {n for (r, n) in refs if r == rc.slug and n != num}
            )

            md = render_issue_md(issue, rc.slug, related_in_repo)
            target = repo_dir / cfg.filename(num)
            if target.is_symlink():
                print("  ERROR: refusing a symbolic-link output file", file=sys.stderr)
                repo_failed = True
                continue
            old = target.read_text(encoding="utf-8") if target.exists() else None
            if old != md:
                target.write_text(md, encoding="utf-8")
                written += 1
                if args.verbose:
                    print(f"    wrote {target.relative_to(out_root)}",
                          file=sys.stderr)
            else:
                skipped += 1

        print(f"  done: {written} written, {skipped} unchanged, "
              f"{excluded} excluded, {int(repo_failed)} failures",
              file=sys.stderr)

        if repo_failed:
            print("  ERROR: API failure; incremental cursor was not advanced",
                  file=sys.stderr)
            any_failure = True
            continue

        repo_state = state.setdefault(rc.slug, {})
        if not isinstance(repo_state, dict):
            repo_state = {}
            state[rc.slug] = repo_state
        repo_state["last_sync"] = sync_iso
        repo_state["last_target_count"] = len(all_targets)
        repo_state["target_numbers"] = sorted(all_targets)
        if excluded_numbers:
            repo_state["excluded_numbers"] = sorted(excluded_numbers)
        else:
            repo_state.pop("excluded_numbers", None)

    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent,
                                         prefix=".github-archive-", delete=False) as staged:
            staged_state = Path(staged.name)
            staged.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        try:
            os.replace(staged_state, state_path)
        finally:
            staged_state.unlink(missing_ok=True)
        print("state saved", file=sys.stderr)
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
