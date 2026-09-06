"""Message normalization, chat selection, pagination and registry behavior."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "teams_archive.py"
SPEC = importlib.util.spec_from_file_location("sync_teams_for_tests", SCRIPT)
TEAMS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = TEAMS
SPEC.loader.exec_module(TEAMS)


def system_event(odata_type: str, *, deleted: bool = False, initiator: dict | None = None) -> dict:
    """Shape of a Graph meeting-thread system event (messageType is
    'unknownFutureValue' on current Graph, 'systemEventMessage' per docs)."""
    return {
        "id": "EXAMPLE_SYSTEM_EVENT_ID",
        "messageType": "unknownFutureValue",
        "createdDateTime": "2026-01-13T04:00:38.847Z",
        "deletedDateTime": "2026-01-13T05:00:00Z" if deleted else None,
        "from": None,
        "body": {"contentType": "html", "content": "<systemEventMessage/>"},
        "eventDetail": {
            "@odata.type": odata_type,
            "initiator": initiator or {"application": None, "device": None, "user": None},
        },
    }


def image_only_message(alt: str = "") -> dict:
    """Shape of a synthetic user message whose whole body is one hosted image."""
    alt_attr = f' alt="{alt}"' if alt else ""
    return {
        "id": "EXAMPLE_MESSAGE_ID",
        "messageType": "message",
        "createdDateTime": "2026-01-13T03:18:16.357Z",
        "deletedDateTime": None,
        "from": {"application": None, "device": None,
                 "user": {"@odata.type": "#microsoft.graph.teamworkUserIdentity",
                          "id": "00000000-0000-0000-0000-000000000000",
                          "displayName": "User A"}},
        "body": {"contentType": "html",
                 "content": ('<p><img src="https://graph.microsoft.com/v1.0/chats/'
                             '19:meeting_PLACEHOLDER@thread.v2/messages/EXAMPLE_MESSAGE_ID/'
                             f'hostedContents/PLACEHOLDER/$value"{alt_attr} '
                             'width="250" height="120"></p>')},
        "attachments": [],
    }


class NormSystemEventTests(unittest.TestCase):
    def test_call_lifecycle_events_are_represented(self):
        cases = {
            "#microsoft.graph.callStartedEventMessageDetail": "*[call started]*",
            "#microsoft.graph.callEndedEventMessageDetail": "*[call ended]*",
            "#microsoft.graph.callRecordingEventMessageDetail": "*[call recording]*",
            "#microsoft.graph.callTranscriptEventMessageDetail": "*[call transcript]*",
            "#microsoft.graph.membersJoinedEventMessageDetail": "*[members joined]*",
            "#microsoft.graph.membersLeftEventMessageDetail": "*[members left]*",
            "#microsoft.graph.chatRenamedEventMessageDetail": "*[chat renamed]*",
        }
        for odata_type, expected in cases.items():
            with self.subTest(odata_type=odata_type):
                n = TEAMS.norm_message(system_event(odata_type))
                self.assertIsNotNone(n)
                self.assertEqual(n["body"], expected)
                self.assertEqual(n["sender"], "(system)")
                self.assertEqual(n["ts"], "2026-01-13T04:00:38Z")
                self.assertEqual(n["id"], "EXAMPLE_SYSTEM_EVENT_ID")

    def test_initiator_display_name_used_when_present(self):
        ev = system_event("#microsoft.graph.membersJoinedEventMessageDetail",
                          initiator={"user": {"displayName": "User B"}})
        n = TEAMS.norm_message(ev)
        self.assertEqual(n["sender"], "User B")

    def test_deleted_system_event_is_dropped(self):
        n = TEAMS.norm_message(
            system_event("#microsoft.graph.callStartedEventMessageDetail", deleted=True))
        self.assertIsNone(n)

    def test_exotic_type_without_event_detail_is_dropped(self):
        msg = system_event("#microsoft.graph.callStartedEventMessageDetail")
        del msg["eventDetail"]
        self.assertIsNone(TEAMS.norm_message(msg))

    def test_missing_odata_type_still_representable(self):
        msg = system_event("")
        msg["eventDetail"] = {"initiator": None}
        n = TEAMS.norm_message(msg)
        self.assertEqual(n["body"], "*[system event]*")


class ImageOnlyMessageTests(unittest.TestCase):
    def test_image_only_message_leaves_marker(self):
        n = TEAMS.norm_message(image_only_message())
        self.assertIsNotNone(n)
        self.assertEqual(n["body"], "*[image]*")
        self.assertEqual(n["sender"], "User A")

    def test_emoji_img_alt_text_recovered(self):
        n = TEAMS.norm_message(image_only_message(alt="\U0001f44d"))
        self.assertEqual(n["body"], "\U0001f44d")

    def test_plain_text_message_unchanged(self):
        msg = image_only_message()
        msg["body"] = {"contentType": "text", "content": "hello there"}
        n = TEAMS.norm_message(msg)
        self.assertEqual(n["body"], "hello there")

    def test_html_text_message_unchanged(self):
        msg = image_only_message()
        msg["body"] = {"contentType": "html", "content": "<p>plain <b>rich</b> text</p>"}
        n = TEAMS.norm_message(msg)
        self.assertEqual(n["body"], "plain **rich** text")

    def test_empty_html_without_images_still_dropped(self):
        msg = image_only_message()
        msg["body"] = {"contentType": "html", "content": "<p> </p>"}
        self.assertIsNone(TEAMS.norm_message(msg))


def reg_row(cid="19:abc@thread.v2", ctype="group", topic="", members=(), **extra):
    """A row in the shape a caller-owned chat directory writes."""
    row = {"id": cid, "type": ctype, "topic": topic, "members": list(members),
           "label": topic or " & ".join(members), "mirrored": False,
           "last_message_at": ""}
    row.update(extra)
    return row


class PeekRankTests(unittest.TestCase):
    def test_exact_topic_is_rank_0_case_insensitive(self):
        self.assertEqual(
            TEAMS.rank_chat_match("example all hands", reg_row(topic="Example All Hands")), 0)

    def test_one_on_one_member_is_rank_1(self):
        self.assertEqual(
            TEAMS.rank_chat_match("alice", reg_row(
                ctype="oneOnOne", members=["Alice Example", "Example Reader"])), 1)

    def test_group_topic_substring_is_rank_2(self):
        self.assertEqual(
            TEAMS.rank_chat_match("all hands", reg_row(topic="Example All Hands")), 2)

    def test_group_member_substring_is_rank_3(self):
        self.assertEqual(
            TEAMS.rank_chat_match("bob", reg_row(
                topic="Example Project", members=["Bob Example", "Example Reader"])), 3)

    def test_no_field_matches_returns_none(self):
        self.assertIsNone(
            TEAMS.rank_chat_match("nomatch", reg_row(
                topic="Example All Hands", members=["Alice Example"])))

    def test_one_on_one_resolves_by_member_not_topic_substring(self):
        # spec: the topic-substring ranks exist for group chats; a 1:1 chat is
        # found by member name (or exact topic), never by topic fragment
        self.assertIsNone(
            TEAMS.rank_chat_match("hand", reg_row(
                ctype="oneOnOne", topic="Handoff", members=["Bob"])))

    def test_empty_needle_never_matches(self):
        self.assertIsNone(TEAMS.rank_chat_match("  ", reg_row(topic="Anything")))

    def test_live_graph_shape_with_member_dicts(self):
        chat = {"id": "19:x@unq.gbl.spaces", "chatType": "oneOnOne", "topic": None,
                "members": [{"displayName": "Alice Example"}, {"displayName": "Example Reader"}]}
        self.assertEqual(TEAMS.rank_chat_match("alice example", chat), 1)


class PeekSelectTests(unittest.TestCase):
    def test_unique_best_rank_wins_over_lower_ranks(self):
        rows = [reg_row(cid="19:sub@t", topic="Example All Hands Fun"),      # substring
                reg_row(cid="19:exact@t", topic="Example All Hands"),        # exact
                reg_row(cid="19:mem@t", topic="Random", members=["Example All Hands Bot"])]
        winner, rank, ties = TEAMS.select_peek_chat("example all hands", rows)
        self.assertEqual(winner["id"], "19:exact@t")
        self.assertEqual(rank, 0)
        self.assertEqual(ties, [])

    def test_winner_independent_of_listing_order(self):
        rows = [reg_row(cid="19:exact@t", topic="Example All Hands"),
                reg_row(cid="19:sub@t", topic="Example All Hands Fun")]
        for ordering in (rows, list(reversed(rows))):
            winner, _, _ = TEAMS.select_peek_chat("example all hands", ordering)
            self.assertEqual(winner["id"], "19:exact@t")

    def test_tie_at_best_rank_returns_candidates_not_first(self):
        rows = [reg_row(cid="19:a@t", topic="Design Review Alpha"),
                reg_row(cid="19:b@t", topic="Design Review Beta")]
        winner, rank, ties = TEAMS.select_peek_chat("design review", rows)
        self.assertIsNone(winner)
        self.assertEqual(rank, 2)
        self.assertEqual({c["id"] for c in ties}, {"19:a@t", "19:b@t"})

    def test_exact_topic_disambiguates_a_substring_tie(self):
        # when several chats contain the words,
        # the intended one IS the exact topic — it must win outright
        rows = [reg_row(cid="19:a@t", topic="Design Review Alpha"),
                reg_row(cid="19:b@t", topic="Design Review")]
        winner, rank, _ = TEAMS.select_peek_chat("design review", rows)
        self.assertEqual(winner["id"], "19:b@t")
        self.assertEqual(rank, 0)

    def test_no_match_returns_empty(self):
        self.assertEqual(TEAMS.select_peek_chat("zzz", [reg_row(topic="Abc")]),
                         (None, None, []))


class PeekResolveTests(unittest.TestCase):
    """resolve_peek_target flow: id passthrough, registry-first, one live
    refresh on miss, candidate print on tie. No file or network access."""

    @staticmethod
    def _fail(*_a, **_k):
        raise AssertionError("must not be called on this path")

    def test_chat_id_prefix_bypasses_registry_and_network(self):
        with mock.patch.object(TEAMS, "load_registry_rows", self._fail), \
             mock.patch.object(TEAMS, "live_chat_rows", self._fail):
            cid, label = TEAMS.resolve_peek_target("19:xyz@thread.v2", {})
        self.assertEqual(cid, "19:xyz@thread.v2")
        self.assertEqual(label, "19:xyz@thread.v2")

    def test_registry_hit_skips_live_enumeration(self):
        rows = [reg_row(cid="19:hit@t", topic="Example All Hands")]
        with mock.patch.object(TEAMS, "load_registry_rows", return_value=rows), \
             mock.patch.object(TEAMS, "live_chat_rows", self._fail):
            cid, label = TEAMS.resolve_peek_target("example all hands", {})
        self.assertEqual(cid, "19:hit@t")
        self.assertEqual(label, "Example All Hands")

    def test_registry_miss_triggers_exactly_one_live_refresh(self):
        live = [reg_row(cid="19:live@t", topic="Fresh Chat")]
        live_mock = mock.Mock(return_value=live)
        with mock.patch.object(TEAMS, "load_registry_rows", return_value=[]), \
             mock.patch.object(TEAMS, "live_chat_rows", live_mock):
            cid, _ = TEAMS.resolve_peek_target("fresh chat", {})
        self.assertEqual(cid, "19:live@t")
        self.assertEqual(live_mock.call_count, 1)

    def test_live_unavailable_resolves_to_none(self):
        with mock.patch.object(TEAMS, "load_registry_rows", return_value=[]), \
             mock.patch.object(TEAMS, "live_chat_rows", return_value=None):
            self.assertEqual(TEAMS.resolve_peek_target("anything", {}), (None, ""))

    def test_tie_prints_candidates_and_does_not_refresh(self):
        rows = [reg_row(cid="19:a@t", topic="Design Review Alpha"),
                reg_row(cid="19:b@t", topic="Design Review Beta")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(TEAMS, "load_registry_rows", return_value=rows), \
             mock.patch.object(TEAMS, "live_chat_rows", self._fail), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = TEAMS.resolve_peek_target("design review", {})
        self.assertEqual(result, (None, ""))
        printed = out.getvalue() + err.getvalue()
        self.assertIn("19:a@t", printed)
        self.assertIn("19:b@t", printed)
        self.assertIn("group topic substring", printed)

    def test_registry_miss_with_live_tie_prints_candidates(self):
        live = [reg_row(cid="19:a@t", topic="Design Review Alpha"),
                reg_row(cid="19:b@t", topic="Design Review Beta")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(TEAMS, "load_registry_rows", return_value=[]), \
             mock.patch.object(TEAMS, "live_chat_rows", return_value=live), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = TEAMS.resolve_peek_target("design review", {})
        self.assertEqual(result, (None, ""))
        self.assertIn("19:a@t", out.getvalue() + err.getvalue())


class PeekReadBudgetTests(unittest.TestCase):
    """The graph message read floors max_pages at 200 for sync-backlog
    correctness; a peek read must honor its tiny page budget literally."""

    def _paged(self, chat_id="19:x@t", since=None, **kw):
        calls = {}

        def fake_paginate(url, token, params, max_pages, *, partial_ok=False):
            calls["max_pages"] = max_pages
            calls["partial_ok"] = partial_ok
            return [], True

        with mock.patch.object(TEAMS, "graph_token", return_value="tok"), \
             mock.patch.object(TEAMS, "graph_paginate", fake_paginate):
            TEAMS.read_chat_graph(chat_id, since, max_pages=1, **kw)
        return calls

    def test_peek_read_honors_exact_page_budget(self):
        calls = self._paged(exact_pages=True)
        self.assertEqual(calls["max_pages"], 1)
        self.assertTrue(calls["partial_ok"])

    def test_sync_read_keeps_the_200_page_backlog_floor(self):
        calls = self._paged()
        self.assertEqual(calls["max_pages"], 200)
        self.assertFalse(calls["partial_ok"])


class RegistryWriteTests(unittest.TestCase):
    def test_write_preserves_last_message_at_and_schema(self):
        live_chats = [
            {"id": "19:eng@t", "chatType": "group", "topic": "Example All Hands",
             "members": [{"displayName": "Example Reader"}, {"displayName": "Bob Example"}]},
            {"id": "19:new@t", "chatType": "oneOnOne", "topic": None,
             "members": [{"displayName": "Alice Example"}]},
        ]
        cfg = {"chats": [{"match": "example all hands"}]}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "teams-chats.json"
            path.write_text(json.dumps({"refreshed": "2026-01-01T00:00:00+00:00",
                                        "chats": [reg_row(cid="19:eng@t",
                                                          topic="Example All Hands",
                                                          last_message_at="2026-01-05T12:00:00Z")]}))
            with mock.patch.object(TEAMS, "registry_path", return_value=path):
                TEAMS.write_registry(live_chats, cfg)
            data = json.loads(path.read_text())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
        rows = {r["id"]: r for r in data["chats"]}
        self.assertEqual(set(rows), {"19:eng@t", "19:new@t"})
        for r in rows.values():
            self.assertEqual(set(r), {"id", "type", "topic", "members", "label",
                                      "mirrored", "last_message_at"})
        self.assertEqual(rows["19:eng@t"]["last_message_at"], "2026-01-05T12:00:00Z")
        self.assertTrue(rows["19:eng@t"]["mirrored"])
        self.assertEqual(rows["19:new@t"]["last_message_at"], "")
        self.assertFalse(rows["19:new@t"]["mirrored"])
        self.assertEqual(rows["19:new@t"]["members"], ["Alice Example"])


if __name__ == "__main__":
    unittest.main()
