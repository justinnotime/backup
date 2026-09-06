from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .issue_refs import canonical, required_refs, sanitize_explicit_ref_tokens, split_ref

FRONTMATTER_RE = re.compile("^([A-Za-z0-9_]+):\\s*(.*?)\\s*$", re.MULTILINE)
GITHUB_ISSUE_URL_PATTERN = "https?://github\\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/(?P<kind>issues|pull)/(?P<number>[1-9]\\d*)(?P<tail>(?:[/?#][A-Za-z0-9._~:/?#@!$&'*+,;=%-]*)?)(?![A-Za-z0-9_.-])"
GITHUB_ISSUE_URL_RE = re.compile(GITHUB_ISSUE_URL_PATTERN, re.IGNORECASE)
GITHUB_MARKDOWN_LINK_RE = re.compile(
    f"\\[(?P<label>[^\\]\\n]+)\\]\\((?P<url>{GITHUB_ISSUE_URL_PATTERN})\\)", re.IGNORECASE
)
OPTIONS = {}
ISSUE_DIRECTORY = "sources/issues"
ISSUE_HEADING = "### PRs / Issues"
FACTS_HEADING = "## Facts"
PROJECTS_HEADING = "## Projects"
EXTERNAL_GITHUB_REPLACEMENT = "related earlier work"
AGENT_WORK_HEADING = "### Agent work"
AGENT_WORK_HEADING_RE = re.compile(r"^#{2,3}[ \t]+Agent work(?:[ \t]+.*)?$", re.MULTILINE)
ISSUE_SECTION_RE = re.compile(
    r"^### PRs / Issues\s*$\n.*?(?=^###\s|^##\s|\Z)", re.MULTILINE | re.DOTALL
)


def configure(options, issue_directory="sources/issues"):
    global OPTIONS, ISSUE_DIRECTORY, ISSUE_HEADING, FACTS_HEADING, PROJECTS_HEADING
    global EXTERNAL_GITHUB_REPLACEMENT, AGENT_WORK_HEADING, AGENT_WORK_HEADING_RE, ISSUE_SECTION_RE
    OPTIONS = options
    ISSUE_DIRECTORY = issue_directory
    ISSUE_HEADING = options.get("heading", "### PRs / Issues")
    FACTS_HEADING = options.get("facts_heading", "## Facts")
    PROJECTS_HEADING = options.get("projects_heading", "## Projects")
    EXTERNAL_GITHUB_REPLACEMENT = options.get(
        "external_reference_replacement", "related earlier work"
    )
    AGENT_WORK_HEADING = options.get("agent_heading", "### Agent work")
    AGENT_WORK_HEADING_RE = re.compile(
        options.get("agent_heading_pattern", r"^#{2,3}[ \t]+Agent work(?:[ \t]+.*)?$"), re.MULTILINE
    )
    ISSUE_SECTION_RE = re.compile(
        "^" + re.escape(ISSUE_HEADING) + r"\s*$\n.*?(?=^###\s|^##\s|\Z)", re.MULTILINE | re.DOTALL
    )


SECTION_HEADING_RE = re.compile("^#{2,3}(?!#)[ \\t]+", re.MULTILINE)


def parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    return dict(FRONTMATTER_RE.findall(text[4:end]))


def one_line(value: object) -> str:
    return re.sub("\\s+", " ", str(value or "")).strip()


def mirror_metadata(root: Path, repo: str, number: int) -> dict[str, object]:
    owner, name = repo.split("/", 1)
    relative = Path(ISSUE_DIRECTORY) / f"{owner}_{name}" / f"{number}.md"
    path = root / relative
    fields = parse_frontmatter(path)
    is_pr = fields.get("type") == "gh-pull-request"
    return {
        "repo": repo,
        "number": number,
        "title": one_line(fields.get("title")),
        "state": one_line(fields.get("state")),
        "merged_at": one_line(fields.get("merged")),
        "url": fields.get("url")
        or f"https://github.com/{repo}/{('pull' if is_pr else 'issues')}/{number}",
        "file": relative.as_posix() if path.is_file() else "",
    }


def fact_metadata(facts: dict, ref: str) -> dict[str, object] | None:
    touched = facts.get("gh_touched_today", {})
    if not isinstance(touched, dict):
        return None
    value = touched.get(ref)
    return value if isinstance(value, dict) else None


def target_activity_label(metadata: dict[str, object]) -> str:
    raw = metadata.get("activity_on_target", [])
    activity = set(raw if isinstance(raw, list) else [])
    if "frontmatter:merged" in activity:
        return "MERGED"
    if "frontmatter:closed" in activity:
        return "closed"
    if activity & {"comment:created", "comment:edited"}:
        return "commented"
    if "frontmatter:created" in activity:
        return "created"
    return "updated"


def render_issue_section(facts: dict, root: Path) -> str:
    lines = [ISSUE_HEADING]
    for ref in required_refs(facts):
        repo, number = split_ref(ref)
        metadata = dict(mirror_metadata(root, repo, number))
        metadata.update(fact_metadata(facts, ref) or {})
        short_repo = repo.rsplit("/", 1)[-1]
        label = f"{short_repo}#{number}"
        url = str(metadata.get("url") or f"https://github.com/{repo}/issues/{number}")
        activity = target_activity_label(metadata)
        title = one_line(metadata.get("title")) or "GitHub activity on target date"
        source = one_line(metadata.get("file"))
        suffix = f" ([src]({source}))" if source else " (mirror unavailable)"
        lines.append(f"- [{label}]({url}) {activity} — {title}{suffix}")
    return "\n".join(lines).rstrip() + "\n"


def normalize_github_labels(markdown: str) -> str:

    def replace(match: re.Match[str]) -> str:
        repo = match.group("repo")
        number = match.group("number")
        return f"[{repo}#{int(number)}]({match.group('url')})"

    return GITHUB_MARKDOWN_LINK_RE.sub(replace, markdown)


def sanitize_external_github_references(markdown: str, facts: dict) -> str:
    section = ISSUE_SECTION_RE.search(markdown)
    if section is None:
        raise ValueError("generated summary is missing '### PRs / Issues'")
    strict_refs = set(required_refs(facts))

    def sanitize_fragment(fragment: str) -> str:

        def replace_markdown(match: re.Match[str]) -> str:
            ref = canonical(f"{match.group('owner')}/{match.group('repo')}", match.group("number"))
            return match.group(0) if ref in strict_refs else EXTERNAL_GITHUB_REPLACEMENT

        def replace_url(match: re.Match[str]) -> str:
            ref = canonical(f"{match.group('owner')}/{match.group('repo')}", match.group("number"))
            return match.group(0) if ref in strict_refs else EXTERNAL_GITHUB_REPLACEMENT

        fragment = GITHUB_MARKDOWN_LINK_RE.sub(replace_markdown, fragment)
        fragment = GITHUB_ISSUE_URL_RE.sub(replace_url, fragment)
        return sanitize_explicit_ref_tokens(fragment, strict_refs, EXTERNAL_GITHUB_REPLACEMENT)

    return (
        sanitize_fragment(markdown[: section.start()])
        + section.group(0)
        + sanitize_fragment(markdown[section.end() :])
    )


def human_clusters(facts: dict) -> list[dict]:
    clusters = facts.get("session_clusters", [])
    if not isinstance(clusters, list):
        return []
    return [
        cluster
        for cluster in clusters
        if isinstance(cluster, dict)
        and cluster.get("kind") == "human"
        and (int(cluster.get("n_real_prompts", 0) or 0) > 0)
    ]


def render_cluster_coverage_line(cluster: dict) -> str:
    time = one_line(cluster.get("time")) or "time unavailable"
    return f"- {time} — {int(cluster.get('n_sessions', 0) or 0)} sessions · {int(cluster.get('n_real_prompts', 0) or 0)} prompts · {int(cluster.get('messages', 0) or 0)} msgs"


def missing_cluster_coverage_lines(agent_work: str, facts: dict) -> list[str]:
    lines = []
    for cluster in human_clusters(facts):
        time = str(cluster.get("time", ""))
        anchor = time[:5]
        if anchor and anchor not in agent_work:
            lines.append(render_cluster_coverage_line(cluster))
    return lines


def render_agent_work_skeleton(facts):
    clusters = facts.get("session_clusters", [])
    if not isinstance(clusters, list):
        clusters = []
    human = human_clusters(facts)
    counts = {
        "human_count": len(human),
        "session_count": sum(int(item.get("n_sessions", 0) or 0) for item in human),
        "prompt_count": sum(int(item.get("n_real_prompts", 0) or 0) for item in human),
        "machine_count": sum(
            1 for item in clusters if isinstance(item, dict) and item.get("kind") != "human"
        ),
    }
    template = (
        OPTIONS.get(
            "agent_summary_template",
            "- Facts: {human_count} human clusters / {session_count} sessions / {prompt_count} prompts; {machine_count} automated clusters excluded.",
        )
        if human
        else OPTIONS.get(
            "empty_agent_template",
            "- No human prompts recorded; {machine_count} automated clusters excluded.",
        )
    )
    lines = [AGENT_WORK_HEADING, "", template.format(**counts)]
    lines.extend(render_cluster_coverage_line(cluster) for cluster in human)
    return "\n".join(lines).rstrip() + "\n"


def install_agent_work_section(markdown: str, facts: dict) -> str:
    existing = AGENT_WORK_HEADING_RE.search(markdown)
    if existing:
        following = markdown[existing.end() :]
        next_heading = SECTION_HEADING_RE.search(following)
        body_end = existing.end() + (next_heading.start() if next_heading else len(following))
        body = markdown[existing.end() : body_end]
        coverage = missing_cluster_coverage_lines(body, facts)
        if coverage:
            body = body.rstrip() + "\n\n" + "\n".join(coverage) + "\n\n"
        result = (
            markdown[: existing.start()]
            + AGENT_WORK_HEADING
            + body
            + markdown[body_end:].lstrip("\n")
        )
        return result.rstrip() + "\n"
    projects_heading = re.search(
        "^" + re.escape(PROJECTS_HEADING) + r"[ \t]*$", markdown, re.MULTILINE
    )
    if not projects_heading:
        raise ValueError("generated summary is missing projects heading")
    section = render_agent_work_skeleton(facts).rstrip()
    result = (
        markdown[: projects_heading.start()].rstrip()
        + "\n\n"
        + section
        + "\n\n"
        + markdown[projects_heading.start() :].lstrip("\n")
    )
    return result.rstrip() + "\n"


def install_issue_section(markdown: str, section: str) -> str:
    markdown = normalize_github_labels(markdown)
    section = section.rstrip()
    existing = ISSUE_SECTION_RE.search(markdown)
    if existing:
        result = (
            markdown[: existing.start()]
            + section
            + "\n\n"
            + markdown[existing.end() :].lstrip("\n")
        )
    else:
        facts_heading = re.search(
            "^" + re.escape(FACTS_HEADING) + r"[ \t]*$", markdown, re.MULTILINE
        )
        if not facts_heading:
            raise ValueError("generated summary is missing facts heading")
        insert_at = facts_heading.end()
        result = markdown[:insert_at] + "\n\n" + section + "\n" + markdown[insert_at:].lstrip("\n")
    return result.rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("facts", type=Path)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--install", type=Path)
    from .config import DEFAULT_CONFIG, activate, load

    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    activate(load(args.config, args.repo_root))
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    section = render_issue_section(facts, args.repo_root)
    if args.install is None:
        print(section, end="")
        return 0
    markdown = args.install.read_text(encoding="utf-8")
    markdown = install_issue_section(markdown, section)
    markdown = install_agent_work_section(markdown, facts)
    markdown = sanitize_external_github_references(markdown, facts)
    args.install.write_text(markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
