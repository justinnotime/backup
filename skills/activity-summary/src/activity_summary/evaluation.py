import json
import re

from .issue_refs import (
    DEFAULT_REPO,
    canonical,
    facts_touched_refs,
    required_refs,
    split_ref,
    summary_link_refs,
)


def refs_in(text):
    return set(summary_link_refs(text))


def commit_refs(commit, canonical_field="issue_refs", legacy_field="issues"):
    if canonical_field in commit:
        return {canonical(*split_ref(ref)) for ref in commit.get(canonical_field, [])}
    return {canonical(DEFAULT_REPO, number) for number in commit.get(legacy_field, [])}


def issue_reference_sets(facts):
    ground_truth = set(required_refs(facts))
    window_all = set(facts_touched_refs(facts))
    for commit in facts.get("commits", []):
        window_all |= commit_refs(commit)
        window_all |= commit_refs(commit, "issue_refs_in_body", "issues_in_body")
    window_all |= ground_truth
    return (ground_truth, window_all)


def cn_bigrams(s):
    cn = re.findall("[\\u4e00-\\u9fff]+", s)
    out = set()
    for run in cn:
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    return out


def tokens(s):
    t = cn_bigrams(s)
    t |= set((w.lower() for w in re.findall("[A-Za-z][A-Za-z0-9_-]{3,}", s)))
    t |= set(("#" + n for n in re.findall("#([1-9]\\d*)", s)))
    return t


AGENT_WORK_PATTERN = r"^### Agent work[^\n]*\n(.*?)(?=^#{2,3}(?!#)[ \t]+|\Z)"


def agent_work_body(summary):
    match = re.search(AGENT_WORK_PATTERN, summary, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def main(argv=None):
    import argparse

    from .config import DEFAULT_CONFIG, activate, load

    parser = argparse.ArgumentParser()
    parser.add_argument("summary")
    parser.add_argument("facts")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    activate(load(args.config))
    summ = open(args.summary, encoding="utf-8").read()
    facts = json.load(open(args.facts, encoding="utf-8"))
    gt_refs, _context_refs = issue_reference_sets(facts)
    summ_refs = refs_in(summ)
    recall = len(gt_refs & summ_refs) / len(gt_refs) if gt_refs else None
    prec = len(summ_refs & gt_refs) / len(summ_refs) if summ_refs else None
    aw = agent_work_body(summ)
    aw_tok = tokens(aw)
    gt = [c for c in facts["session_clusters"] if c["kind"] == "human" and c["messages"] >= 4]
    rep = 0
    detail = []
    for cl in gt:
        ptok = set()
        for p in cl.get("user_prompts", []):
            ptok |= tokens(p)
        overlap = len(ptok & aw_tok)
        hit = overlap >= 2 or cl["time"][:5] in aw
        rep += 1 if hit else 0
        detail.append({"time": cl["time"], "msgs": cl["messages"], "overlap": overlap, "hit": hit})
    sess_recall = rep / len(gt) if gt else None
    print(
        json.dumps(
            {
                "summary": args.summary.split("/")[-1],
                "issue_gt": len(gt_refs),
                "issue_summary": len(summ_refs),
                "issue_recall": round(recall, 3) if recall is not None else None,
                "issue_precision": round(prec, 3) if prec is not None else None,
                "sess_gt": len(gt),
                "sess_represented": rep,
                "sess_recall": round(sess_recall, 3) if sess_recall is not None else None,
                "sess_detail": detail,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
