"""Descriptive aggregates from enriched usage observations. LLM calls = 0."""

import gzip
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

from .usage_analysis import validate_local_output


def footprint(rows):
    priced = [r for r in rows if r["reference_usd"] is not None]
    return {
        "observations": len(rows),
        "sessions": len({(r["harness"], r["session"]) for r in rows}),
        "priced_observations": len(priced),
        "reference_usd": sum(r["reference_usd"] for r in priced) if priced else None,
        "write_usd": sum(
            sum(r["cost_by_category"][k] for k in ("write5", "write1", "write_unknown"))
            for r in priced
        )
        if priced
        else None,
        "resets": sum(r["cache_reset"] for r in rows),
        "reset_rate": sum(r["cache_reset"] for r in rows) / len(rows) if rows else None,
        "median_context": statistics.median(r["context"] for r in rows)
        if rows
        else None,
    }


def cluster_interval(rows, *, seed, resamples):
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        unit = groups[row["harness"], row["session"]]
        unit[0] += row["cache_reset"]
        unit[1] += 1
    units = [groups[key] for key in sorted(groups)]
    if len(units) < 5:
        return None
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        sample = rng.choices(units, k=len(units))
        draws.append(sum(u[0] for u in sample) / sum(u[1] for u in sample))
    draws.sort()
    # Empirical nearest-rank percentile interval, resampling whole sessions.
    return [draws[math.ceil(p * resamples) - 1] for p in (0.025, 0.975)]


def behavior_statistics(rows, *, seed=0, resamples=1000):
    if not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 100:
        raise ValueError("at least 100 bootstrap resamples required")
    conditions = {
        "observed_gap_gt_300": ("observed_gap_seconds", lambda x: x > 300),
        "observed_gap_le_240": ("observed_gap_seconds", lambda x: 0 <= x <= 240),
        "start_gap_lower_gt_300": ("start_gap_lower_seconds", lambda x: x > 300),
        "start_gap_upper_le_240": ("start_gap_upper_seconds", lambda x: 0 <= x <= 240),
    }

    def selected(values, condition):
        field, predicate = conditions[condition]
        return [r for r in values if r[field] is not None and predicate(r[field])]

    patterns = {
        "all": rows,
        "notification-immediate": [
            r for r in rows if r["input_kind"] == "notification"
        ],
        "notification-fetch-only": [
            r
            for r in rows
            if r["input_kind"] == "notification"
            and r["actions"] == ["coordinate-receive"]
        ],
        "all-fetch-only": [r for r in rows if r["actions"] == ["coordinate-receive"]],
        "no-visible-task": [r for r in rows if r["task_id"] is None],
        "unknown-function": [r for r in rows if not r["function_candidates"]],
    }
    pools = defaultdict(list)
    for row in rows:
        if row["cache_comparable"]:
            pools[row["harness"], row["model"]].append(row)
    groups = []
    for (harness, model), pool in sorted(pools.items()):
        rules = []
        for condition in conditions:
            values = selected(pool, condition)
            rules.append(
                dict(
                    condition=condition,
                    **footprint(values),
                    session_bootstrap_95=cluster_interval(
                        values, seed=seed, resamples=resamples
                    ),
                )
            )
        by_session = defaultdict(list)
        cross = defaultdict(list)
        for row in pool:
            by_session[row["session"]].append(row)
            wake, observed = row.get("wake_gap_seconds"), row["observed_gap_seconds"]
            if (
                row["first_after_wake"]
                and wake is not None
                and observed is not None
                and wake >= 0
                and observed >= 0
            ):
                cross[wake > 300, observed > 300].append(row)
        differences = []
        for values in by_session.values():
            short = selected(values, "observed_gap_le_240")
            long = selected(values, "observed_gap_gt_300")
            if len(short) >= 20 and len(long) >= 3:
                differences.append(
                    sum(r["cache_reset"] for r in long) / len(long)
                    - sum(r["cache_reset"] for r in short) / len(short)
                )
        groups.append(
            {
                "harness": harness,
                "model": model,
                "rules": rules,
                "within_session_comparison": {
                    "sessions": len(differences),
                    "positive": sum(d > 0 for d in differences),
                    "median_difference": statistics.median(differences)
                    if differences
                    else None,
                    "minimum_short_observations": 20,
                    "minimum_long_observations": 3,
                },
                "message_versus_model_gap": [
                    dict(
                        wake_gap_gt_300=w,
                        model_observation_gap_gt_300=m,
                        **footprint(values),
                    )
                    for (w, m), values in sorted(cross.items())
                ],
            }
        )
    return {
        "schema_version": "agent-usage-statistics/v1",
        "footprints": {name: footprint(values) for name, values in patterns.items()},
        "groups": groups,
        "bootstrap_seed": seed,
        "bootstrap_resamples": resamples,
        "independent_prospective_validation": False,
        "causal_savings_estimate": None,
        "complete_billing_coverage": False,
    }


def summarize_file(source, output, *, seed=0, resamples=1000):
    source, output = Path(source), Path(output)
    validate_local_output(output)
    if output.exists():
        raise FileExistsError("output already exists")
    # Hash and parse the same bytes, including when the caller's file is replaced.
    payload = source.read_bytes()
    rows = [json.loads(line) for line in gzip.decompress(payload).splitlines()]
    if any(r.get("schema_version") != "agent-usage/v1" for r in rows):
        raise ValueError("expected enriched agent-usage/v1 observations")
    report = behavior_statistics(rows, seed=seed, resamples=resamples)
    report["source_usage_sha256"] = hashlib.sha256(payload).hexdigest()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    target = output / "statistics.json"
    with target.open("x") as stream:
        target.chmod(0o600)
        json.dump(report, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return {"status": "statistics-written", "observations": len(rows)}
