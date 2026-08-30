from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_skills.sessions.harnesses import decoder_for
from agent_skills.sessions.harnesses.claude import ClaudeDecoder
from agent_skills.sessions.harnesses.codex import CodexDecoder
from agent_skills.sessions.harnesses.cursor import CursorDecoder
from agent_skills.sessions.harnesses.openclaw import OpenClawDecoder
from agent_skills.sessions.harnesses.opencode import OpenCodeDecoder
from agent_skills.sessions.model import SourceSnapshot


def jsonl(*records: dict) -> bytes:
    return b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)


def snapshot(
    harness: str,
    payload: bytes | None,
    *,
    path: Path | None = None,
    options: dict | None = None,
    source_ref: str | None = None,
) -> SourceSnapshot:
    return SourceSnapshot(
        source_id=f"synthetic-{harness}",
        harness=harness,
        node_label="node-example",
        source_ref=source_ref or f"{harness}/candidate",
        path=path or Path(f"/srv/example/{harness}-session.jsonl"),
        payload=payload,
        decoder_options=options or {},
    )


class DecoderRegistryTest(unittest.TestCase):
    def test_every_supported_harness_is_registered(self) -> None:
        for harness in (
            "claude-code",
            "codex",
            "opencode",
            "dsh",
            "cursor",
            "openclaw",
        ):
            self.assertEqual(decoder_for(harness).harness, harness)


class OpenCodeDecoderTest(unittest.TestCase):
    def create_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE session (
              id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT,
              time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE message (
              id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
            );
            CREATE TABLE part (
              id TEXT PRIMARY KEY, message_id TEXT, time_created INTEGER, data TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
            ("session-complete-example", "Example", "/srv/example/project-one", 10, 20),
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
            (
                "session-child-example",
                "session-complete-example",
                "Child",
                "/srv/example/project-one",
                11,
                21,
            ),
        )
        messages = (
            ("message-auto", "session-complete-example", 5, {"role": "user"}),
            ("message-b", "session-complete-example", 20, {"role": "assistant"}),
            ("message-a", "session-complete-example", 10, {"role": "user"}),
            ("message-c", "session-child-example", 11, {"role": "user"}),
        )
        for message_id, session_id, created, data in messages:
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                (message_id, session_id, created, json.dumps(data)),
            )
        parts = (
            (
                "part-auto",
                "message-auto",
                5,
                {"type": "text", "text": "AUTO ignored synthetic"},
            ),
            ("part-b", "message-b", 20, {"type": "text", "text": "synthetic answer"}),
            ("part-a", "message-a", 10, {"type": "text", "text": "synthetic request"}),
            ("part-c", "message-c", 11, {"type": "text", "text": "child request"}),
            ("part-tool", "message-b", 21, {"type": "tool", "text": "ignored"}),
        )
        for part_id, message_id, created, data in parts:
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                (part_id, message_id, created, json.dumps(data)),
            )
        connection.commit()
        connection.close()

    def test_stable_database_snapshot_keeps_only_top_level_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "snapshot.db"
            self.create_database(database)
            before = database.read_bytes()
            result = OpenCodeDecoder().decode(
                snapshot(
                    "opencode",
                    database.read_bytes(),
                    path=database,
                    options={"synthetic_prefixes": ["AUTO "]},
                )
            )
            after = database.read_bytes()
        self.assertEqual(result.completeness, "complete")
        self.assertEqual(before, after)
        self.assertEqual(
            [item.session_id for item in result.sessions], ["session-complete-example"]
        )
        session = result.sessions[0]
        self.assertEqual(session.project_hint, "project-one")
        self.assertEqual(
            [(event.role_hint, event.text) for event in session.events],
            [("user-like", "synthetic request"), ("assistant", "synthetic answer")],
        )
        self.assertEqual(result.observations.unknown_record_counts, {})

    def test_bad_database_is_loud_without_private_diagnostic_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "broken.db"
            database.write_bytes(b"synthetic invalid sqlite bytes")
            result = OpenCodeDecoder().decode(
                snapshot("opencode", database.read_bytes(), path=database)
            )
        self.assertEqual(result.completeness, "invalid")
        self.assertEqual(result.diagnostics[0].code, "OPENCODE_DATABASE_UNREADABLE")
        self.assertEqual(result.diagnostics[0].source_id, "synthetic-opencode")
        self.assertNotIn(str(database), repr(result.diagnostics))

    def test_configured_synthetic_cwd_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "snapshot.db"
            self.create_database(database)
            result = OpenCodeDecoder().decode(
                snapshot(
                    "opencode",
                    database.read_bytes(),
                    path=database,
                    options={"excluded_cwd_prefixes": ["/srv/example"]},
                )
            )
        self.assertEqual(result.completeness, "complete")
        self.assertFalse(result.sessions)
        self.assertEqual(result.rejected_sessions[0].reason_code, "EXCLUDED_CWD")


class ClaudeDecoderTest(unittest.TestCase):
    def test_queued_prompts_slash_commands_and_noise_filtering(self) -> None:
        command = (
            "<command-name>review</command-name>"
            "<command-message>review</command-message>"
            "<command-args>synthetic-change</command-args>"
        )
        payload = jsonl(
            {
                "type": "user",
                "sessionId": "claude-session-example",
                "cwd": "/srv/example/project-one",
                "timestamp": "2026-01-05T10:00:00Z",
                "message": {"content": "synthetic direct request"},
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "timestamp": "2026-01-05T10:00:01Z",
                "content": "synthetic queued request",
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "synthetic direct request",
            },
            {"type": "user", "isMeta": True, "message": {"content": command}},
            {"type": "queue-operation", "operation": "enqueue", "content": command},
            {
                "type": "user",
                "isSidechain": True,
                "message": {"content": "ignored sidechain"},
            },
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "ignored metadata"},
            },
            {"type": "user", "message": {"content": "AUTO ignored synthetic"}},
            {
                "type": "assistant",
                "timestamp": "2026-01-05T10:00:02Z",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "ignored reasoning"},
                        {"type": "tool_use", "name": "ignored"},
                        {"type": "text", "text": "synthetic claude answer"},
                    ]
                },
            },
            {"type": "progress", "data": "ignored progress"},
        )
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                payload,
                options={
                    "synthetic_prompt_prefixes": ["AUTO "],
                    "project_hint": "project-one",
                },
            )
        )
        self.assertEqual(result.completeness, "complete")
        session = result.sessions[0]
        self.assertEqual(session.session_id, "claude-session-example")
        self.assertEqual(
            [(event.role_hint, event.text) for event in session.events],
            [
                ("user-like", "synthetic direct request"),
                ("user-like", "synthetic queued request"),
                ("user-like", "[slash] /review synthetic-change"),
                ("assistant", "synthetic claude answer"),
            ],
        )
        self.assertNotIn("ignored reasoning", repr(session.events))
        self.assertEqual(result.observations.accepted_direct_user_events, 3)

    def test_sidechain_echo_does_not_hide_queued_prompt(self) -> None:
        text = "synthetic queued-only request"
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "isSidechain": True,
                        "message": {"content": text},
                    },
                    {
                        "type": "queue-operation",
                        "operation": "enqueue",
                        "content": text,
                    },
                ),
            )
        )
        self.assertEqual(
            [event.text for event in result.sessions[0].events],
            [text],
        )

    def test_conversational_subagent_retention_is_configurable(self) -> None:
        records = [
            {"type": "user", "message": {"content": f"synthetic request {index}"}}
            for index in range(3)
        ]
        disabled = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(*records),
                options={
                    "session_id": "claude-subagent-example",
                    "conversation_kind": "conversational-subagent",
                    "retain_conversational_subagents": False,
                },
            )
        )
        self.assertFalse(disabled.sessions)
        self.assertEqual(
            disabled.rejected_sessions[0].reason_code,
            "CONVERSATIONAL_SUBAGENT_DISABLED",
        )

        retained = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(*records),
                options={
                    "session_id": "claude-subagent-example",
                    "conversation_kind": "conversational-subagent",
                    "retain_conversational_subagents": True,
                    "conversational_subagent_min_user_events": 3,
                },
            )
        )
        self.assertEqual(
            retained.sessions[0].conversation_kind, "conversational-subagent"
        )
        self.assertEqual(len(retained.sessions[0].events), 3)

        detected = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(*records),
                source_ref="claude-code/project/session/subagents/agent-example.jsonl",
            )
        )
        self.assertEqual(
            detected.sessions[0].conversation_kind, "conversational-subagent"
        )


def codex_meta(**extra: object) -> dict:
    return {
        "type": "session_meta",
        "timestamp": "2026-01-05T10:00:00Z",
        "payload": {
            "id": "codex-session-example",
            "cwd": "/srv/example/project-one",
            "source": "cli",
            **extra,
        },
    }


def codex_legacy(
    user: str = "synthetic codex request", answer: str = "synthetic codex answer"
) -> list[dict]:
    return [
        {
            "type": "event_msg",
            "timestamp": "2026-01-05T10:00:01Z",
            "payload": {"type": "user_message", "message": user},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-05T10:00:02Z",
            "payload": {"type": "agent_message", "message": answer},
        },
    ]


def codex_items(
    user: str = "synthetic codex request", answer: str = "synthetic codex answer"
) -> list[dict]:
    return [
        {
            "type": "event_msg",
            "timestamp": "2026-01-05T10:00:01Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "id": "item-user",
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": user}],
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-05T10:00:02Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "id": "item-answer",
                    "type": "AgentMessage",
                    "content": [{"type": "Text", "text": answer}],
                },
            },
        },
    ]


def codex_response(
    user: str = "synthetic codex request", answer: str = "synthetic codex answer"
) -> list[dict]:
    return [
        {
            "type": "response_item",
            "timestamp": "2026-01-05T10:00:01Z",
            "payload": {
                "id": "response-user",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": user}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-05T10:00:02Z",
            "payload": {
                "id": "response-answer",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer}],
            },
        },
    ]


class CodexDecoderTest(unittest.TestCase):
    def decode(self, *records: dict, options: dict | None = None):
        return CodexDecoder().decode(
            snapshot(
                "codex",
                jsonl(*records),
                options=options or {"project_hint": "project-one"},
            )
        )

    def test_all_three_known_message_generations(self) -> None:
        for generation in (codex_legacy(), codex_items(), codex_response()):
            with self.subTest(raw_type=generation[0]["type"]):
                result = self.decode(codex_meta(), *generation)
                self.assertEqual(result.completeness, "complete")
                self.assertEqual(
                    [
                        (event.role_hint, event.text)
                        for event in result.sessions[0].events
                    ],
                    [
                        ("user-like", "synthetic codex request"),
                        ("assistant", "synthetic codex answer"),
                    ],
                )

    def test_coexisting_streams_deduplicate_and_partial_streams_complement(
        self,
    ) -> None:
        coexisting = self.decode(
            codex_meta(), *codex_legacy(), *codex_items(), *codex_response()
        )
        self.assertEqual(len(coexisting.sessions[0].events), 2)
        self.assertEqual(
            coexisting.sessions[0].metadata["canonical_generation"], "item_completed"
        )

        legacy = codex_legacy()
        legacy.append(
            {
                "type": "event_msg",
                "timestamp": "2026-01-05T10:00:03Z",
                "payload": {"type": "user_message", "message": "synthetic follow-up"},
            }
        )
        response_tail = codex_response(
            "synthetic follow-up", "synthetic follow-up answer"
        )
        complemented = self.decode(
            codex_meta(), *codex_items(), *legacy, *response_tail
        )
        self.assertEqual(
            [event.text for event in complemented.sessions[0].events],
            [
                "synthetic codex request",
                "synthetic codex answer",
                "synthetic follow-up",
                "synthetic follow-up answer",
            ],
        )
        self.assertIn(
            "CODEX_STREAMS_COMPLEMENTED",
            {item.code for item in complemented.diagnostics},
        )

    def test_explicit_child_agent_is_dropped_but_user_fork_is_kept(self) -> None:
        child = self.decode(codex_meta(thread_source="subagent"), *codex_items())
        self.assertFalse(child.sessions)
        self.assertEqual(child.rejected_sessions[0].reason_code, "EXPLICIT_SUBAGENT")

        fork = self.decode(codex_meta(forked_from_id="parent-example"), *codex_items())
        self.assertEqual(len(fork.sessions), 1)

        first_meta_wins = self.decode(
            codex_meta(),
            {"type": "session_meta", "payload": {"thread_source": "subagent"}},
            *codex_items(),
        )
        self.assertEqual(len(first_meta_wins.sessions), 1)

    def test_fallback_identity_keeps_complete_source_stem(self) -> None:
        complete_id = "rollout-2026-01-05-session-prefix-that-must-remain-complete"
        result = CodexDecoder().decode(
            snapshot(
                "codex",
                jsonl(*codex_items()),
                source_ref=f"codex/{complete_id}.jsonl",
            )
        )
        self.assertEqual(result.sessions[0].session_id, complete_id)

    def test_synthetic_context_title_trailer_and_unknown_future_format(self) -> None:
        trailer = (
            "synthetic retained request\n\n"
            "Based on this message, call functions.happy__change_title with a synthetic title"
        )
        filtered = self.decode(
            codex_meta(),
            *codex_legacy(
                "<environment_context>synthetic injected context", "ignored answer"
            ),
            *codex_items(trailer, "synthetic retained answer"),
        )
        user_events = [
            event
            for event in filtered.sessions[0].events
            if event.role_hint == "user-like"
        ]
        self.assertEqual(
            [event.text for event in user_events], ["synthetic retained request"]
        )

        future = self.decode(
            codex_meta(),
            {
                "type": "event_msg",
                "payload": {
                    "type": "future_user_message",
                    "content": [
                        {"kind": "future_text", "value": "synthetic future request"}
                    ],
                },
            },
        )
        self.assertEqual(future.completeness, "incomplete")
        self.assertFalse(future.sessions)
        self.assertIn(
            "CODEX_UNKNOWN_MESSAGE_FORMAT", {item.code for item in future.diagnostics}
        )
        self.assertNotIn("synthetic future request", repr(future.diagnostics))


class CursorDecoderTest(unittest.TestCase):
    def test_query_context_filter_and_approximate_assistant_time(self) -> None:
        payload = jsonl(
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<timestamp>Monday, Jan 5, 2026, 1:20 PM "
                                "(UTC+02:00)</timestamp>\n"
                                "<attached_files>synthetic context</attached_files>\n"
                                "<user_query>synthetic cursor request</user_query>"
                            ),
                        }
                    ]
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "ignored reasoning"},
                        {"type": "text", "text": "synthetic cursor answer"},
                    ]
                },
            },
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<attached_files>ignored sibling context</attached_files>",
                        }
                    ]
                },
            },
            {
                "role": "user",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "<user_query>AUTO synthetic turn</user_query>",
                        }
                    ]
                },
            },
        )
        result = CursorDecoder().decode(
            snapshot(
                "cursor",
                payload,
                options={
                    "session_id": "cursor-session-example",
                    "project_hint": "project-one",
                    "synthetic_prefixes": ["AUTO "],
                },
            )
        )
        self.assertEqual(result.completeness, "complete")
        session = result.sessions[0]
        self.assertEqual(session.session_id, "cursor-session-example")
        self.assertEqual(session.project_hint, "project-one")
        self.assertEqual(
            [event.text for event in session.events],
            ["synthetic cursor request", "synthetic cursor answer"],
        )
        self.assertNotIn("synthetic context", session.events[0].text)
        self.assertEqual(session.events[0].timestamp_quality, "exact")
        self.assertEqual(session.events[1].timestamp_quality, "approximate")
        self.assertEqual(session.events[0].timestamp, session.events[1].timestamp)
        self.assertEqual(result.observations.unknown_record_counts, {})


class OpenClawDecoderTest(unittest.TestCase):
    def messages(self, *, channel: str = "chat") -> bytes:
        return jsonl(
            {
                "type": "session",
                "id": "openclaw-session-example",
                "timestamp": "2026-01-05T10:00:00Z",
                "cwd": "/srv/example/project-one",
                "channel": channel,
                "label": "synthetic label",
            },
            {
                "type": "message",
                "id": "m1",
                "timestamp": "2026-01-05T10:00:01Z",
                "message": {
                    "role": "user",
                    "content": "NOTICE: synthetic operational update",
                },
            },
            {
                "type": "message",
                "id": "m2",
                "timestamp": "2026-01-05T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": "System: synthetic forwarded conversation",
                },
            },
            {
                "type": "message",
                "id": "m3",
                "timestamp": "2026-01-05T10:00:03Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "ignored reasoning"},
                        {"type": "tool_use", "name": "ignored"},
                        {"type": "text", "text": "synthetic openclaw answer"},
                    ],
                },
            },
            {
                "type": "message",
                "id": "m4",
                "message": {"role": "toolResult", "content": "ignored tool result"},
            },
        )

    def test_operational_channel_and_metadata_policies(self) -> None:
        result = OpenClawDecoder().decode(
            snapshot(
                "openclaw",
                self.messages(),
                options={
                    "exclude_operational_notifications": True,
                    "operational_notification_prefixes": ["NOTICE:"],
                    "retain_channel_forwarded": True,
                    "include_channel_metadata": True,
                    "include_session_metadata": True,
                    "session_metadata_fields": ["label"],
                    "project_hint": "project-one",
                },
            )
        )
        session = result.sessions[0]
        self.assertEqual(
            [event.text for event in session.events],
            ["System: synthetic forwarded conversation", "synthetic openclaw answer"],
        )
        self.assertEqual(session.events[0].metadata, {"channel_forwarded": True})
        self.assertEqual(
            session.metadata, {"channel": "chat", "label": "synthetic label"}
        )

    def test_cron_and_forwarded_conversation_filters(self) -> None:
        cron = OpenClawDecoder().decode(
            snapshot(
                "openclaw",
                self.messages(channel="cron"),
                options={"exclude_cron_sessions": True},
            )
        )
        self.assertEqual(cron.rejected_sessions[0].reason_code, "CRON_SESSION")

        forwarded = OpenClawDecoder().decode(
            snapshot(
                "openclaw",
                self.messages(),
                options={
                    "exclude_operational_notifications": True,
                    "operational_notification_prefixes": ["NOTICE:"],
                    "retain_channel_forwarded": False,
                },
            )
        )
        self.assertFalse(forwarded.sessions)
        self.assertEqual(forwarded.rejected_sessions[0].reason_code, "NO_DIRECT_USER")


if __name__ == "__main__":
    unittest.main()
