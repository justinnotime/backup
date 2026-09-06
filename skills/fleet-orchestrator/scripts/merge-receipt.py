#!/usr/bin/env python3
"""Inspect pull request readiness using head-specific review and test evidence."""


from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg

AUTO_REVIEW_MARKERS = tuple(cfg.get("github.automatic_review_markers", []))


def gh(args: list[str]) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return out.stdout.strip()


def real_reviews(rows: list[dict], head: str) -> list[str]:


    out = []
    for r in rows:
        if not (r.get("commit_id") or "").startswith(head):
            continue
        body = (r.get("body") or "").strip()
        if not body or any(marker in body for marker in AUTO_REVIEW_MARKERS):
            continue
        out.append(f"{r['user']['login']}:{r['state']}")
    return out


def overlap(pr_files: list[str], base_files: list[str]) -> list[str]:
    return sorted(set(pr_files) & set(base_files))


def receipt(repo: str, number: int, want_head: str | None) -> tuple[list[tuple], str]:
    v = json.loads(gh(["pr", "view", str(number), "-R", repo, "--json",
                       "headRefOid,headRefName,isDraft,baseRefName,title,url"]))
    head, branch, base = v["headRefOid"], v["headRefName"], v["baseRefName"]
    gates: list[tuple[str, bool, str]] = []

    gates.append(("head is the one that was reviewed",
                  want_head is None or head.startswith(want_head),
                  head if want_head is None else f"{head[:12]} vs expected {want_head[:12]}"))
    gates.append(("not a draft", not v["isDraft"], f"draft={v['isDraft']}"))

    mergeable, state = "null", "unknown"
    for _ in range(4):
        rest = json.loads(gh(["api", f"repos/{repo}/pulls/{number}"]) or "{}")
        if rest.get("mergeable") is not None:
            mergeable = str(rest["mergeable"]).lower()
            state = rest.get("mergeable_state", "unknown")
            break
        time.sleep(2)
    gates.append(("mergeable, computed via REST not cached",
                  mergeable == "true" and state in ("clean", "unstable"),
                  f"{mergeable}/{state}"))

    checks = [c.split("\t") for c in gh(["pr", "checks", str(number), "-R", repo]).splitlines()]
    bad = [c[0] for c in checks if len(c) > 1 and c[1] not in ("pass", "skipping")]
    gates.append((f"all {len(checks)} checks pass", not bad, ", ".join(bad) or "none pending or failing"))

    runs = gh(["run", "list", "-R", repo, "--branch", branch, "--limit", "40",
               "--json", "status,name,headSha", "-q",
               f'[.[]|select(.headSha=="{head}" and .status!="completed")]|map(.name)|join(", ")'])
    gates.append(("no incomplete workflow RUNS at this sha", not runs,
                  runs or "none (runs enumerated separately from check-runs)"))

    owner, name = repo.split("/", 1)
    q = (f'query={{repository(owner:"{owner}",name:"{name}")'
         f'{{pullRequest(number:{number}){{reviewThreads(last:100)'
         f'{{nodes{{isResolved}}}}}}}}}}')
    threads = gh(["api", "graphql", "-f", q, "-q",
                  "[.data.repository.pullRequest.reviewThreads.nodes[]"
                  "|select(.isResolved==false)]|length"])
    gates.append(("zero unresolved review threads", threads == "0", f"{threads or '?'} unresolved"))

    revs = json.loads(gh(["api", f"repos/{repo}/pulls/{number}/reviews", "--paginate"]) or "[]")
    real = real_reviews(revs, head)
    gates.append(("a real review at this head", bool(real),
                  ", ".join(real) or "NONE (auto-stamp and empty bodies excluded)"))

    cmp_ = json.loads(gh(["api", f"repos/{repo}/compare/{base}...{head}"]) or "{}")
    behind = cmp_.get("behind_by", 0)
    pr_files = [f["filename"] for f in json.loads(
        gh(["api", f"repos/{repo}/pulls/{number}/files", "--paginate"]) or "[]")]
    base_files = [f["filename"] for f in
                  json.loads(gh(["api", f"repos/{repo}/compare/{head}...{base}"]) or "{}").get("files", [])]
    clash = overlap(pr_files, base_files) if behind else []
    gates.append((f"base freshness ({behind} behind {base})", not clash,
                  "no file overlap with those commits" if not clash
                  else "OVERLAPS: " + ", ".join(clash[:5])))

    return gates, v["url"]


def self_test() -> int:
    global AUTO_REVIEW_MARKERS
    AUTO_REVIEW_MARKERS = ("synthetic automatic approval",)

    bad = 0
    rows = [{"commit_id": "abc123", "body": "synthetic automatic approval", "user": {"login": "bot"}, "state": "APPROVED"},
            {"commit_id": "abc123", "body": "  ", "user": {"login": "seat"}, "state": "COMMENTED"},
            {"commit_id": "def456", "body": "real verdict", "user": {"login": "seat"}, "state": "APPROVED"},
            {"commit_id": "abc123", "body": "real verdict", "user": {"login": "seat"}, "state": "APPROVED"}]
    got = real_reviews(rows, "abc123")
    ok = got == ["seat:APPROVED"]
    print(("OK    " if ok else f"FAIL  got {got}: ")
          + "auto-stamp, empty body and wrong-head reviews are all excluded")
    bad += not ok

    ok = real_reviews(rows, "zzz999") == []
    print(("OK    " if ok else "FAIL  ") + "a head nobody reviewed yields no review")
    bad += not ok

    ok = overlap(["a.py", "b.py"], ["b.py", "c.py"]) == ["b.py"]
    print(("OK    " if ok else "FAIL  ") + "file overlap is the intersection, not the count of either side")
    bad += not ok

    ok = overlap(["a.py"], ["b.py"]) == []
    print(("OK    " if ok else "FAIL  ") + "disjoint file sets overlap in nothing")
    bad += not ok

    print("---\n" + ("OK    every filter behaved" if not bad else f"FAIL  {bad} wrong"))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("number", nargs="?", type=int)
    ap.add_argument("--repo", help="repository as OWNER/NAME")
    ap.add_argument("--head", help="the sha a review was written against; "
                                   "gate 1 fails if the PR has moved off it")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.number is None:
        ap.error("a PR number is required")

    if not args.repo or len(args.repo.split("/")) != 2:
        ap.error("--repo OWNER/NAME is required")
    gates, url = receipt(args.repo, args.number, args.head)
    width = max(len(g[0]) for g in gates)
    for i, (label, good, detail) in enumerate(gates, 1):
        print(f"{'OK  ' if good else 'FAIL'} {i}. {label:<{width}}  {detail}")
    every = all(g[1] for g in gates)
    print("\n" + ("READY - every gate passed" if every
                  else "NOT READY - the FAIL lines above are the reason"))
    print(url)
    return 0 if every else 1


if __name__ == "__main__":
    sys.exit(main())
