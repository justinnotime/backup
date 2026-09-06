"""Build graphs, activity timelines and inventories from an explicit local issue archive.

No network or language-model calls are made.
"""

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "error: PyYAML not installed (apt install python3-yaml or pip install pyyaml)\n"
    )
    sys.exit(2)


def load_issues(repo_dir: Path) -> dict[int, dict]:
    """Parse all .md files under repo_dir; return {number: frontmatter-dict}."""
    out = {}
    for p in sorted(repo_dir.glob("*.md")):
        text = p.read_text()
        m = re.match("^---\\n(.*?)\\n---", text, re.DOTALL)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        num = fm.get("number")
        if num is None:
            continue
        out[int(num)] = {
            "title": fm.get("title", "") or "",
            "state": fm.get("state", "") or "",
            "labels": fm.get("labels") or [],
            "related": [
                int(r)
                for r in fm.get("related") or []
                if isinstance(r, int) or (isinstance(r, str) and r.isdigit())
            ],
            "created": str(fm.get("created", "")),
            "closed": str(fm.get("closed", "") or ""),
            "type": fm.get("type", "gh-issue"),
            "author": fm.get("author", ""),
        }
    return out


def filter_relevant(
    issues: dict[int, dict],
    labels: list[str],
    title_include: re.Pattern | None,
    title_exclude: re.Pattern | None,
) -> dict[int, dict]:
    """Keep issues that match the label OR title-include filter (and don't hit exclude)."""
    out = {}
    for n, i in issues.items():
        title_lower = i["title"].lower()
        if title_exclude and title_exclude.search(title_lower):
            continue
        keep = False
        if labels and any((lbl in (i["labels"] or []) for lbl in labels)):
            keep = True
        if title_include and title_include.search(title_lower):
            keep = True
        if not labels and (not title_include):
            keep = True
        if keep:
            out[n] = i
    return out


def auto_detect_trackers(issues: dict[int, dict], min_outdeg: int = 10) -> list[int]:
    """Heuristic: an issue is a tracker if title contains [tracker]/[master]
    OR has a 'tracker' label OR refers to ≥ min_outdeg other issues in this set."""
    found = set()
    for n, i in issues.items():
        title_lower = i["title"].lower()
        if "[tracker]" in title_lower or "[master]" in title_lower:
            found.add(n)
        if "tracker" in (i["labels"] or []) or "master" in (i["labels"] or []):
            found.add(n)
        if len([r for r in i["related"] if r in issues]) >= min_outdeg:
            found.add(n)
    return sorted(found)


def assign_tier(issue: dict) -> str:
    """Classify archived type and state: A closed PR, B closed issue, C open, D other. A closed PR is not necessarily merged."""
    if issue["type"] == "gh-pull-request" and issue["state"] == "closed":
        return "A"
    if issue["type"] == "gh-issue" and issue["state"] == "closed":
        return "B"
    if issue["state"] == "open":
        return "C"
    return "D"


def parse_dt(s: str):
    if not s or s == "None":
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def clean_tracker_title(title: str, max_len: int = 40) -> str:
    """Strip noise from tracker titles for use as cluster labels.

    Removes: leading `[tag]` brackets, common project prefixes that look
    like `prefix:` or `prefix-foo:`, trailing `(master ...)` / `(release-blocker ...)`
    parenthetical noise. Truncates to max_len.
    """
    s = title
    while True:
        m = re.match("^\\s*\\[[^\\]]+\\]\\s*", s)
        if not m:
            break
        s = s[m.end() :]
    s = re.sub("^[a-z][\\w\\-]+(?:[\\s/][\\w\\-]+)?:\\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(
        "\\s*\\((master[^)]*|release-blocker[^)]*|中文[^)]*)\\)\\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = s.strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def dot_label(text: str) -> str:
    """Keep issue titles inside one quoted DOT label."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', "'")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def build_full_dot(
    issues: dict[int, dict],
    trackers: list[int],
    closed_per_tracker: int = 6,
    top_cross: int = 8,
) -> str:
    """Clustered DOT — one cluster per tracker + all open children + top-N
    closed children + a separate cluster of cross-tracker bridge issues."""
    parent = defaultdict(set)
    for t in trackers:
        if t not in issues:
            continue
        for r in issues[t].get("related", []):
            if r in issues:
                parent[r].add(t)
    in_deg = defaultdict(int)
    for n, i in issues.items():
        for r in i.get("related", []):
            if r in issues:
                in_deg[r] += 1

    def primary(n):
        if n in trackers or not parent.get(n):
            return None
        tracker_set = parent[n]
        if not tracker_set:
            return None
        return min(tracker_set)

    children = defaultdict(list)
    for n in issues:
        if n in trackers:
            continue
        if len(parent.get(n, [])) >= 2:
            continue
        p = primary(n)
        if p is not None:
            children[p].append(n)
    cross = {n: parent[n] for n in parent if len(parent[n]) >= 2 and n not in trackers}
    palette = [
        "#fef3c7",
        "#dbeafe",
        "#fee2e2",
        "#fce7f3",
        "#dcfce7",
        "#f3e8ff",
        "#ffedd5",
        "#e5e7eb",
    ]
    out = []
    out.append("digraph issue_graph {")
    out.append(
        '  graph [rankdir=LR, fontname="Helvetica", fontsize=11, compound=true, ranksep=0.6, nodesep=0.25, label="Issue dependency graph — closed=green fill, open=amber fill", labelloc="t", labeljust="l"];'
    )
    out.append(
        '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=9, margin="0.06,0.04"];'
    )
    out.append(
        '  edge [fontname="Helvetica", fontsize=8, color="#94a3b8", arrowsize=0.6];'
    )
    out.append("")
    for idx, tid in enumerate(trackers):
        if tid not in issues:
            continue
        info = issues[tid]
        state = info["state"]
        color = palette[idx % len(palette)]
        head = dot_label(clean_tracker_title(info["title"], max_len=50))
        out.append(f"  subgraph cluster_{tid} {{")
        out.append(
            f'    label="#{tid} {head}"; style="rounded,filled"; fillcolor="{color}"; color="#64748b";'
        )
        shape = "doublecircle" if state == "open" else "doubleoctagon"
        fill = "#facc15" if state == "open" else "#cbd5e1"
        out.append(
            f'    "{tid}" [label="#{tid}\\n[tracker]", shape={shape}, fillcolor="{fill}", penwidth=2];'
        )
        open_kids = [n for n in children[tid] if issues[n]["state"] == "open"]
        closed_kids = sorted(
            [n for n in children[tid] if issues[n]["state"] == "closed"],
            key=lambda x: -in_deg[x],
        )[:closed_per_tracker]
        for n in open_kids:
            t = dot_label(issues[n]["title"][:36])
            out.append(
                f'    "{n}" [label="#{n}\\n{t}", fillcolor="#fde68a", color="#b45309", penwidth=1.5];'
            )
        for n in closed_kids:
            t = dot_label(issues[n]["title"][:36])
            out.append(
                f'    "{n}" [label="#{n}\\n{t}", fillcolor="#d1fae5", color="#065f46"];'
            )
        out.append("  }")
        out.append("")
    bridges = sorted(cross.items(), key=lambda kv: -len(kv[1]))[:top_cross]
    if bridges:
        out.append("  subgraph cluster_cross {")
        out.append(
            '    label="Cross-tracker bridges (load-bearing PRs)"; style="rounded,dashed,filled"; fillcolor="#fafaf9"; color="#a8a29e";'
        )
        for n, ts in bridges:
            t = dot_label(issues[n]["title"][:34])
            state = issues[n]["state"]
            fill = "#fde68a" if state == "open" else "#e0e7ff"
            out.append(
                f'    "{n}" [label="#{n}\\n{t}\\n[{len(ts)} trackers]", fillcolor="{fill}", color="#3730a3"];'
            )
        out.append("  }")
        out.append("")
    for t in trackers:
        if t not in issues:
            continue
        for r in issues[t].get("related", []):
            if r in trackers and r != t:
                out.append(
                    f'  "{t}" -> "{r}" [penwidth=2, color="#1e40af", arrowsize=0.9];'
                )
    for t in trackers:
        for n in children[t]:
            if issues[n]["state"] == "open":
                out.append(f'  "{t}" -> "{n}" [color="#b45309"];')
    for n, ts in bridges:
        for t in ts:
            if t in trackers:
                out.append(
                    f'  "{t}" -> "{n}" [style=dashed, color="#6366f1", arrowsize=0.5];'
                )
    out.append("}")
    return "\n".join(out)


def build_spine_dot(issues: dict[int, dict], trackers: list[int]) -> str:
    """Just the trackers + their inter-tracker `related:` edges."""
    out = []
    out.append("digraph tracker_spine {")
    out.append("  rankdir=TB;")
    out.append("  node [shape=box];")
    for tid in trackers:
        if tid not in issues:
            continue
        clean = dot_label(clean_tracker_title(issues[tid]["title"], max_len=34))
        out.append(f'  "{tid}" [label="#{tid}\\n{clean}"];')
    out.append("")
    for t in trackers:
        if t not in issues:
            continue
        for r in issues[t].get("related", []):
            if r in trackers and r != t:
                out.append(f'  "{t}" -> "{r}";')
    out.append("}")
    return "\n".join(out)


DENSITY_CHARS = [(0, " "), (1, "·"), (3, "▪"), (6, "▴"), (10, "▮")]


def density(n: int) -> str:
    for thresh, ch in DENSITY_CHARS:
        if n <= thresh:
            return ch
    return "█"


def build_timeline(
    issues: dict[int, dict],
    trackers: list[int],
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    label_width: int = 26,
) -> str:
    """ASCII swim-lane: tracker × day, opens on top, closes on bottom."""
    parent = defaultdict(set)
    for t in trackers:
        if t not in issues:
            continue
        for r in issues[t].get("related", []):
            if r in issues:
                parent[r].add(t)

    def attribution(n):
        if n in trackers:
            return [n]
        return list(parent.get(n, []))

    if start is None or end is None:
        active = []
        for n, i in issues.items():
            for s in (parse_dt(i.get("created")), parse_dt(i.get("closed"))):
                if s and s.year >= 2000:
                    active.append(s)
        if not active:
            return "(no issues with parseable dates)"
        if start is None:
            start = min(active)
            start = start - datetime.timedelta(days=start.weekday())
        if end is None:
            end = max(active)
    days = (end - start).days + 1
    if days <= 0 or days > 366:
        return f"(date range invalid or too large: {start} → {end} = {days} days)"
    events = defaultdict(lambda: defaultdict(lambda: {"open": 0, "close": 0}))
    for n, i in issues.items():
        for tid in attribution(n):
            o = parse_dt(i.get("created"))
            c = parse_dt(i.get("closed"))
            if o and start <= o <= end:
                events[tid][(o - start).days]["open"] += 1
            if c and start <= c <= end:
                events[tid][(c - start).days]["close"] += 1
    wk_chars = [" "] * days
    for d in range(days):
        date = start + datetime.timedelta(days=d)
        if date.weekday() == 0:
            for k, ch in enumerate(date.strftime("%m-%d")):
                if d + k < days:
                    wk_chars[d + k] = ch
    week_line = "".join(wk_chars)
    dow_line = "".join(
        ("MTWTFSS"[(start + datetime.timedelta(days=d)).weekday()] for d in range(days))
    )

    def short_label(tid):
        title = issues.get(tid, {}).get("title", "")
        return clean_tracker_title(title, max_len=label_width - 8)

    out = []
    out.append(f"Issue cadence — daily swim-lane, {start} → {end} ({days} days)")
    out.append(
        "Each cell = events that day attributed to a tracker (= issue referenced by it)."
    )
    out.append(
        "Density: ·=1  ▪=2-3  ▴=4-6  ▮=7-10  █=11+   Top of pair = opens (+), bottom = closes (−)"
    )
    out.append("")
    out.append(" " * label_width + week_line)
    out.append(" " * label_width + dow_line)
    out.append(" " * label_width + "─" * days)
    for tid in trackers:
        if tid not in issues:
            continue
        lab = short_label(tid)
        prefix_open = f"  #{tid} {lab:<{label_width - 8}}"
        prefix_close = " " * len(prefix_open)
        op_row = prefix_open + " +│"
        cl_row = prefix_close + " −│"
        for d in range(days):
            ev = events[tid].get(d, {"open": 0, "close": 0})
            op_row += density(ev["open"])
            cl_row += density(ev["close"])
        op_row += "│"
        cl_row += "│"
        out.append(op_row)
        out.append(cl_row)
    out.append(" " * label_width + "─" * days)
    op_total = "".join(
        (
            density(sum((events[t].get(d, {}).get("open", 0) for t in trackers)))
            for d in range(days)
        )
    )
    cl_total = "".join(
        (
            density(sum((events[t].get(d, {}).get("close", 0) for t in trackers)))
            for d in range(days)
        )
    )
    indent_len = 2 + 1 + 5 + 1 + (label_width - 8)
    out.append(f"  ALL{' ' * (indent_len - 5)} +│{op_total}│")
    out.append(f"{' ' * indent_len} −│{cl_total}│")
    return "\n".join(out)


def print_stats(issues, trackers):
    parent = defaultdict(set)
    for t in trackers:
        if t not in issues:
            continue
        for r in issues[t].get("related", []):
            if r in issues:
                parent[r].add(t)
    states = defaultdict(int)
    tiers = defaultdict(int)
    for n, i in issues.items():
        states[i["state"]] += 1
        tiers[assign_tier(i)] += 1
    print(f"Issues: {len(issues)} total | states: {dict(states)}")
    print(
        f"Tiers: A(merged-PR)={tiers['A']}  B(closed-issue)={tiers['B']}  C(open)={tiers['C']}  D(other)={tiers['D']}"
    )
    print(f"Trackers: {len(trackers)} → {trackers}")
    print()
    print(f"{'Tracker':>8}  {'Closed':>6}  {'Open':>4}  Title")
    for tid in trackers:
        if tid not in issues:
            continue
        op = sum(
            (
                1
                for r in issues[tid].get("related", [])
                if r in issues and issues[r]["state"] == "open"
            )
        )
        cl = sum(
            (
                1
                for r in issues[tid].get("related", [])
                if r in issues and issues[r]["state"] == "closed"
            )
        )
        print(f"  #{tid}  {cl:>6}  {op:>4}  {issues[tid]['title'][:70]}")
    print()
    cross = [n for n, p in parent.items() if len(p) >= 2 and n not in trackers]
    print(f"Cross-tracker bridges (issues touching ≥2 trackers): {len(cross)}")


def local_path(value: str) -> Path:
    """Expand only the caller's explicitly supplied local path."""
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", required=True, help="Directory of archived issue Markdown files"
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Match this label; repeatable, OR with title-include",
    )
    parser.add_argument(
        "--title-include", default="", help="Case-insensitive title inclusion regex"
    )
    parser.add_argument(
        "--title-exclude",
        default="",
        help="Case-insensitive title exclusion regex, applied first",
    )
    parser.add_argument(
        "--trackers",
        default="",
        help="Comma-separated tracker IDs; otherwise auto-detect",
    )
    parser.add_argument(
        "--tracker-min-outdeg",
        type=int,
        default=10,
        help="Auto-detect trackers referencing at least this many selected issues",
    )
    parser.add_argument("--start", default="", help="Timeline start date, YYYY-MM-DD")
    parser.add_argument("--end", default="", help="Timeline end date, YYYY-MM-DD")
    parser.add_argument(
        "--out-dir", default="", help="Explicit artifact directory; omit for stdout"
    )
    parser.add_argument(
        "--prefix",
        default=datetime.date.today().isoformat(),
        help="Output filename prefix; default is today's date",
    )
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "full-dot", "spine", "timeline", "inventory", "stats"],
    )
    parser.add_argument("--top-cross", type=int, default=8)
    parser.add_argument("--closed-per-tracker", type=int, default=6)
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational stderr output",
    )
    return parser.parse_args(argv)


def write_artifact(path: Path, content: str) -> None:
    """Replace one generated artifact atomically without following a file symlink."""
    if path.is_symlink():
        raise ValueError("output-file-symlink-refused")
    fd, temporary = tempfile.mkstemp(prefix=".issue-graph-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _main(argv=None):
    args = parse_args(argv)
    input_dir = local_path(args.input_dir)
    if not input_dir.is_dir():
        raise ValueError("input directory does not exist")
    if (
        not args.prefix
        or args.prefix in {".", ".."}
        or "/" in args.prefix
        or "\\" in args.prefix
    ):
        raise ValueError("prefix must be a single filename component")
    if min(args.top_cross, args.closed_per_tracker, args.tracker_min_outdeg) < 0:
        raise ValueError("graph limits must not be negative")
    title_inc = (
        re.compile(args.title_include, re.IGNORECASE) if args.title_include else None
    )
    title_exc = (
        re.compile(args.title_exclude, re.IGNORECASE) if args.title_exclude else None
    )
    start = datetime.date.fromisoformat(args.start) if args.start else None
    end = datetime.date.fromisoformat(args.end) if args.end else None

    def info(message):
        if not args.quiet:
            print(message, file=sys.stderr)

    info(f"loading {input_dir} …")
    raw = load_issues(input_dir)
    info(f"  parsed {len(raw)} issues")
    issues = filter_relevant(raw, args.label, title_inc, title_exc)
    info(
        f"  filtered to {len(issues)} relevant issues (labels={args.label or '∅'} title-include={args.title_include or '∅'})"
    )
    if args.trackers:
        trackers = sorted(
            int(value) for value in args.trackers.split(",") if value.strip()
        )
        if any(value < 1 for value in trackers):
            raise ValueError("tracker IDs must be positive")
        info(f"  using {len(trackers)} explicit trackers: {trackers}")
    else:
        trackers = auto_detect_trackers(issues, min_outdeg=args.tracker_min_outdeg)
        info(f"  auto-detected {len(trackers)} trackers: {trackers}")
    if args.mode == "stats":
        print_stats(issues, trackers)
        return 0

    artifacts = {}
    if args.mode in {"all", "full-dot"}:
        artifacts[f"{args.prefix}-issue-graph.dot"] = build_full_dot(
            issues,
            trackers,
            closed_per_tracker=args.closed_per_tracker,
            top_cross=args.top_cross,
        )
    if args.mode in {"all", "spine"}:
        artifacts[f"{args.prefix}-tracker-spine.dot"] = build_spine_dot(
            issues, trackers
        )
    if args.mode in {"all", "timeline"}:
        artifacts[f"{args.prefix}-timeline.txt"] = build_timeline(
            issues, trackers, start=start, end=end
        )
    if args.mode in {"all", "inventory"}:
        inventory = {
            str(number): {**issue, "tier": assign_tier(issue)}
            for number, issue in issues.items()
        }
        artifacts[f"{args.prefix}-issues.json"] = json.dumps(
            inventory, indent=2, ensure_ascii=False
        )
    output_dir = local_path(args.out_dir) if args.out_dir else None
    if output_dir:
        for name in artifacts:
            if (output_dir / name).is_symlink():
                raise ValueError("output-file-symlink-refused")
        output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        if output_dir:
            write_artifact(output_dir / name, content)
            info(f"  wrote {output_dir / name}")
        else:
            print(f"\n# ─── {name} ─────────────────────────────────────────────────")
            print(content)
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (OSError, ValueError, TypeError, re.error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
