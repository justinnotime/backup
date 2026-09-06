"""Synthetic local archives exercise graph topology, dates and executable isolation."""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import issue_graph as graph


def issue(number, **changes):
    return {
        "number": number,
        "title": f"Synthetic issue {number}",
        "state": "open",
        "type": "gh-issue",
        "labels": ["example"],
        "related": [],
        "created": "2025-01-06T12:00:00Z",
        "closed": "",
        "author": "example-user",
        **changes,
    }


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / "archive"
    source.mkdir()
    records = [
        issue(101, title="[tracker] Example: Foundation", related=[102, 201, 202, 203]),
        issue(102, title="[master] Example: Delivery", related=[202, 204]),
        issue(201, title="Ready for review"),
        issue(
            202,
            title="Shared dependency",
            state="closed",
            type="gh-pull-request",
            closed="2025-01-07T09:00:00Z",
        ),
        issue(203, title="Completed issue", state="closed", closed="2025-01-08"),
        issue(204, title="Unfinished dependency", created="2025-01-07"),
    ]
    for record in records:
        (source / f"{record['number']}.md").write_text(
            "---\n"
            + yaml.safe_dump(record, sort_keys=False)
            + "---\n\nOriginal issue body.\n",
            encoding="utf-8",
        )
    (source / "README.md").write_text("# Archive index\n")
    return source


def test_filter_union_and_exclusion_precedence():
    issues = {
        1: issue(1, title="Selected by label", labels=["selected"]),
        2: issue(2, title="MATCH by title", labels=[]),
        3: issue(3, title="MATCH excluded", labels=["selected"]),
        4: issue(4, title="Unrelated", labels=[]),
    }
    assert list(
        graph.filter_relevant(
            issues, ["selected"], re.compile("match", re.I), re.compile("excluded")
        )
    ) == [1, 2]
    assert graph.filter_relevant(issues, [], None, None) == issues


def test_tracker_detection_uses_title_label_or_selected_outdegree():
    issues = {
        1: issue(1, title="[tracker] Example"),
        2: issue(2, labels=["master"]),
        3: issue(3, related=[1, 2, 999]),
        4: issue(4, related=[998, 999]),
    }
    assert graph.auto_detect_trackers(issues, min_outdeg=2) == [1, 2, 3]


def test_full_graph_preserves_tracker_edges_open_children_and_shared_dependencies(
    archive,
):
    issues = graph.load_issues(archive)
    result = graph.build_full_dot(issues, [101, 102], closed_per_tracker=0, top_cross=1)
    assert "subgraph cluster_101" in result and "subgraph cluster_102" in result
    assert '"101" -> "102" [penwidth=2' in result
    assert '"101" -> "201" [color=' in result
    assert '"102" -> "204" [color=' in result
    assert "subgraph cluster_cross" in result
    assert '"101" -> "202" [style=dashed' in result
    assert '"102" -> "202" [style=dashed' in result
    assert '"203"' not in result
    assert '"202"' not in graph.build_full_dot(issues, [101, 102], top_cross=0)


def test_spine_contains_only_selected_trackers_and_directed_relationships(archive):
    text = graph.build_spine_dot(graph.load_issues(archive), [101, 102, 999])
    assert '"101" -> "102";' in text
    assert '"102" -> "101";' not in text
    assert '"201"' not in text and '"999"' not in text


@pytest.mark.parametrize("builder", [graph.build_full_dot, graph.build_spine_dot])
def test_titles_cannot_escape_quoted_graph_labels(builder):
    issues = {1: issue(1, title='[tracker] A "quote" and \\slash\nnext')}
    text = builder(issues, [1])
    assert "A 'quote' and \\\\slash next" in text
    dot = shutil.which("dot")
    if dot:
        result = subprocess.run(
            [dot, "-Tdot"], input=text, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr


def test_timeline_counts_shared_issue_for_both_trackers_and_closure_day(archive):
    text = graph.build_timeline(
        graph.load_issues(archive),
        [101, 102],
        start=datetime.date(2025, 1, 6),
        end=datetime.date(2025, 1, 8),
    )
    rows = [line for line in text.splitlines() if "│" in line]
    assert len(rows) == 6
    assert rows[0].endswith("+│▴  │")
    assert rows[1].endswith("−│ ··│")
    assert rows[2].endswith("+│▪· │")
    assert rows[3].endswith("−│ · │")
    assert rows[4].endswith("+│▴· │")
    assert rows[5].endswith("−│ ▪·│")


def test_timeline_infers_monday_start_and_bounds_large_ranges(archive):
    issues = graph.load_issues(archive)
    issues[101]["created"] = "2025-01-08"
    assert "2025-01-06 → 2025-01-08" in graph.build_timeline(issues, [101])
    assert "too large" in graph.build_timeline(
        issues, [101], datetime.date(2024, 1, 1), datetime.date(2025, 1, 1)
    )
    assert "no issues" in graph.build_timeline({}, [])


def test_tiers_classify_archived_type_and_state_without_assuming_merge():
    assert graph.assign_tier(issue(1, type="gh-pull-request", state="closed")) == "A"
    assert graph.assign_tier(issue(2, state="closed")) == "B"
    assert graph.assign_tier(issue(3)) == "C"
    assert graph.assign_tier(issue(4, state="unknown")) == "D"


@pytest.mark.parametrize(
    "mode,suffixes",
    [
        (
            "all",
            {"issue-graph.dot", "tracker-spine.dot", "timeline.txt", "issues.json"},
        ),
        ("full-dot", {"issue-graph.dot"}),
        ("spine", {"tracker-spine.dot"}),
        ("timeline", {"timeline.txt"}),
        ("inventory", {"issues.json"}),
    ],
)
def test_cli_writes_only_selected_artifacts_and_preserves_inputs(
    archive, tmp_path, mode, suffixes
):
    before = {p.name: p.read_bytes() for p in archive.iterdir()}
    output = tmp_path / "generated"
    assert (
        graph.main(
            [
                "--input-dir",
                str(archive),
                "--out-dir",
                str(output),
                "--prefix",
                "example",
                "--mode",
                mode,
                "--quiet",
            ]
        )
        == 0
    )
    assert {p.name for p in output.iterdir()} == {
        "example-" + suffix for suffix in suffixes
    }
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == before
    if "issues.json" in suffixes:
        data = json.loads((output / "example-issues.json").read_text())
        assert (
            len(data) == 6 and data["202"]["tier"] == "A" and data["203"]["tier"] == "B"
        )


def test_stdout_and_stats_modes_create_no_files(archive, tmp_path, capsys):
    assert graph.main(["--input-dir", str(archive), "--mode", "timeline", "-q"]) == 0
    assert "Issue cadence" in capsys.readouterr().out
    output = tmp_path / "unused"
    assert (
        graph.main(
            [
                "--input-dir",
                str(archive),
                "--mode",
                "stats",
                "--out-dir",
                str(output),
                "-q",
            ]
        )
        == 0
    )
    report = capsys.readouterr().out
    assert "Issues: 6 total" in report and "Cross-tracker bridges" in report
    assert not output.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--prefix", "../outside"],
        ["--start", "invalid"],
        ["--title-include", "["],
        ["--trackers", "one"],
        ["--top-cross", "-1"],
    ],
)
def test_bad_options_fail_before_any_output(archive, tmp_path, args):
    output = tmp_path / "generated"
    assert (
        graph.main(["--input-dir", str(archive), "--out-dir", str(output), *args]) == 2
    )
    assert not output.exists()


def test_output_symlink_cannot_overwrite_source(archive, tmp_path):
    output = tmp_path / "generated"
    output.mkdir()
    protected = archive / "101.md"
    before = protected.read_bytes()
    (output / "example-issues.json").symlink_to(protected)
    assert (
        graph.main(
            [
                "--input-dir",
                str(archive),
                "--out-dir",
                str(output),
                "--prefix",
                "example",
            ]
        )
        == 2
    )
    assert protected.read_bytes() == before
    assert len(list(output.iterdir())) == 1


def test_missing_archive_does_not_report_empty_success(tmp_path):
    assert (
        graph.main(["--input-dir", str(tmp_path / "missing"), "--mode", "stats"]) == 2
    )


def test_copied_package_runs_without_siblings_or_original_home(archive, tmp_path):
    package = tmp_path / "standalone"
    original = Path(__file__).resolve().parents[1]
    shutil.copytree(original / "scripts", package / "scripts")
    shutil.copytree(
        original / "src",
        package / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    home = tmp_path / "empty-home"
    home.mkdir()
    shutil.copytree(archive, home / "selected")
    command = package / "scripts/graph"
    result = subprocess.run(
        [str(command), "--input-dir", "$HOME/selected", "--mode", "inventory", "-q"],
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "GITHUB_ARCHIVE_PYTHON": sys.executable,
        },
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"202"' in result.stdout
    assert sorted(path.name for path in home.iterdir()) == ["selected"]


def test_cli_has_no_implicit_repository_selection():
    with pytest.raises(SystemExit) as error:
        graph.parse_args(["--mode", "stats"])
    assert error.value.code == 2
