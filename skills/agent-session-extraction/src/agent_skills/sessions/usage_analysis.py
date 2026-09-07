"""Configured tariff scenarios and descriptive comparisons over usage records."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .manifest import load_manifest
from .sources import (
    discover_candidates,
    revalidate_snapshot,
    snapshot_candidate,
    validate_configured_path,
)
from .telemetry import TOKENS, decode_telemetry
from .telemetry_features import digest, seconds


def price(row, rates):
    matches = [
        rate
        for rate in rates
        if rate["model"] == row["model"]
        and rate.get("harness", row["harness"]) == row["harness"]
        and (
            not rate.get("start")
            or (row["time"] and seconds(row["time"]) >= seconds(rate["start"]))
        )
        and (
            not rate.get("end")
            or (row["time"] and seconds(row["time"]) < seconds(rate["end"]))
        )
    ]
    if len(matches) != 1:
        return None
    rate = matches[0]
    prices = rate["per_million"]
    if any(
        k not in prices
        or isinstance(prices[k], bool)
        or not isinstance(prices[k], (int, float))
        or not math.isfinite(prices[k])
        or prices[k] < 0
        for k in TOKENS
    ):
        raise ValueError("invalid rate categories")
    long = rate.get("long_context")
    context = sum(row[k] for k in TOKENS if k != "output")
    result = {k: row[k] * prices[k] / 1_000_000 for k in TOKENS}
    if long and context > long["threshold"]:
        for k in TOKENS:
            result[k] *= long[
                "output_multiplier" if k == "output" else "input_multiplier"
            ]
    return result


def enrich(rows, rates=()):
    groups = defaultdict(list)
    for row in rows:
        groups[row["harness"], row["session"]].append(row)
    for values in groups.values():
        values.sort(key=lambda r: (r["time"] is None, r["time"] or "", str(r["line"])))
        previous = None
        seen_wakes = set()
        for index, row in enumerate(values):
            context = sum(row[k] for k in TOKENS if k != "output")
            row.update(
                within_window_operation_index=index,
                context=context,
                read_fraction=row["read"] / context if context else None,
                first_after_wake=bool(
                    row["wake_id"]
                    and row["wake_id"] not in seen_wakes
                    and not row.get("wake_before_window", False)
                ),
            )
            seen_wakes.add(row["wake_id"])
            cost = price(row, rates)
            row["cost_by_category"] = cost
            row["reference_usd"] = sum(cost.values()) if cost is not None else None
            row["observed_gap_seconds"] = None
            row["start_gap_lower_seconds"] = None
            row["start_gap_upper_seconds"] = None
            row["cache_comparable"] = False
            row["cache_reset"] = bool(
                context
                and row["read"] / context < 0.2
                and sum(row[k] for k in ("write5", "write1", "write_unknown")) / context
                > 0.5
            )
            if previous:
                now, before = seconds(row["time"]), seconds(previous["time"])
                if now is not None and before is not None:
                    row["observed_gap_seconds"] = now - before
                lo, hi = (
                    row.get("request_start_at") or row.get("ready_at"),
                    row.get("request_start_at") or row.get("first_response_at"),
                )
                plo, phi = (
                    previous.get("request_start_at") or previous.get("ready_at"),
                    previous.get("request_start_at")
                    or previous.get("first_response_at"),
                )
                if (
                    all(v is not None for v in (lo, hi, plo, phi))
                    and lo <= hi
                    and plo <= phi
                ):
                    row["start_gap_lower_seconds"] = max(0, lo - phi)
                    row["start_gap_upper_seconds"] = hi - plo
                prevcontext = previous["context"]
                row["cache_comparable"] = bool(
                    prevcontext >= 100_000
                    and 0.8 <= context / prevcontext <= 1.2
                    and previous["model"] == row["model"]
                    and (previous["read_fraction"] or 0) >= 0.8
                    and row["compactions"] == previous["compactions"]
                )
            previous = row
    return sorted(
        rows,
        key=lambda r: (r["time"] or "", r["harness"], r["session"], str(r["line"])),
    )


def summarize(rows):
    dimensions = defaultdict(
        lambda: {
            "observations": 0,
            "priced_observations": 0,
            "reference_usd": 0.0,
            "tokens": Counter(),
        }
    )
    rules = defaultdict(
        lambda: {
            "observations": 0,
            "resets": 0,
            "sessions": set(),
            "examples": [],
            "counterexamples": [],
        }
    )
    for row in rows:
        # Each dimension independently conserves the total; multiple action
        # candidates form one combination instead of duplicating dollar values.
        labels = {
            "harness": row["harness"],
            "model": row["model"],
            "task_origin": row["task_origin"],
            "wake_origin": row["wake_origin"],
            "input_kind": row["input_kind"],
            "function_candidates": "+".join(row["function_candidates"]) or "unknown",
            "actions": "+".join(row["actions"]) or "no-visible-tool",
            "project_candidates": "+".join(row["project_candidates"]) or "unknown",
        }
        for dimension, label in labels.items():
            value = dimensions[dimension, label]
            value["observations"] += 1
            value["tokens"].update({k: row[k] for k in TOKENS})
            if row["reference_usd"] is not None:
                value["priced_observations"] += 1
                value["reference_usd"] += row["reference_usd"]
        if not row["cache_comparable"]:
            continue
        gap = row["observed_gap_seconds"]
        bound = row["start_gap_lower_seconds"]
        upper = row["start_gap_upper_seconds"]
        tests = {
            "observed_gap_gt_300": gap is not None and gap > 300,
            "observed_gap_le_240": gap is not None and gap <= 240,
            "start_gap_lower_gt_300": bound is not None and bound > 300,
            "start_gap_upper_le_240": upper is not None and upper <= 240,
        }
        group = (
            "session-group-b"
            if int(digest((row["harness"], row["session"])), 16) % 5 == 0
            else "session-group-a"
        )
        for name, included in tests.items():
            if not included:
                continue
            value = rules[row["harness"], row["model"], group, name]
            value["observations"] += 1
            value["resets"] += int(row["cache_reset"])
            value["sessions"].add(row["session"])
            field = "examples" if row["cache_reset"] else "counterexamples"
            if len(value[field]) < 5:
                value[field].append(row["usage_key"])
    return {
        "schema_version": "agent-usage-analysis/v1",
        "observations": len(rows),
        "unpriced_observations": sum(r["reference_usd"] is None for r in rows),
        "tokens": {k: sum(r[k] for r in rows) for k in TOKENS},
        "reference_usd": sum(r["reference_usd"] or 0 for r in rows),
        "dimensions": [
            dict(dimension=d, label=label, **value)
            for (d, label), value in sorted(dimensions.items())
        ],
        "cache_rules": [
            dict(
                harness=h,
                model=m,
                group=g,
                condition=c,
                **{k: v for k, v in value.items() if k != "sessions"},
                sessions=len(value["sessions"]),
            )
            for (h, m, g, c), value in sorted(rules.items())
        ],
    }


def write_jsonl(path, rows):
    with path.open("xb") as raw:
        path.chmod(0o600)
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as target:
            for row in rows:
                target.write((json.dumps(row, sort_keys=True) + "\n").encode())


def validate_local_output(path):
    """Keep private usage artifacts outside worktrees and Git object stores."""
    for destination in {Path(path).absolute(), Path(path).resolve()}:
        for parent in (destination, *destination.parents):
            if (parent / ".git").exists() or (
                (parent / "HEAD").is_file() and (parent / "objects").is_dir()
            ):
                raise ValueError("analysis output must be outside Git repositories")


def run_analysis(manifest_path, output, *, start, end, config=None):
    """Read only configured sources and write a new private result directory."""
    config = config or {}
    start_at, end_at = seconds(start), seconds(end)
    if start_at is None or end_at is None or start_at >= end_at:
        raise ValueError("window needs ordered timestamps with timezone")
    manifest = load_manifest(Path(manifest_path), environ=os.environ)
    output = Path(output)
    destination = output.resolve()
    roots = [
        validate_configured_path(s).resolved for s in manifest.sources if s.enabled
    ]
    owned = [
        manifest.output.repository_root / p for p in manifest.publisher.owned_subtrees
    ]
    if any(
        destination == root or destination.is_relative_to(root)
        for root in [*roots, *(p.resolve() for p in owned)]
    ):
        raise ValueError("analysis output overlaps a source or archive subtree")
    validate_local_output(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    rows, events, inventory, counts = {}, {}, [], Counter()
    seen_payloads = set()
    for source in manifest.sources:
        if not source.enabled:
            continue
        root = validate_configured_path(source)
        for candidate in discover_candidates(source, root):
            snapshot = snapshot_candidate(source, root, candidate)
            sha = (
                hashlib.sha256(snapshot.payload).hexdigest()
                if snapshot.payload is not None
                else None
            )
            inventory.append(
                {
                    "source_id": source.source_id,
                    "source_ref": snapshot.source_ref,
                    "sha256": sha,
                    "bytes": len(snapshot.payload)
                    if snapshot.payload is not None
                    else None,
                    "access_mode": snapshot.access_mode,
                }
            )
            if sha and (source.harness, sha) in seen_payloads:
                counts["duplicate-source-bytes"] += 1
                continue
            seen_payloads.add((source.harness, sha))
            batch = decode_telemetry(snapshot, config=config)
            revalidate_snapshot(snapshot, source, root)
            inventory[-1].update(
                harness=source.harness,
                decode_status=batch.decode_status,
                observations=len(batch.usage),
                diagnostics=dict(batch.counts),
            )
            counts.update(
                {source.harness + ":" + k: v for k, v in batch.counts.items()}
            )
            for row in batch.usage:
                at = seconds(row["time"])
                if at is None:
                    counts["usage-without-timestamp"] += 1
                    continue
                if not start_at <= at < end_at:
                    continue
                row["wake_before_window"] = (
                    row["wake_at"] is not None and row["wake_at"] < start_at
                )
                key = row["usage_key"]
                if key in rows:
                    counts["duplicate-usage-across-sources"] += 1
                    old = rows[key]
                    if any(row[k] != old[k] for k in TOKENS):
                        counts["usage-variants-across-sources"] += 1
                    if (
                        row["harness"] != "claude-code"
                        or row["output"] <= old["output"]
                    ):
                        continue
                rows[key] = row
            for event in batch.events:
                if event["at"] is not None and start_at <= event["at"] < end_at:
                    events.setdefault(event["id"], event)
    enriched = enrich(list(rows.values()), config.get("rates", ()))
    report = summarize(enriched)
    report.update(
        window=[start, end],
        collected_at=datetime.now(UTC).isoformat(),
        diagnostics=dict(counts),
        source_count=len(inventory),
        manifest_sha256=digest(Path(manifest_path).read_text()),
        configuration_sha256=digest(config),
        reference_tariff_label=config.get("tariff_label", "unspecified"),
        complete_billing_coverage=False,
    )
    for name, values in (
        ("usage", enriched),
        ("activity", events.values()),
        ("sources", inventory),
    ):
        write_jsonl(output / (name + ".jsonl.gz"), values)
    with (output / "summary.json").open("x") as target:
        (output / "summary.json").chmod(0o600)
        json.dump(report, target, indent=2, sort_keys=True)
        target.write("\n")
    return {
        "status": "observations-written" if enriched else "no-observations",
        "observations": len(enriched),
        "unpriced_observations": report["unpriced_observations"],
        "complete_billing_coverage": False,
    }
