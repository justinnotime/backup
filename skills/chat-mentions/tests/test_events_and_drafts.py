"""Deterministic tests for mention-patrol event logic and the mention-drafts
draft box. No network, no Graph token, no real runtime root — the patrol's
pure functions are tested directly, and the drafts tool runs against a
temporary explicit state directory.
"""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chat_mentions import cli
from chat_mentions import drafts as DRAFTS
from chat_mentions import events as PATROL

ROOT = Path(__file__).resolve().parents[1]

ME = "00000000-me"
NOW = datetime(2025, 1, 2, 8, 0, 0, tzinfo=timezone.utc)


def msg(
    mid="m1",
    sender=("u1", "User One"),
    ts="2025-01-02T07:59:00Z",
    mentions=(),
    message_type="message",
    application=None,
    body="hello",
):
    return {
        "id": mid,
        "messageType": message_type,
        "createdDateTime": ts,
        "from": {
            "application": application,
            "user": {"id": sender[0], "displayName": sender[1]} if sender else None,
        },
        "body": {"contentType": "html", "content": f"<p>{body}</p>"},
        "mentions": [
            {"id": i, "mentioned": {"user": {"id": uid, "displayName": "X"}}}
            for i, uid in enumerate(mentions)
        ],
    }


class ClassifyTests(unittest.TestCase):
    def test_dm_from_someone_else_triggers(self):
        self.assertEqual(PATROL.classify_message(msg(), ME, True), "dm")

    def test_group_mention_of_me_triggers(self):
        self.assertEqual(
            PATROL.classify_message(msg(mentions=[ME]), ME, False), "mention"
        )

    def test_group_message_without_my_mention_is_skipped(self):
        self.assertIsNone(
            PATROL.classify_message(msg(mentions=["someone-else"]), ME, False)
        )

    def test_own_message_skipped_even_in_dm(self):
        self.assertIsNone(
            PATROL.classify_message(msg(sender=(ME, "Example Owner")), ME, True)
        )

    def test_application_sender_skipped(self):
        bot = msg(mentions=[ME], application={"id": "bot-app"})
        self.assertIsNone(PATROL.classify_message(bot, ME, False))

    def test_system_event_skipped(self):
        self.assertIsNone(
            PATROL.classify_message(msg(message_type="unknownFutureValue"), ME, True)
        )

    def test_team_style_mention_without_user_id_skipped(self):
        m = msg()
        m["mentions"] = [{"id": 0, "mentioned": {"conversation": {"id": "19:team"}}}]
        self.assertIsNone(PATROL.classify_message(m, ME, False))


class CapTests(unittest.TestCase):
    def test_under_cap_allows(self):
        times = [PATROL.iso(NOW - timedelta(minutes=10))] * 3
        allowed, kept = PATROL.cap_allows(times, NOW, cap=4)
        self.assertTrue(allowed)
        self.assertEqual(len(kept), 3)

    def test_at_cap_blocks(self):
        times = [PATROL.iso(NOW - timedelta(minutes=10))] * 4
        allowed, _ = PATROL.cap_allows(times, NOW, cap=4)
        self.assertFalse(allowed)

    def test_entries_older_than_an_hour_are_pruned_and_reallow(self):
        times = [PATROL.iso(NOW - timedelta(minutes=90))] * 4
        allowed, kept = PATROL.cap_allows(times, NOW, cap=4)
        self.assertTrue(allowed)
        self.assertEqual(kept, [])


class CollectEventsTests(unittest.TestCase):
    def test_dm_and_mention_events_carry_expected_fields(self):
        chat_state, senders = {}, {}
        events, suppressed = PATROL.collect_events(
            [msg(mid="a", body="ping")], chat_state, senders, ME, True, NOW
        )
        self.assertEqual(suppressed, 0)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["kind"], "dm")
        self.assertEqual(e["msg_id"], "a")
        self.assertEqual(e["sender_name"], "User One")
        self.assertIn("ping", e["preview"])

    def test_rerun_with_same_messages_is_idempotent(self):
        chat_state, senders = {}, {}
        batch = [msg(mid="a"), msg(mid="b", ts="2025-01-02T07:59:30Z")]
        first, _ = PATROL.collect_events(batch, chat_state, senders, ME, True, NOW)
        second, _ = PATROL.collect_events(batch, chat_state, senders, ME, True, NOW)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(chat_state["watermark"], "2025-01-02T07:59:30Z")

    def test_sender_over_cap_is_suppressed_not_emitted(self):
        chat_state = {}
        senders = {"u1": [PATROL.iso(NOW - timedelta(minutes=5))] * 4}
        events, suppressed = PATROL.collect_events(
            [msg(mid="a")], chat_state, senders, ME, True, NOW, cap=4
        )
        self.assertEqual(events, [])
        self.assertEqual(suppressed, 1)
        # the suppressed message is still marked seen — no replay next run
        self.assertIn("a", chat_state["seen"])

    def test_non_trigger_messages_still_advance_watermark(self):
        chat_state, senders = {}, {}
        events, _ = PATROL.collect_events(
            [msg(mid="own", sender=(ME, "Example Owner"), ts="2025-01-02T07:58:00Z")],
            chat_state,
            senders,
            ME,
            True,
            NOW,
        )
        self.assertEqual(events, [])
        self.assertEqual(chat_state["watermark"], "2025-01-02T07:58:00Z")


class DraftBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "schema": "chat-mentions/v1",
                    "state_directory": "state/mention-patrol",
                }
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["--config", str(self.config), *argv]) == 0
        return out.getvalue()

    def test_new_list_dismiss_mark_sent_lifecycle(self):
        path = self.run_cmd(
            "new",
            "--chat-id",
            "19:x@t",
            "--msg-id",
            "17860001",
            "--topic",
            "Example group",
            "--sender",
            "Example Person",
            "--body",
            "draft reply text",
        ).strip()
        self.assertTrue(Path(path).is_file())

        listing = self.run_cmd("list")
        self.assertIn("pending", listing)
        self.assertIn("Example group", listing)

        shown = self.run_cmd("show", "17860001")
        self.assertIn("draft reply text", shown)
        self.assertIn("status: pending", shown)

        self.run_cmd("dismiss", "17860001")
        self.assertIn("dismissed", self.run_cmd("list", "--status", "dismissed"))
        self.assertIn("(no pending drafts)", self.run_cmd("list"))

    def test_mark_sent_records_note_but_tool_has_no_network(self):
        self.run_cmd(
            "new", "--chat-id", "19:x@t", "--msg-id", "m2", "--body", "second draft"
        )
        out = self.run_cmd("mark-sent", "m2", "--note", "platform-id-123")
        self.assertIn("teams-send", out)
        shown = self.run_cmd("show", "m2")
        self.assertIn("status: sent", shown)
        self.assertIn("platform-id-123", shown)
        source = (ROOT / "src/chat_mentions/drafts.py").read_text().lower()
        for needle in ("requests", "urllib", "socket", "msal", "http"):
            self.assertNotIn(
                needle, source, f"draft tool must stay network-free (found {needle!r})"
            )

    def test_open_joins_queue_against_draft_box(self):
        qdir = Path(self.tmp.name) / "state" / "mention-patrol"
        qdir.mkdir(parents=True)
        event = {
            "kind": "mention",
            "msg_id": "q1",
            "ts": "2025-01-02T06:15:50Z",
            "sender_name": "Example Sender",
            "chat_topic": "Example group",
            "chat_id": "19:x@t",
            "preview": "Example Owner ping",
        }
        (qdir / "queue.jsonl").write_text(json.dumps(event) + "\n")

        out = self.run_cmd("open")
        self.assertIn("q1", out)
        self.assertIn("Example Sender", out)

        self.run_cmd(
            "new", "--chat-id", "19:x@t", "--msg-id", "q1", "--body", "reply draft"
        )
        self.assertIn("(queue fully handled", self.run_cmd("open"))
        # a second draft for the same msg_id is refused even under a
        # different topic slug
        with self.assertRaises(SystemExit):
            self.run_cmd(
                "new",
                "--chat-id",
                "19:x@t",
                "--msg-id",
                "q1",
                "--topic",
                "Different Topic",
                "--body",
                "second try",
            )
        # dismissed drafts also keep the event closed
        self.run_cmd("dismiss", "q1")
        self.assertIn("(queue fully handled", self.run_cmd("open"))

    def test_pending_draft_older_than_expiry_lists_as_expired(self):
        path = Path(
            self.run_cmd(
                "new", "--chat-id", "19:x@t", "--msg-id", "m3", "--body", "old draft"
            ).strip()
        )
        meta, body = DRAFTS.parse(path.read_text())
        meta["created"] = DRAFTS.iso(
            datetime.now(timezone.utc) - timedelta(hours=DRAFTS.EXPIRE_HOURS + 1)
        )
        path.write_text(DRAFTS.render(meta, body))
        self.assertIn("expired", self.run_cmd("list", "--status", "expired"))
        self.assertIn("(no pending drafts)", self.run_cmd("list"))


if __name__ == "__main__":
    unittest.main()
