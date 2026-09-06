import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from genspark_archive import calendar
from genspark_archive.common import ArchiveError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    path = tmp_path / "config.json"
    value = {
        "schema": "genspark-archive/v1",
        "repository_root": str(root),
        "rate_delay": 0,
        "calendar": {"output_directory": "calendar", "account": "reader@example.invalid"},
    }
    path.write_text(json.dumps(value))
    client = Mock()
    client.call.side_effect = ArchiveError("unexpected service call")
    monkeypatch.setattr(calendar, "Client", Mock(return_value=client))
    monkeypatch.setattr(calendar, "utc_now", lambda: datetime(2030, 5, 17, 12, tzinfo=timezone.utc))
    return path, value, root, client


def event(title="Example event", date="2030-05-16"):
    return {
        "title": title,
        "start": {"dateTime": date + "T10:00:00Z"},
        "end": {"dateTime": date + "T11:00:00Z"},
        "organizer": {"emailAddress": {"name": "Example organizer"}},
        "attendees": [
            {"emailAddress": {"address": "person@example.invalid"}},
            {"email": "guest@example.invalid"},
        ],
        "location": {"displayName": "Example room"},
        "description": "Original event description",
    }


def answer(client, value):
    client.call.side_effect = None
    client.call.return_value = value


def test_calendar_preserves_full_description_and_event_formats():
    source = event()
    source["description"] = "word " * 150
    rendered = calendar.event_to_markdown(source)
    assert source["description"] in rendered
    assert "## 2030-05-16T10:00 — Example event" in rendered
    assert "**Organizer:** Example organizer" in rendered
    assert "**Attendees:** person@example.invalid, guest@example.invalid" in rendered
    assert "**Location:** Example room" in rendered
    assert calendar.event_to_markdown(
        {"summary": "All day", "start": {"date": "2030-05-16"}, "end": {"date": "2030-05-17"}}
    ).startswith("## 2030-05-16 — All day")


def test_source_provided_attendee_preview_preserves_count_and_limit_note():
    source = event()
    source["attendees"] = {
        "total": 25,
        "preview": ["Example One", "Example Two"],
        "note": "The source returned only the first two participants.",
    }
    source["description"] = "Available source text [truncated by source]"
    rendered = calendar.event_to_markdown(source)
    assert "**Attendees (available preview):** Example One, Example Two" in rendered
    assert "**Attendee count:** 25" in rendered
    assert source["attendees"]["note"] in rendered
    assert source["description"] in rendered


@pytest.mark.parametrize(
    "attendees",
    [
        {"preview": []},
        {"total": 0, "preview": ["unexpected"]},
        {"total": 1, "preview": [None]},
        {"total": 1, "preview": "not-array"},
    ],
)
def test_malformed_attendee_preview_fails(attendees):
    source = event()
    source["attendees"] = attendees
    with pytest.raises(ArchiveError):
        calendar.event_to_markdown(source)


def test_complete_calendar_uses_explicit_limit_and_stable_quarter_file(setup):
    config, _, root, client = setup
    answer(
        client, {"data": {"events": [event("Later", "2030-05-16"), event("Earlier", "2030-05-15")]}}
    )
    assert calendar.main(["--config", str(config)]) == 0
    path = root / "calendar/2030-Q2-events.md"
    text = path.read_text()
    assert text.index("Earlier") < text.index("Later")
    assert text.startswith("# Calendar Events (2030-Q2)")
    arguments = client.call.call_args.args[0]
    assert arguments[arguments.index("--limit") + 1] == "1000"
    assert arguments[-2:] == ["-a", "reader@example.invalid"]
    before = path.stat().st_mtime_ns
    assert calendar.main(["--config", str(config)]) == 0
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": None},
        {"data": {}},
        {"data": {"events": None}},
        {"data": {"events": ["bad"]}},
        {"data": {"events": [{}]}},
        {"data": {"events": [], "error": "synthetic failure"}},
        {"data": {"events": [], "has_more": True}},
        {"data": {"events": [event()], "truncated": True}},
        {"data": {"events": [], "total": 1}},
    ],
)
def test_invalid_or_incomplete_calendar_never_overwrites_existing_archive(setup, response):
    config, _, root, client = setup
    path = root / "calendar/2030-Q2-events.md"
    path.parent.mkdir()
    path.write_text("existing complete calendar")
    answer(client, response)
    assert calendar.main(["--config", str(config)]) == 1
    assert path.read_text() == "existing complete calendar"


def test_exact_limit_is_incomplete_even_without_pagination_flag(setup):
    config, _, root, client = setup
    answer(client, {"data": {"events": [event()]}})
    assert calendar.main(["--config", str(config), "--list-limit", "1"]) == 1
    assert not (root / "calendar").exists()


def test_empty_successful_calendar_is_distinct_from_missing_collection(setup):
    config, _, root, client = setup
    answer(client, {"session_state": {"calendar_events": []}})
    assert calendar.main(["--config", str(config)]) == 0
    assert (root / "calendar/2030-Q2-events.md").read_text() == "# Calendar Events (2030-Q2)\n"


def test_cli_parameters_override_calendar_config(setup):
    config, value, _, client = setup
    value["calendar"].update(days_back=5, days_forward=10, list_limit=75)
    config.write_text(json.dumps(value))
    answer(client, {"data": {"events": []}})
    assert calendar.main(["--config", str(config), "--days-back", "2", "--list-limit", "88"]) == 0
    argv = client.call.call_args.args[0]
    assert argv[argv.index("--time_min") + 1] == "2030-05-15T00:00:00Z"
    assert argv[argv.index("--time_max") + 1] == "2030-05-27T23:59:59Z"
    assert argv[argv.index("--limit") + 1] == "88"


@pytest.mark.parametrize("flag", ["--doctor", "--dry-run"])
def test_calendar_local_checks_do_not_call_service_or_create_output(setup, flag):
    config, _, root, client = setup
    assert calendar.main(["--config", str(config), flag]) == 0
    client.call.assert_not_called()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "arguments", [["--days-back", "-1"], ["--days-forward", "-1"], ["--list-limit", "0"]]
)
def test_invalid_calendar_options_fail_before_service(setup, arguments):
    config, _, _, client = setup
    assert calendar.main(["--config", str(config), *arguments]) == 1
    client.call.assert_not_called()


def test_public_calendar_script_executes_only_configured_synthetic_cli(setup, tmp_path):
    config, value, root, _ = setup
    command_log = tmp_path / "argv.json"
    fake = tmp_path / "fake-cli.py"
    fake.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        + f"Path({str(command_log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        + "print(json.dumps({'data': {'events': []}}))\n"
    )
    value["command"] = [sys.executable, str(fake)]
    config.write_text(json.dumps(value))
    script = Path(__file__).resolve().parents[1] / "scripts/sync-calendar"
    env = {**os.environ, "GENSPARK_ARCHIVE_PYTHON": sys.executable, "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(script), "--config", str(config)], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(command_log.read_text())[:2] == ["calendar", "list"]
    assert len(list((root / "calendar").glob("*-events.md"))) == 1
