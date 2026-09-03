from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_skills.sessions.harnesses import decoder_for
from agent_skills.sessions.harnesses.base import jsonl_lines
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
                ("assistant", "synthetic claude answer"),
                ("user-like", "[slash] /review synthetic-change"),
            ],
        )
        self.assertNotIn("ignored reasoning", repr(session.events))
        self.assertEqual(result.observations.accepted_direct_user_events, 3)

    def test_sidechain_echo_suppresses_duplicate_queued_prompt(self) -> None:
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
            [event.text for session in result.sessions for event in session.events],
            [],
        )
        self.assertEqual(result.observations.recognizable_user_markers, 0)

    def test_real_user_text_blocks_are_supported(self) -> None:
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "synthetic block direct request",
                                }
                            ]
                        },
                    }
                ),
            )
        )

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(
            [event.text for event in result.sessions[0].events],
            ["synthetic block direct request"],
        )
        self.assertEqual(result.observations.recognizable_user_markers, 1)

    def test_future_direct_user_input_text_blocks_are_visible(self) -> None:
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "synthetic future direct request",
                                }
                            ]
                        },
                    }
                ),
            )
        )

        self.assertEqual(result.completeness, "incomplete")
        self.assertFalse(result.sessions)
        self.assertEqual(result.observations.recognizable_user_markers, 1)
        self.assertEqual(
            result.observations.unknown_record_counts,
            {"user.unknown-text-content": 1},
        )

    def test_current_metadata_records_are_explicitly_ignored(self) -> None:
        metadata_records = (
            {"type": "ai-title", "aiTitle": "synthetic title"},
            {"type": "agent-name", "agentName": "synthetic agent"},
            {"type": "bridge-session", "bridgeSessionId": "synthetic bridge"},
            {"type": "agent-setting", "setting": "synthetic setting"},
            {"type": "atis-latch", "atis": "synthetic state"},
            {"type": "cost-state", "cost": "synthetic cost state"},
            {
                "type": "file-history-delta",
                "backup": "synthetic backup marker",
            },
            {"type": "last-prompt", "lastPrompt": "synthetic prompt marker"},
            {"type": "mode", "mode": "synthetic mode"},
            {"type": "permission-mode", "permissionMode": "synthetic mode"},
            {"type": "pr-link", "prNumber": 1},
            {"type": "relocated", "relocatedCwd": "/srv/example/relocated"},
            {"type": "started", "key": "synthetic-agent-key"},
            {
                "type": "result",
                "key": "synthetic-agent-key",
                "result": {"summary": "synthetic agent result"},
            },
            {"type": "worktree-state", "worktreeSession": True},
        )
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "sessionId": "claude-session-example",
                        "message": {"content": "synthetic direct request"},
                    },
                    *metadata_records,
                ),
            )
        )

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(result.observations.unknown_record_counts, {})
        self.assertEqual(
            {
                key
                for key, count in result.observations.recognized_record_counts.items()
                if key.startswith("ignored.") and count
            },
            {f"ignored.{record['type']}" for record in metadata_records},
        )
        self.assertEqual(
            [event.text for event in result.sessions[0].events],
            ["synthetic direct request"],
        )

    def test_main_session_filters_sidechain_and_meta_context(self) -> None:
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "isSidechain": True,
                        "message": {"content": "synthetic sidechain echo"},
                    },
                    {
                        "type": "user",
                        "isMeta": True,
                        "message": {"content": "synthetic metadata echo"},
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "content": "synthetic result"}
                            ]
                        },
                    },
                ),
                options={
                    "session_id": "claude-subagent-example",
                    "conversation_kind": "main",
                },
            )
        )

        self.assertEqual(result.completeness, "complete")
        self.assertFalse(result.sessions)
        self.assertEqual(result.observations.recognizable_user_markers, 0)
        self.assertEqual(result.observations.accepted_direct_user_events, 0)
        self.assertEqual(result.rejected_sessions[0].reason_code, "NO_DIRECT_USER_EVENT")

    def test_conversational_subagent_keeps_its_sidechain_conversation(self) -> None:
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "isSidechain": True,
                        "message": {"content": "synthetic child request"},
                    },
                    {
                        "type": "assistant",
                        "isSidechain": True,
                        "message": {
                            "content": [
                                {"type": "text", "text": "synthetic child answer"}
                            ]
                        },
                    },
                ),
                source_ref="root/project/parent/subagents/agent-child.jsonl",
                options={
                    "conversation_kind": "conversational-subagent",
                    "retain_conversational_subagents": True,
                    "conversational_subagent_min_user_events": 1,
                },
            )
        )

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(result.sessions[0].session_id, "agent-child")
        self.assertEqual(
            [(event.role_hint, event.text) for event in result.sessions[0].events],
            [
                ("user-like", "synthetic child request"),
                ("assistant", "synthetic child answer"),
            ],
        )

    def test_unknown_future_record_remains_visible(self) -> None:
        result = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                jsonl(
                    {
                        "type": "user",
                        "sessionId": "claude-session-example",
                        "message": {"content": "synthetic direct request"},
                    },
                    {
                        "type": "future-session-metadata",
                        "futureField": "synthetic opaque value",
                    },
                ),
            )
        )

        self.assertEqual(result.completeness, "incomplete")
        self.assertEqual(
            result.observations.unknown_record_counts,
            {"future-session-metadata": 1},
        )
        self.assertIn(
            "CLAUDE_UNKNOWN_RECORD", {item.code for item in result.diagnostics}
        )

    def test_only_exact_malformed_line_hashes_can_be_grandfathered(self) -> None:
        malformed_line = b'{"type":"synthetic-broken"'
        payload = (
            jsonl(
                {
                    "type": "user",
                    "sessionId": "claude-session-example",
                    "message": {"content": "synthetic direct request"},
                }
            )
            + malformed_line
            + b"\n"
        )
        without_compatibility = ClaudeDecoder().decode(snapshot("claude-code", payload))
        self.assertEqual(without_compatibility.completeness, "incomplete")
        self.assertIn(
            "CLAUDE_MALFORMED_RECORD",
            {item.code for item in without_compatibility.diagnostics},
        )

        digest = hashlib.sha256(malformed_line).hexdigest()
        grandfathered = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                payload,
                options={"grandfathered_malformed_line_sha256": [digest]},
            )
        )
        self.assertEqual(grandfathered.completeness, "complete")
        self.assertEqual(grandfathered.observations.unknown_record_counts, {})
        self.assertEqual(
            grandfathered.observations.recognized_record_counts[
                "ignored.grandfathered-malformed-line"
            ],
            1,
        )
        self.assertNotIn(digest, repr(grandfathered))

        wrong_hash = ClaudeDecoder().decode(
            snapshot(
                "claude-code",
                payload,
                options={"grandfathered_malformed_line_sha256": ["0" * 64]},
            )
        )
        self.assertEqual(wrong_hash.completeness, "incomplete")

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
    def test_response_item_synthetic_runtime_messages_are_filtered(self) -> None:
        records = [codex_meta()]
        for index, tag in enumerate(
            ("hook_prompt", "subagent_notification", "turn_aborted"), start=1
        ):
            records.append(
                {
                    "type": "response_item",
                    "timestamp": f"2026-01-05T10:00:0{index}Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"<{tag}>synthetic runtime context</{tag}>",
                            }
                        ],
                    },
                }
            )
        records.extend(codex_items())

        result = self.decode(*records)

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(
            [(event.role_hint, event.text) for event in result.sessions[0].events],
            [
                ("user-like", "synthetic codex request"),
                ("assistant", "synthetic codex answer"),
            ],
        )
        self.assertNotIn("synthetic runtime context", repr(result.sessions))

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

    def test_competing_stream_versions_are_loud_and_not_concatenated(self) -> None:
        result = self.decode(
            codex_meta(),
            *codex_items(answer="synthetic canonical answer"),
            *codex_response(answer="synthetic competing answer"),
        )

        self.assertEqual(result.completeness, "incomplete")
        self.assertIn(
            "CODEX_STREAM_DIVERGENCE",
            {item.code for item in result.diagnostics},
        )
        self.assertEqual(
            [event.text for event in result.sessions[0].events],
            ["synthetic codex request", "synthetic canonical answer"],
        )
        self.assertNotIn("synthetic competing answer", repr(result.sessions))

    def test_response_item_image_prefix_matches_legacy_user_text(self) -> None:
        result = self.decode(
            codex_meta(),
            *codex_legacy(),
            *codex_response(
                '<image name=[Image #1] path="/synthetic/image.png">\n'
                "</image>\n"
                "synthetic codex request"
            ),
        )

        self.assertEqual(result.completeness, "complete")
        self.assertNotIn(
            "CODEX_STREAM_DIVERGENCE",
            {item.code for item in result.diagnostics},
        )
        self.assertEqual(
            [event.text for event in result.sessions[0].events],
            ["synthetic codex request", "synthetic codex answer"],
        )
        self.assertNotIn("/synthetic/image.png", repr(result.sessions))

    def test_shared_message_key_conflict_cannot_hide_as_a_subsequence(self) -> None:
        items = codex_items(answer="synthetic old answer")
        items[0]["payload"]["item"]["id"] = "shared-user"
        items[1]["payload"]["item"]["id"] = "shared-answer"
        items.append(
            {
                "type": "event_msg",
                "timestamp": "2026-01-05T10:00:03Z",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "id": "later-answer",
                        "type": "AgentMessage",
                        "content": [
                            {"type": "Text", "text": "synthetic new answer"}
                        ],
                    },
                },
            }
        )
        responses = codex_response(answer="synthetic new answer")
        responses[0]["payload"]["id"] = "shared-user"
        responses[1]["payload"]["id"] = "shared-answer"

        result = self.decode(codex_meta(), *items, *responses)

        self.assertEqual(result.completeness, "incomplete")
        self.assertIn(
            "CODEX_STREAM_DIVERGENCE",
            {item.code for item in result.diagnostics},
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

    def test_operational_error_and_world_state_are_explicitly_ignored(self) -> None:
        result = self.decode(
            codex_meta(),
            *codex_items(),
            {
                "type": "event_msg",
                "payload": {
                    "type": "error",
                    "message": "synthetic operational error",
                    "codex_error_info": "synthetic error category",
                },
            },
            {
                "type": "world_state",
                "ordinal": 1,
                "payload": {
                    "full": True,
                    "state": {"syntheticKey": "synthetic state value"},
                },
            },
            {
                "type": "inter_agent_communication_metadata",
                "ordinal": 2,
                "payload": {"trigger_turn": False},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "id": "synthetic-collaboration-call",
                        "status": "completed",
                        "agents_states": {},
                        "receiver_agents": [],
                        "receiver_thread_ids": [],
                        "sender_thread_id": "synthetic-sender",
                        "tool": "synthetic-tool",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "SubAgentActivity",
                        "id": "synthetic-agent-activity",
                        "agent_path": "synthetic-agent",
                        "agent_thread_id": "synthetic-thread",
                        "kind": "synthetic-kind",
                    },
                },
            },
        )

        self.assertEqual(result.completeness, "complete")
        self.assertEqual(result.observations.unknown_record_counts, {})
        self.assertEqual(
            result.observations.recognized_record_counts["event_msg.ignored.error"],
            1,
        )
        self.assertEqual(
            result.observations.recognized_record_counts["ignored.world_state"],
            1,
        )
        self.assertEqual(
            result.observations.recognized_record_counts[
                "ignored.inter_agent_communication_metadata"
            ],
            1,
        )
        self.assertEqual(
            result.observations.recognized_record_counts[
                "event_msg.item_completed.ignored.CollabAgentToolCall"
            ],
            1,
        )
        self.assertEqual(
            result.observations.recognized_record_counts[
                "event_msg.item_completed.ignored.SubAgentActivity"
            ],
            1,
        )
        self.assertNotIn("synthetic operational error", repr(result.sessions))
        self.assertNotIn("synthetic state value", repr(result.sessions))


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


class TornTailTest(unittest.TestCase):
    def test_unterminated_unparseable_tail_is_left_for_the_next_read(self) -> None:
        payload = jsonl({"a": 1}, {"b": 2}) + b'{"c": "still being writ'
        self.assertEqual(jsonl_lines(payload), [b'{"a": 1}', b'{"b": 2}'])

    def test_unterminated_complete_record_is_kept(self) -> None:
        payload = jsonl({"a": 1}) + b'{"b": 2}'
        self.assertEqual(jsonl_lines(payload), [b'{"a": 1}', b'{"b": 2}'])

    def test_terminated_malformed_line_still_counts_as_malformed(self) -> None:
        payload = jsonl({"a": 1}) + b'{"broken"\n'
        self.assertEqual(jsonl_lines(payload), [b'{"a": 1}', b'{"broken"'])

    def test_codex_snapshot_torn_inside_a_live_line_stays_complete(self) -> None:
        records = [codex_meta(), *codex_items()]
        complete = CodexDecoder().decode(snapshot("codex", jsonl(*records)))
        torn_payload = jsonl(*records) + jsonl(codex_items()[0])[:40]
        torn = CodexDecoder().decode(snapshot("codex", torn_payload))

        self.assertEqual(complete.completeness, "complete")
        self.assertEqual(torn.completeness, "complete")
        self.assertNotIn(
            "CODEX_MALFORMED_RECORD", {item.code for item in torn.diagnostics}
        )
        self.assertEqual(torn.sessions, complete.sessions)

    def test_claude_snapshot_torn_inside_a_live_line_stays_complete(self) -> None:
        records = [
            {
                "type": "user",
                "sessionId": "claude-session-example",
                "cwd": "/srv/example/project-one",
                "timestamp": "2026-01-05T10:00:00Z",
                "message": {"content": "synthetic direct request"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-01-05T10:00:02Z",
                "message": {"content": [{"type": "text", "text": "synthetic answer"}]},
            },
        ]
        complete = ClaudeDecoder().decode(snapshot("claude-code", jsonl(*records)))
        torn_payload = jsonl(*records) + jsonl(records[0])[:35]
        torn = ClaudeDecoder().decode(snapshot("claude-code", torn_payload))

        self.assertEqual(complete.completeness, "complete")
        self.assertEqual(torn.completeness, "complete")
        self.assertNotIn(
            "CLAUDE_MALFORMED_RECORD", {item.code for item in torn.diagnostics}
        )
        self.assertEqual(torn.sessions, complete.sessions)
