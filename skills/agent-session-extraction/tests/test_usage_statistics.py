"""Synthetic checks for local storage and descriptive statistical claims."""

import gzip
import json
import subprocess

import pytest
from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.cli import main
from agent_skills.sessions.usage_analysis import run_analysis, validate_local_output
from agent_skills.sessions.usage_statistics import behavior_statistics, summarize_file


def observation(session="private-session-sentinel", **changes):
    return dict(
        {
            "schema_version": "agent-usage/v1",
            "harness": "example-harness",
            "model": "example-model",
            "session": session,
            "reference_usd": 2,
            "cost_by_category": {"write5": 1, "write1": 0, "write_unknown": 0},
            "cache_reset": True,
            "context": 200_000,
            "cache_comparable": True,
            "input_kind": "notification",
            "actions": ["coordinate-receive"],
            "task_id": None,
            "function_candidates": [],
            "first_after_wake": True,
            "wake_gap_seconds": 30,
            "observed_gap_seconds": 360,
            "start_gap_lower_seconds": 310,
            "start_gap_upper_seconds": 400,
        },
        **changes,
    )


def test_overlapping_patterns_are_not_added_and_unknown_cost_is_preserved():
    rows = [observation(), observation(reference_usd=None, cost_by_category=None)]
    report = behavior_statistics(rows)
    total = report["footprints"]["all"]
    assert total["observations"] == 2 and total["priced_observations"] == 1
    assert total["reference_usd"] == 2 and total["write_usd"] == 1
    assert report["footprints"]["notification-fetch-only"] == total
    assert "private-session-sentinel" not in json.dumps(report)
    unknown = behavior_statistics(rows[1:])["footprints"]["all"]
    assert unknown["reference_usd"] is None
    assert behavior_statistics([])["footprints"]["all"]["reset_rate"] is None
    assert report["causal_savings_estimate"] is None


def test_bootstrap_resamples_sessions_not_individual_observations():
    rows = [observation(str(i), cache_reset=bool(i % 2)) for i in range(6)]

    def interval(values):
        report = behavior_statistics(values, seed=7, resamples=1000)
        return report["groups"][0]["rules"][0]["session_bootstrap_95"]

    expected = interval(rows)
    assert expected[0] < 0.5 < expected[1]
    assert interval(rows * 50) == expected
    assert interval(list(reversed(rows))) == expected
    assert interval(rows[:4]) is None


def test_within_session_support_and_wake_gap_are_distinct():
    rows = [observation(cache_reset=False, observed_gap_seconds=30)] * 20
    rows += [observation()] * 3
    rows += [observation("unsupported", cache_reset=False, observed_gap_seconds=30)]
    rows += [observation(model="another-model")] * 3
    result = behavior_statistics(rows)
    group = next(g for g in result["groups"] if g["model"] == "example-model")
    assert group["within_session_comparison"]["sessions"] == 1
    assert group["within_session_comparison"]["median_difference"] == 1
    cross = group["message_versus_model_gap"]
    long = next(r for r in cross if r["model_observation_gap_gt_300"])
    assert not long["wake_gap_gt_300"] and long["resets"] == 3
    assert len(result["groups"]) == 2


def test_missing_and_negative_gaps_are_not_short_gap_evidence():
    rows = [
        observation(
            observed_gap_seconds=None,
            start_gap_lower_seconds=None,
            start_gap_upper_seconds=None,
        )
    ]
    rows += [
        observation(
            observed_gap_seconds=-1,
            start_gap_lower_seconds=-1,
            start_gap_upper_seconds=-1,
        )
    ]
    group = behavior_statistics(rows)["groups"][0]
    assert all(r["observations"] == 0 for r in group["rules"])
    assert not group["message_versus_model_gap"]


def test_local_cli_no_overwrite_and_no_record_identifiers(tmp_path, capsys):
    source = tmp_path / "usage.jsonl.gz"
    source.write_bytes(gzip.compress((json.dumps(observation()) + "\n").encode()))
    before = source.read_bytes()
    output = tmp_path / "results"
    args = ["summarize-usage", "--input", str(source), "--output", str(output)]
    assert main(args) == 0
    report = json.loads((output / "statistics.json").read_text())
    assert report["footprints"]["all"]["reference_usd"] == 2
    assert report["bootstrap_resamples"] == 1000
    assert source.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "statistics.json").stat().st_mode & 0o777 == 0o600
    assert main(args) == 2
    assert "private-session-sentinel" not in capsys.readouterr().out
    with pytest.raises(ValueError):
        behavior_statistics([], resamples=0)


def test_both_writers_refuse_repository_outputs_before_reading_sources(tmp_path):
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    source = tmp_path / "source"
    source.mkdir()
    manifest = write_manifest(
        tmp_path / "manifest.json", manifest_data(source, tmp_path / "archive")
    )
    output = repository / "private-results"
    with pytest.raises(ValueError, match="outside Git"):
        run_analysis(
            manifest, output, start="2026-02-01T00:00:00Z", end="2026-03-01T00:00:00Z"
        )
    with pytest.raises(ValueError, match="outside Git"):
        summarize_file(tmp_path / "missing-input", output)
    assert not output.exists()


def test_git_worktree_bare_metadata_and_symlinks_are_local_output_boundaries(tmp_path):
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "synthetic",
        ],
        check=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-q",
            "--detach",
            str(worktree),
        ],
        check=True,
    )
    bare = tmp_path / "store.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    (tmp_path / "linked").symlink_to(worktree, target_is_directory=True)
    (repository / "outside-link").symlink_to(
        tmp_path / "local", target_is_directory=True
    )
    for parent in (
        repository,
        worktree,
        bare,
        repository / ".git/objects",
        tmp_path / "linked",
        repository / "outside-link",
    ):
        with pytest.raises(ValueError, match="outside Git"):
            validate_local_output(parent / "new" / "analysis")
    validate_local_output(tmp_path / "local/new/analysis")
