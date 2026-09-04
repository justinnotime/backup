from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.api import run
from agent_skills.sessions.identity import base_filename, date_for
from agent_skills.sessions.model import NORMALIZED_SCHEMA_VERSION, Event, Session
from agent_skills.sessions.slicing import slice_sessions_by_day, split_session


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def make_session(specs, session_id="session-x") -> Session:
    events = tuple(
        Event(
            index,
            ts(stamp) if stamp else None,
            "exact" if stamp else "unknown",
            role,
            text,
            "message",
        )
        for index, (stamp, role, text) in enumerate(specs)
    )
    stamps = [event.timestamp for event in events if event.timestamp]
    return Session(
        NORMALIZED_SCHEMA_VERSION,
        "claude-code",
        session_id,
        "source-a/x.jsonl",
        "node-a",
        "/srv/example/project-one",
        "project-one",
        min(stamps) if stamps else None,
        max(stamps) if stamps else None,
        events,
        {},
    )


class SplitSessionTest(unittest.TestCase):
    def test_new_day_starts_at_utc_midnight_whatever_the_role(self):
        session = make_session(
            [
                ("2026-02-03T23:50:00Z", "user", "q1"),
                ("2026-02-03T23:51:00Z", "assistant", "a1"),
                ("2026-02-04T00:10:00Z", "assistant", "a1 continues past midnight"),
                ("2026-02-04T08:00:00Z", "user", "q2"),
                ("2026-02-04T08:01:00Z", "assistant", "a2"),
            ]
        )
        slices = split_session(session)
        self.assertEqual([item.day for item in slices], ["2026-02-03", "2026-02-04"])
        self.assertEqual([event.text for event in slices[0].events], ["q1", "a1"])
        self.assertEqual(
            [event.text for event in slices[1].events],
            ["a1 continues past midnight", "q2", "a2"],
        )
        # The session start is kept on every slice; the end is the slice's own.
        self.assertEqual(slices[1].started_at, session.started_at)
        self.assertEqual(slices[0].ended_at, ts("2026-02-03T23:51:00Z"))
        self.assertEqual(slices[1].ended_at, session.ended_at)
        self.assertEqual(slices[0].identity, ("claude-code", "node-a", "session-x@2026-02-03"))
        self.assertEqual(date_for(slices[1]), "2026-02-04")
        self.assertTrue(base_filename(slices[1], "session-prefix-8").startswith("2026-02-04_"))
        self.assertEqual(session.identity, ("claude-code", "node-a", "session-x"))

    def test_single_day_session_is_tagged_with_its_day(self):
        session = make_session(
            [("2026-02-03T10:00:00Z", "user", "q"), ("2026-02-03T10:01:00Z", "assistant", "a")]
        )
        (only,) = split_session(session)
        self.assertEqual(only.day, "2026-02-03")
        self.assertEqual(only.events, session.events)

    def test_assistant_only_activity_after_midnight_still_starts_a_new_day(self):
        # Autonomous agents keep replying for hours without a user turn; the
        # earlier day's file must not keep growing through that.
        session = make_session(
            [
                ("2026-02-03T23:50:00Z", "user", "q1"),
                ("2026-02-04T00:20:00Z", "assistant", "still working"),
                ("2026-02-04T06:21:00Z", "assistant", "done"),
            ]
        )
        slices = split_session(session)
        self.assertEqual([item.day for item in slices], ["2026-02-03", "2026-02-04"])
        self.assertEqual(len(slices[0].events), 1)
        self.assertEqual(len(slices[1].events), 2)

    def test_untimestamped_session_stays_whole_and_untagged(self):
        session = make_session([(None, "user", "q"), (None, "assistant", "a")])
        self.assertEqual(split_session(session), (session,))
        self.assertIsNone(split_session(session)[0].day)

    def test_off_mode_returns_sessions_unchanged(self):
        session = make_session(
            [("2026-02-03T23:50:00Z", "user", "q1"), ("2026-02-04T08:00:00Z", "user", "q2")]
        )
        self.assertEqual(slice_sessions_by_day((session,), mode="off"), (session,))
        with self.assertRaises(ValueError):
            slice_sessions_by_day((session,), mode="weekly")


def write_multiday_claude(path: Path, *, session_id="claude-session-multi", days=2) -> None:
    cwd = "/srv/example/project-one"
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": "2026-02-03T23:50:00Z",
            "message": {"content": "first day request"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-02-03T23:51:00Z",
            "message": {"content": [{"type": "text", "text": "first day answer"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-02-04T00:10:00Z",
            "message": {
                "content": [{"type": "text", "text": "answer continues past midnight"}]
            },
        },
    ]
    if days >= 2:
        records += [
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": cwd,
                "timestamp": "2026-02-04T08:00:00Z",
                "message": {"content": "second day request"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-02-04T08:01:00Z",
                "message": {"content": [{"type": "text", "text": "second day answer"}]},
            },
        ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


class DaySplitPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        write_multiday_claude(self.source / "session.jsonl")
        self.output = self.root / "output"
        self.output.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manifest(self, mode: str | None) -> Path:
        data = manifest_data(
            self.source, self.output, publisher="filesystem-atomic", cleanup="owner"
        )
        if mode is not None:
            data["output"]["day_split"] = mode
        return write_manifest(self.root / f"manifest-{mode}.json", data)

    def history_files(self) -> list[Path]:
        return sorted((self.output / "History").rglob("*.md"))

    def prompt_files(self) -> list[Path]:
        return sorted((self.output / "Prompts").rglob("*.md"))

    def test_default_and_off_keep_one_file_per_session(self):
        report = run(self.manifest(None))
        self.assertEqual(report.status, "ok")
        files = self.history_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("2026-02-03_"))
        self.assertNotIn("- Day:", files[0].read_text(encoding="utf-8"))

    def test_hybrid_slices_a_new_session_per_day_and_is_idempotent(self):
        report = run(self.manifest("hybrid"))
        self.assertEqual(report.status, "ok")
        files = self.history_files()
        self.assertEqual([path.name[:10] for path in files], ["2026-02-03", "2026-02-04"])
        self.assertEqual([path.parent.name for path in files], ["2026-02", "2026-02"])
        first = files[0].read_text(encoding="utf-8")
        second = files[1].read_text(encoding="utf-8")
        self.assertIn("- Day: 2026-02-03", first)
        self.assertIn("- Day: 2026-02-04", second)
        self.assertIn("- Session: claude-session-multi", first)
        self.assertNotIn("answer continues past midnight", first)
        self.assertIn("answer continues past midnight", second)
        self.assertNotIn("second day request", first)
        self.assertIn("second day request", second)
        self.assertNotIn("first day request", second)
        # Both slices state the session's real start; only the end differs.
        self.assertIn("- Started: 2026-02-03 23:50:00Z", second)
        self.assertIn("- Ended: 2026-02-03 23:51:00Z", first)
        prompts = self.prompt_files()
        self.assertEqual([path.name[:10] for path in prompts], ["2026-02-03", "2026-02-04"])
        self.assertIn("first day request", prompts[0].read_text(encoding="utf-8"))
        self.assertIn("second day request", prompts[1].read_text(encoding="utf-8"))
        # The inventory round-trips the Day header into the identity, so a
        # second run has nothing to do.
        again = run(self.manifest("hybrid"))
        self.assertEqual((again.write_count, again.removal_count), (0, 0))
        self.assertEqual(len(self.history_files()), 2)

    def test_hybrid_keeps_a_session_with_existing_output_whole_as_it_grows(self):
        write_multiday_claude(self.source / "session.jsonl", days=1)
        run(self.manifest("off"))
        self.assertEqual(len(self.history_files()), 1)
        # The session continues on the next day after the switch to hybrid.
        write_multiday_claude(self.source / "session.jsonl", days=2)
        report = run(self.manifest("hybrid"))
        self.assertEqual(report.removal_count, 0)
        files = self.history_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("second day request", text)
        self.assertNotIn("- Day:", text)
        self.assertEqual(len(self.prompt_files()), 1)

    def test_all_replaces_an_existing_whole_file_with_slices(self):
        run(self.manifest("off"))
        self.assertEqual(len(self.history_files()), 1)
        whole = self.history_files()[0]
        report = run(self.manifest("all"))
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.removal_count, 2)  # one history, one prompt file
        files = self.history_files()
        self.assertEqual([path.name[:10] for path in files], ["2026-02-03", "2026-02-04"])
        # The first day's slice takes over the path the whole file vacated:
        # no collision suffix on a migrated session's first day.
        self.assertEqual(files[0], whole)
        self.assertIn("- Day: 2026-02-03", whole.read_text(encoding="utf-8"))
        again = run(self.manifest("all"))
        self.assertEqual((again.write_count, again.removal_count), (0, 0))


if __name__ == "__main__":
    unittest.main()
