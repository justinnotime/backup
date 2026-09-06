#!/usr/bin/env python3
"""Find mergeable pull requests without substantive review at their current head."""


from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg

ORG = cfg.get("github.owner", "")
AUTO_REVIEW_MARKERS = tuple(cfg.get("github.automatic_review_markers", []))


SURFACE = tuple(cfg.get("github.path_substrings", []))
WHOLE_REPOS = tuple(cfg.get("github.whole_repositories", []))
MIXED_REPOS = tuple(cfg.get("github.mixed_repositories", []))
PR_LIST_LIMIT = 500


def gh(args: list[str]) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return out.stdout.strip()


def ours(repo: str, number: int) -> bool:
    if repo in WHOLE_REPOS:
        return True
    paths = gh(["pr", "view", str(number), "-R", f"{ORG}/{repo}",
                "--json", "files", "-q", ".files[].path"]).splitlines()
    return any(s in p for p in paths for s in SURFACE)


def open_prs(repo: str) -> list[int]:


    raw = gh(["pr", "list", "-R", f"{ORG}/{repo}", "--state", "open",
              "--limit", str(PR_LIST_LIMIT), "--json", "number,isDraft"])
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if len(rows) >= PR_LIST_LIMIT:
        print(f"FAIL  {repo}: hit the {PR_LIST_LIMIT}-PR listing cap, so this run "
              f"CANNOT claim to have examined every open PR - raise PR_LIST_LIMIT")
    return [r["number"] for r in rows if not r["isDraft"]]


def substantive_reviews(repo: str, number: int, head: str) -> list[str]:


    raw = gh(["api", f"repos/{ORG}/{repo}/pulls/{number}/reviews", "--paginate"])
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for r in rows:
        if r.get("commit_id") != head:
            continue
        body = (r.get("body") or "").strip()
        if not body or any(marker in body for marker in AUTO_REVIEW_MARKERS):
            continue
        out.append(f"{r['user']['login']} {r['state']}")
    return out


def inspect(repo: str, number: int) -> dict | None:
    view = gh(["pr", "view", str(number), "-R", f"{ORG}/{repo}", "--json",
               "headRefOid,headRefName,title,updatedAt"])
    try:
        v = json.loads(view)
    except json.JSONDecodeError:
        return None
    head, branch = v["headRefOid"], v["headRefName"]


    mergeable, state = "unknown", "unknown"
    for _ in range(4):
        rest = gh(["api", f"repos/{ORG}/{repo}/pulls/{number}",
                   "-q", '"\\(.mergeable) \\(.mergeable_state)"']).split()
        if rest and rest[0] != "null":
            mergeable, state = rest[0], (rest[1] if len(rest) > 1 else "unknown")
            break
        time.sleep(2)

    checks = gh(["pr", "checks", str(number), "-R", f"{ORG}/{repo}"]).splitlines()
    failing = [c.split("\t")[0] for c in checks
               if len(c.split("\t")) > 1 and c.split("\t")[1] not in ("pass", "skipping")]

    runs = gh(["run", "list", "-R", f"{ORG}/{repo}", "--branch", branch, "--limit", "25",
               "--json", "status,name", "-q",
               '[.[]|select(.status!="completed")]|map(.name)|join(", ")'])


    q = (f'query={{repository(owner:"{ORG}",name:"{repo}")'
         f'{{pullRequest(number:{number}){{reviewThreads(last:100)'
         f'{{nodes{{isResolved}}}}}}}}}}')
    threads = gh(["api", "graphql", "-f", q,
                  "-q", "[.data.repository.pullRequest.reviewThreads.nodes[]"
                        "|select(.isResolved==false)]|length"])

    return {
        "repo": repo, "number": number, "title": v["title"][:58], "head": head,
        "mergeable": mergeable, "state": state, "failing": failing,
        "incomplete_runs": runs, "unresolved": threads or "?",
        "reviews": substantive_reviews(repo, number, head),
        "updated": v["updatedAt"][5:16],
    }


def is_green_and_unread(r: dict) -> bool:

    green = (r["mergeable"] == "true" and not r["failing"]
             and not r["incomplete_runs"] and r["unresolved"] == "0")
    return green and not r["reviews"]


def self_test() -> int:
    global AUTO_REVIEW_MARKERS
    AUTO_REVIEW_MARKERS = ("synthetic automatic approval",)


    base = {"mergeable": "true", "failing": [], "incomplete_runs": "",
            "unresolved": "0", "reviews": []}
    cases = [
        (True, "green and unread fires", base),
        (False, "a substantive review suppresses it", {**base, "reviews": ["x APPROVED"]}),
        (False, "a failing check suppresses it", {**base, "failing": ["lint"]}),
        (False, "an incomplete run suppresses it", {**base, "incomplete_runs": "Validate"}),
        (False, "an unresolved thread suppresses it", {**base, "unresolved": "1"}),
        (False, "unknown mergeability suppresses it", {**base, "mergeable": "null"}),
        (False, "an unreadable thread count suppresses it", {**base, "unresolved": "?"}),
    ]
    bad = 0
    for want, label, row in cases:
        got = is_green_and_unread(row)
        print(("OK    " if got == want else "FAIL  ") + label)
        bad += got != want


    rows = [{"commit_id": "h", "body": "synthetic automatic approval", "user": {"login": "bot"}, "state": "APPROVED"},
            {"commit_id": "h", "body": "   ", "user": {"login": "seat"}, "state": "COMMENTED"},
            {"commit_id": "other", "body": "real verdict", "user": {"login": "seat"}, "state": "APPROVED"},
            {"commit_id": "h", "body": "real verdict", "user": {"login": "seat"}, "state": "APPROVED"}]
    kept = [r for r in rows
            if r["commit_id"] == "h" and (r["body"] or "").strip()
            and not any(marker in (r["body"] or "") for marker in AUTO_REVIEW_MARKERS)]
    ok = len(kept) == 1 and kept[0]["user"]["login"] == "seat"
    print(("OK    " if ok else "FAIL  ") + "auto-stamp, empty body and wrong-head reviews all excluded")
    bad += not ok

    print("---\n" + ("OK    every guard behaved" if not bad else f"FAIL  {bad} case(s) wrong"))
    return 1 if bad else 0


def main() -> int:
    global ORG
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner", default=ORG, help="GitHub repository owner")
    ap.add_argument("--repo", action="append", default=[], help="repository name; repeatable")
    ap.add_argument("--all", action="store_true",
                    help="print every examined PR, not only the unread-but-green ones")
    ap.add_argument("--line", action="store_true",
                    help="restrict mixed repositories to configured path substrings")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the detector fires and each guard suppresses it; no API calls")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    ORG = args.owner
    repositories = args.repo or list(WHOLE_REPOS + MIXED_REPOS)
    if not ORG or not repositories:
        ap.error("--owner and --repo, or configured GitHub owner and repositories, are required")
    if args.line and any(repo not in WHOLE_REPOS for repo in repositories) and not SURFACE:
        ap.error("--line requires github.path_substrings for mixed repositories")
    rows = []
    for repo in repositories:
        numbers = open_prs(repo)
        examined = 0
        for n in numbers:
            if args.line and not ours(repo, n):
                continue
            info = inspect(repo, n)
            if info:
                rows.append(info)
                examined += 1


        if args.line and examined < len(numbers):
            print(f"NOTE  {repo}: {len(numbers) - examined} open PR(s) skipped by --line "
                  f"(paths matched none of {', '.join(SURFACE)})")

    flagged = 0
    for r in sorted(rows, key=lambda x: (x["repo"], x["number"])):
        if is_green_and_unread(r):
            flagged += 1
            print(f"FLAG  {r['repo']}#{r['number']} looks ready and nobody has read it")
            print(f"      head {r['head'][:12]} | {r['state']} | updated {r['updated']}")
            print(f"      {r['title']}")
        elif args.all:
            why = []
            if r["failing"]:
                why.append("checks " + ",".join(r["failing"]))
            if r["incomplete_runs"]:
                why.append("runs " + r["incomplete_runs"])
            if r["unresolved"] != "0":
                why.append(f"{r['unresolved']} unresolved")
            if r["mergeable"] != "true":


                why.append(f"mergeable={r['mergeable']}")
            if r["reviews"]:
                why.append("read by " + "; ".join(r["reviews"]))
            print(f"OK    {r['repo']}#{r['number']} {r['title']}")
            print(f"      {' | '.join(why) or 'green and read'}")

    print(f"---\n{'FAIL' if flagged else 'OK'}  {len(rows)} selected PR(s) examined, "
          f"{flagged} green with no substantive review at head")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
