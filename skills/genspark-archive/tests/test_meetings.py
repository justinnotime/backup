import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from genspark_archive import meetings
from genspark_archive.common import ArchiveError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    state = tmp_path / "progress/meetings.state.json"
    config = tmp_path / "config.json"
    value = {
        "schema": "genspark-archive/v1",
        "repository_root": str(root),
        "rate_delay": 0,
        "meetings": {"output_directory": "meetings", "state_file": str(state)},
    }
    config.write_text(json.dumps(value))
    client = Mock()
    client.call.side_effect = ArchiveError("unexpected service call")
    monkeypatch.setattr(meetings, "Client", Mock(return_value=client))
    monkeypatch.setattr(meetings, "utc_now", lambda: datetime(2030, 5, 17, 12, tzinfo=timezone.utc))
    return config, value, root, state, client


def meeting(ident="example-id", *, status="INIT", date="2030-05-17T09:00:00Z"):
    return {"id": ident, "title": "Example meeting", "status": status, "created_at": date}


def page(records, *, more=False, token=None):
    return {"data": {"notes": records, "has_more": more, "continuation_token": token}}


def detail(ident="example-id", *, status="COMPLETED", text="Verbatim transcript."):
    return {
        "data": {
            "meeting": {
                "id": ident,
                "status": status,
                "user_notes": "Original note.",
                "transcription_text": text,
            }
        }
    }


def test_renderer_ignores_generated_summary_but_keeps_raw_fields():
    record = meeting(status="COMPLETED")
    original = {
        "summary": "Generated interpretation must not enter the archive.",
        "user_notes": "Human-authored note.",
        "transcription_text": "Verbatim transcript.",
    }
    rendered = meetings.meeting_to_markdown(record, original)
    assert "## Summary" not in rendered
    assert original["summary"] not in rendered
    assert "## User Notes\n\nHuman-authored note." in rendered
    assert "## Transcript\n\nVerbatim transcript." in rendered


def test_complete_pagination_then_details_preserves_filename_and_old_state(setup):
    config, _, root, state, client = setup
    state.parent.mkdir()
    state.write_text(json.dumps({"synced_ids": ["existing-id"], "custom": "preserved"}))
    client.call.side_effect = [
        page([meeting("first-id")], more=True, token="next-page"),
        page([meeting("second-id")]),
        detail("first-id"),
        detail("second-id"),
    ]
    assert meetings.main(["--config", str(config)]) == 0
    assert client.call.call_args_list[1].args[0] == [
        "meeting",
        "list",
        "--page_size",
        "50",
        "--continuation_token",
        "next-page",
    ]
    assert [call.args[0][1] for call in client.call.call_args_list] == [
        "list",
        "list",
        "get",
        "get",
    ]
    expected = (
        root
        / "meetings"
        / ("2030-05-17_Example-meeting_" + hashlib.md5(b"first-id").hexdigest()[:8] + ".md")
    )
    assert expected.exists()
    assert "**Status:** COMPLETED" in expected.read_text()
    updated = json.loads(state.read_text())
    assert updated["synced_ids"] == ["existing-id", "first-id", "second-id"]
    assert updated["custom"] == "preserved"


def test_detail_failure_cannot_freeze_list_completed_record_or_advance_any_progress(setup):
    config, _, root, state, client = setup
    state.parent.mkdir()
    original_state = '{"synced_ids": ["existing-id"]}'
    state.write_text(original_state)
    client.call.side_effect = [
        page([meeting("first-id", status="COMPLETED"), meeting("bad-id", status="COMPLETED")]),
        detail("first-id"),
        ArchiveError("synthetic get failure"),
    ]
    assert meetings.main(["--config", str(config)]) == 1
    assert state.read_text() == original_state
    assert not (root / "meetings").exists()


def test_later_page_failure_never_fetches_details_or_changes_state(setup):
    config, _, root, state, client = setup
    client.call.side_effect = [
        page([meeting()], more=True, token="next"),
        ArchiveError("synthetic list failure"),
    ]
    assert meetings.main(["--config", str(config)]) == 1
    assert not state.exists()
    assert not (root / "meetings").exists()
    assert all(call.args[0][1] == "list" for call in client.call.call_args_list)


def test_init_is_refetched_until_detail_completes(setup):
    config, _, root, state, client = setup
    client.call.side_effect = [
        page([meeting()]),
        detail(status="INIT", text=""),
        page([meeting()]),
        detail(),
    ]
    assert meetings.main(["--config", str(config)]) == 0
    assert json.loads(state.read_text())["synced_ids"] == []
    path = next((root / "meetings").glob("*.md"))
    assert "**Status:** INIT" in path.read_text()
    assert meetings.main(["--config", str(config)]) == 0
    assert json.loads(state.read_text())["synced_ids"] == ["example-id"]
    assert "Verbatim transcript." in path.read_text()


@pytest.mark.parametrize(
    "status,days,synced",
    [
        ("INIT", 3, True),
        ("INIT", 30, False),
        ("FAILED", 3, False),
        ("ERROR", 3, False),
        ("PROCESSING", 3, False),
    ],
)
def test_retry_period_applies_only_to_successfully_read_init_records(setup, status, days, synced):
    config, value, _, state, client = setup
    value["meetings"]["give_up_days"] = days
    config.write_text(json.dumps(value))
    client.call.side_effect = [
        page([meeting(date="2030-05-01T09:00:00Z")]),
        detail(status=status, text=""),
    ]
    assert meetings.main(["--config", str(config)]) == 0
    assert bool(json.loads(state.read_text())["synced_ids"]) is synced


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": {}},
        {"data": {"notes": None}},
        {"data": {"notes": [{}]}},
        {"data": {"notes": [], "has_more": True}},
        {"data": {"notes": [], "has_more": "yes"}},
        {"data": {"notes": [], "error": "synthetic error"}},
    ],
)
def test_malformed_meeting_pages_fail_without_output(setup, response):
    config, _, root, state, client = setup
    client.call.side_effect = [response]
    assert meetings.main(["--config", str(config)]) == 1
    assert not state.exists()
    assert not (root / "meetings").exists()


@pytest.mark.parametrize(
    "response",
    [
        {"data": {}},
        {"data": {"meeting": {}}},
        {"data": {"meeting": {"status": "COMPLETED", "id": "other-id"}}},
        {"data": {"meeting": {"status": "COMPLETED", "transcript": ["invalid"]}}},
        {"data": {"meeting": {"status": "COMPLETED", "error": "synthetic failure"}}},
    ],
)
def test_malformed_details_never_become_permanently_synced(setup, response):
    config, _, root, state, client = setup
    client.call.side_effect = [page([meeting(status="COMPLETED")]), response]
    assert meetings.main(["--config", str(config)]) == 1
    assert not state.exists()
    assert not (root / "meetings").exists()


def test_repeated_pagination_token_fails_instead_of_looping(setup):
    config, _, root, state, client = setup
    client.call.side_effect = [
        page([meeting()], more=True, token="repeat"),
        page([], more=True, token="repeat"),
    ]
    assert meetings.main(["--config", str(config)]) == 1
    assert client.call.call_count == 2
    assert not state.exists()
    assert not (root / "meetings").exists()


def test_missing_pagination_flag_on_full_page_fails(setup):
    config, _, _, state, client = setup
    client.call.side_effect = [{"session_state": {"meetings": [meeting()]}}]
    assert meetings.main(["--config", str(config), "--page-size", "1"]) == 1
    assert not state.exists()


def test_empty_page_with_continuation_is_followed(setup):
    config, _, _, state, client = setup
    client.call.side_effect = [page([], more=True, token="next"), page([meeting()]), detail()]
    assert meetings.main(["--config", str(config)]) == 0
    assert json.loads(state.read_text())["synced_ids"] == ["example-id"]


@pytest.mark.parametrize(
    "arguments", [["--page-size", "0"], ["--page-size", "51"], ["--give-up-days", "-1"]]
)
def test_invalid_meeting_options_fail_before_service(setup, arguments):
    config, _, _, _, client = setup
    assert meetings.main(["--config", str(config), *arguments]) == 1
    client.call.assert_not_called()


@pytest.mark.parametrize("flag", ["--doctor", "--dry-run"])
def test_meeting_local_checks_do_not_call_service_or_write_state(setup, flag):
    config, _, root, state, client = setup
    assert meetings.main(["--config", str(config), flag]) == 0
    client.call.assert_not_called()
    assert not state.exists()
    assert list(root.iterdir()) == []


def test_write_failure_preserves_progress(setup, monkeypatch):
    config, _, _, state, client = setup
    client.call.side_effect = [page([meeting()]), detail()]
    monkeypatch.setattr(
        meetings, "write_text", Mock(side_effect=OSError("synthetic write failure"))
    )
    assert meetings.main(["--config", str(config)]) == 1
    assert not state.exists()


def test_already_synced_records_do_not_fetch_details(setup):
    config, _, root, state, client = setup
    state.parent.mkdir()
    state.write_text(json.dumps({"synced_ids": ["example-id"]}))
    client.call.side_effect = [page([meeting()])]
    assert meetings.main(["--config", str(config)]) == 0
    assert client.call.call_count == 1
    assert not (root / "meetings").exists()


def test_public_meeting_script_uses_configured_synthetic_cli(setup, tmp_path):
    config, value, root, state, _ = setup
    fake = tmp_path / "fake-cli.py"
    fake.write_text(
        "import json,sys\n"
        + f"record = {meeting(status='COMPLETED')!r}\n"
        + "print(json.dumps({'data': {'notes': [record], 'has_more': False}} if sys.argv[2] == 'list' else {'data': {'meeting': {**record, 'transcript': 'Synthetic transcript'}}}))\n"
    )
    value["command"] = [sys.executable, str(fake)]
    config.write_text(json.dumps(value))
    script = Path(__file__).resolve().parents[1] / "scripts/sync-meetings"
    env = {**os.environ, "GENSPARK_ARCHIVE_PYTHON": sys.executable, "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(script), "--config", str(config)], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text())["synced_ids"] == ["example-id"]
    assert "Synthetic transcript" in next((root / "meetings").glob("*.md")).read_text()
