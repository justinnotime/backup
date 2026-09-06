#!/usr/bin/env python3
"""Classify incomplete pull request checks and explicitly rerun selected failed jobs."""


from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg

ORG = cfg.get("github.owner", "")
AUTO_REVIEW_MARKERS = tuple(cfg.get("github.automatic_review_markers", []))
PR_LIST_LIMIT = 500


def gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=False).stdout.strip()


def jsonl(raw: str, default):
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return default


def open_prs(repo: str) -> list[int]:
    rows = jsonl(gh(["pr", "list", "-R", f"{ORG}/{repo}", "--state", "open",
                     "--limit", str(PR_LIST_LIMIT), "--json", "number,isDraft"]), [])
    if len(rows) >= PR_LIST_LIMIT:
        print(f"FAIL  {repo}: hit the {PR_LIST_LIMIT} listing cap - this run cannot "
              f"claim to have seen every open PR")
    return [r["number"] for r in rows if not r["isDraft"]]


def classify(repo: str, number: int) -> dict:
    v = jsonl(gh(["pr", "view", str(number), "-R", f"{ORG}/{repo}", "--json",
                  "headRefOid,headRefName,title"]), None)
    if not v:
        return {"number": number, "klass": "UNKNOWN", "detail": "pr view failed"}
    head, branch = v["headRefOid"], v["headRefName"]
    out = {"repo": repo, "number": number, "head": head, "branch": branch,
           "title": v["title"][:56]}

    runs = [r for r in jsonl(gh(["run", "list", "-R", f"{ORG}/{repo}", "--branch", branch,
                                 "--limit", "40", "--json",
                                 "status,conclusion,name,headSha,databaseId"]), [])
            if r["headSha"] == head]
    if any(r["status"] != "completed" for r in runs):
        names = ", ".join(r["name"] for r in runs if r["status"] != "completed")
        return {**out, "klass": "RUNNING", "detail": names}

    cancelled = [r for r in runs if r["conclusion"] == "cancelled"]
    failed = [r for r in runs if r["conclusion"] == "failure"]


    if failed:
        return {**out, "klass": "REAL",
                "detail": ", ".join(r["name"] for r in failed),
                "run_ids": [r["databaseId"] for r in failed]}
    if cancelled:
        jobs = []
        for r in cancelled:
            for j in jsonl(gh(["api", f"repos/{ORG}/{repo}/actions/runs/"
                               f"{r['databaseId']}/jobs", "--paginate"]), {}).get("jobs", []):
                if j.get("conclusion") == "cancelled":
                    jobs.append(j["name"])
        return {**out, "klass": "INFRA",
                "detail": (", ".join(sorted(set(jobs))) or "superseded before jobs started"),
                "run_ids": [r["databaseId"] for r in cancelled]}

    revs = jsonl(gh(["api", f"repos/{ORG}/{repo}/pulls/{number}/reviews", "--paginate"]), [])
    real = [f"{r['user']['login']}:{r['state']}" for r in revs
            if (r.get("commit_id") or "").startswith(head[:12])
            and (r.get("body") or "").strip()
            and not any(marker in (r.get("body") or "") for marker in AUTO_REVIEW_MARKERS)]
    if not real:
        return {**out, "klass": "STALE-REVIEW",
                "detail": "no substantive review at this head"}

    q = (f'query={{repository(owner:"{ORG}",name:"{repo}")'
         f'{{pullRequest(number:{number}){{reviewThreads(last:100)'
         f'{{nodes{{isResolved}}}}}}}}}}')
    n_open = gh(["api", "graphql", "-f", q, "-q",
                 "[.data.repository.pullRequest.reviewThreads.nodes[]"
                 "|select(.isResolved==false)]|length"])
    if n_open not in ("0", ""):
        return {**out, "klass": "THREADS", "detail": f"{n_open} unresolved"}
    return {**out, "klass": "READY", "detail": ", ".join(real)}


def self_test() -> int:
    global AUTO_REVIEW_MARKERS
    AUTO_REVIEW_MARKERS = ("synthetic automatic approval",)

    bad = 0
    cases = [
        ("a failed run outranks a cancelled one", [("failure", "completed"),
                                                   ("cancelled", "completed")], "REAL"),
        ("cancelled alone is infrastructure", [("cancelled", "completed")], "INFRA"),
        ("anything still running is early, not red", [("", "in_progress")], "RUNNING"),
    ]
    for label, runs, want in cases:
        rows = [{"conclusion": c, "status": s} for c, s in runs]
        if any(r["status"] != "completed" for r in rows):
            got = "RUNNING"
        elif [r for r in rows if r["conclusion"] == "failure"]:
            got = "REAL"
        elif [r for r in rows if r["conclusion"] == "cancelled"]:
            got = "INFRA"
        else:
            got = "OTHER"
        print(("OK    " if got == want else f"FAIL  got {got}: ") + label)
        bad += got != want
    print("---\n" + ("OK    the cancelled-vs-failed split holds"
                     if not bad else f"FAIL  {bad} wrong"))
    return 1 if bad else 0


def main() -> int:
    global ORG
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prs", nargs="*", type=int, help="PR numbers; default is every open PR")
    ap.add_argument("--repo", help="repository as OWNER/NAME, or NAME with configured github.owner")
    ap.add_argument("--rerun", action="store_true",
                    help="re-run failed jobs of INFRA runs for the PRs named on the "
                         "command line. Refuses to run over the default whole-repo set")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.repo:
        ap.error("--repo OWNER/NAME is required")
    if "/" in args.repo:
        ORG, args.repo = args.repo.split("/", 1)
    if not ORG or not args.repo:
        ap.error("--repo OWNER/NAME or github.owner configuration is required")
    if args.rerun and not args.prs:
        ap.error("--rerun needs an explicit PR list; it will not re-run the whole repo")

    numbers = args.prs or open_prs(args.repo)
    buckets: dict[str, list[dict]] = {}
    for n in numbers:
        r = classify(args.repo, n)
        buckets.setdefault(r["klass"], []).append(r)

    order = ["REAL", "THREADS", "STALE-REVIEW", "INFRA", "RUNNING", "READY", "UNKNOWN"]
    for k in order:
        for r in buckets.get(k, []):
            print(f"{k:<13} #{r['number']:<6} {r['detail'][:46]:<48}{r.get('title','')}")

    counts = {k: len(v) for k, v in buckets.items()}
    total = sum(counts.values())
    print(f"---\n{total} PR(s): " + ", ".join(f"{k}={counts[k]}" for k in order if k in counts))
    infra = buckets.get("INFRA", [])
    if infra and not args.rerun:
        print(f"NOTE  {len(infra)} PR(s) are red only because a run was CANCELLED - "
              f"nothing in those changes failed. Re-run with --rerun and an explicit list.")
    for r in infra if args.rerun else []:
        for rid in r.get("run_ids", []):
            gh(["run", "rerun", str(rid), "-R", f"{ORG}/{args.repo}", "--failed"])
            print(f"OK    re-ran failed jobs of run {rid} for #{r['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
