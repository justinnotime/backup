from __future__ import annotations

import hashlib
import json
import os
import subprocess
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from markdown_issues import tracker
from markdown_issues.config import ConfigurationError, load
from markdown_issues.create import create_issue

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture
def configured(tmp_path):
    cfg = json.loads((PACKAGE / "references/example.json").read_text())
    cfg["repository_root"] = str(tmp_path)
    for directory in (cfg["open_directory"], cfg["closed_directory"]):
        (tmp_path / directory).mkdir(parents=True)
    path = tmp_path / "tracker.json"
    path.write_text(json.dumps(cfg))
    cfg = load(path)
    tracker.configure(cfg)
    return cfg, path


def arguments(**changes):
    value = {
        "title": 'Review "sample" output',
        "actor": None,
        "assignee": None,
        "priority": "P1",
        "kind": "action",
        "project": "",
        "sub_state": None,
        "review_after": None,
        "labels": "example, review",
        "source": [],
        "watch_path": [],
        "dry_run": False,
    }
    value.update(changes)
    return Namespace(**value)


def test_creation_matches_stable_identifier_and_does_not_stage(configured):
    cfg, _ = configured
    root = Path(cfg["repository_root"])
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    relative, text = create_issue(cfg, arguments(assignee="writer"), now=stamp)
    suffix = hashlib.md5(
        b"2026-01-02_review-sample-output_writer_2026-01-02T03:04:05Z", usedforsecurity=False
    ).hexdigest()[:8]
    assert relative == f"records/active/2026-01-02_review-sample-output_{suffix}.md"
    assert (root / relative).read_text() == text
    issue = tracker.parse_issue(root / relative, root)
    assert issue.title == arguments().title
    assert not tracker.audit_issues(root, [issue], stamp.date())
    assert not (root / ".git").exists()
    with pytest.raises(ConfigurationError):
        create_issue(cfg, arguments(assignee="writer"), now=stamp)
    assert (root / relative).read_text() == text


def test_quoted_title_sources_watch_paths_and_labels_round_trip(configured):
    cfg, _ = configured
    root = Path(cfg["repository_root"])
    source = 'documents/a "quoted" file\\name.md'
    (root / source).parent.mkdir(parents=True)
    (root / source).write_text("Synthetic source")
    selected = arguments(
        title='Review "quoted" output \\ input',
        source=[source],
        watch_path=[source],
        labels='quoted"label,back\\slash',
        assignee="writer",
    )
    relative, _ = create_issue(cfg, selected)
    issue = tracker.parse_issue(root / relative, root)
    assert issue.title == selected.title
    assert issue.fields["sources"] == [source]
    assert issue.fields["watch_paths"] == [source]
    assert issue.fields["labels"] == ['quoted"label', "back\\slash"]
    assert not tracker.audit_issues(root, [issue], datetime.now(timezone.utc).date())


def test_json_inline_lists_and_legacy_scalar_quotes():
    assert tracker.parse_inline_list('["a,b", "quote\\"value", "back\\\\slash"]') == [
        "a,b",
        'quote"value',
        "back\\slash",
    ]
    assert tracker.strip_quotes("'legacy text'") == "legacy text"
    assert tracker.strip_quotes('"legacy \\q text"') == "legacy \\q text"


def test_dry_run_creates_no_directory_or_file(configured):
    cfg, _ = configured
    cfg["open_directory"] = "not-created/active"
    root = Path(cfg["repository_root"])
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    _, text = create_issue(cfg, arguments(dry_run=True))
    assert "## Acceptance" in text
    assert before == sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_invalid_body_template_cannot_create_a_broken_record(configured):
    cfg, _ = configured
    cfg["body_template"] = "# {{title}}\nNo required sections.\n"
    with pytest.raises(ConfigurationError):
        create_issue(cfg, arguments())
    assert not list(Path(cfg["repository_root"]).rglob("*.md"))


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "first\nstate: closed"},
        {"actor": "unknown"},
        {"actor": "unassigned"},
        {"assignee": "unknown"},
        {"priority": "P99"},
        {"kind": "other"},
        {"review_after": "2026-02-31"},
        {"review_after": "2026-05-01"},
        {"sub_state": "unknown"},
        {"source": ["../../outside"]},
        {"watch_path": ["../*"]},
        {"project": "x\nlabels: bad"},
    ],
)
def test_invalid_creation_never_writes(configured, changes):
    cfg, _ = configured
    root = Path(cfg["repository_root"])
    with pytest.raises(ConfigurationError):
        create_issue(cfg, arguments(**changes))
    assert not list(root.rglob("*.md"))


def test_custom_directories_and_headings_are_used_everywhere(configured):
    cfg, path = configured
    cfg.update(
        open_directory="tickets/backlog",
        closed_directory="tickets/archive",
        headings={"context": "## Background", "acceptance": "## Checks", "notes": "## History"},
    )
    path.write_text(json.dumps(cfg))
    cfg = load(path)
    tracker.configure(cfg)
    relative, text = create_issue(cfg, arguments(assignee="writer"))
    assert relative.startswith("tickets/backlog/")
    assert "## History" in text and "## Notes" not in text
    root = Path(cfg["repository_root"])
    issues = tracker.load_issues(root)
    assert len(issues) == 1 and issues[0].notes and issues[0].acceptance_total == 1
    assert not tracker.audit_issues(root, issues, datetime.now(timezone.utc).date())


@pytest.mark.parametrize(
    "field,value",
    [
        ("open_directory", "../outside"),
        ("closed_directory", "/tmp/outside"),
        ("open_directory", "records/resolved"),
        ("stale_days", -1),
        ("actors", []),
        ("actors", ["writer", "reviewer", "unassigned", "Uppercase"]),
        ("actors", ["writer", "reviewer", "unassigned", "under_score"]),
        ("default_actor", "absent"),
        ("base_refs", ["--all"]),
    ],
)
def test_bad_configuration_is_rejected(configured, field, value):
    cfg, path = configured
    cfg[field] = value
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigurationError):
        load(path)


def test_source_and_output_symlinks_are_not_followed(configured, tmp_path):
    cfg, path = configured
    (tmp_path / "outside").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ConfigurationError):
        create_issue(cfg, arguments(source=["linked"]))
    cfg["open_directory"] = "linked/items"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigurationError):
        load(path)


def test_missing_configured_directories_cannot_report_empty_success(configured):
    cfg, _ = configured
    cfg["open_directory"], cfg["closed_directory"] = "wrong/active", "wrong/resolved"
    tracker.configure(cfg)
    with pytest.raises(ConfigurationError):
        tracker.load_issues(Path(cfg["repository_root"]))


def test_cli_accepts_legacy_key_equals_options_and_has_no_hidden_home_dependency(
    configured, tmp_path
):
    _cfg, path = configured
    home = tmp_path / "empty-home"
    home.mkdir()
    result = subprocess.run(
        [
            str(PACKAGE / "scripts/issues"),
            "--config",
            str(path),
            "create",
            "Synthetic task",
            "--priority=P2",
            "--actor=writer",
            "--assignee=reviewer",
            "--kind=watch",
            "--review-after=2026-12-31",
        ],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        text=True,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    issue = tracker.parse_issue(tmp_path / result.stdout.strip(), tmp_path)
    assert issue.sub_state == "scheduled" and issue.assignee == "reviewer"
    assert not list(home.iterdir())
