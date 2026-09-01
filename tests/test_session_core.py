from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.audit import (
    OutputInventory,
    entry_from_content,
    scan_inventory,
)
from agent_skills.sessions.cleanup import plan_cleanup
from agent_skills.sessions.identity import allocate_filenames, relative_output_path
from agent_skills.sessions.indexes import add_indexes
from agent_skills.sessions.manifest import RedactionSpec, load_manifest
from agent_skills.sessions.model import (
    NORMALIZED_SCHEMA_VERSION,
    Event,
    ExtractionSnapshot,
    PlannedFile,
    PublicationPlan,
    Session,
    SourceOutcome,
)
from agent_skills.sessions.pipeline import build_publication_plan
from agent_skills.sessions.policies import deduplicate_sessions
from agent_skills.sessions.redact import RedactionError, Redactor
from agent_skills.sessions.render import render_history, render_prompts, truncate_prompt


def session(
    *, harness="codex", node="node-a", session_id="session-000000000001", project="demo"
):
    timestamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    return Session(
        NORMALIZED_SCHEMA_VERSION,
        harness,
        session_id,
        "source-a/session.jsonl",
        node,
        None,
        project,
        timestamp,
        timestamp,
        (Event(0, timestamp, "exact", "user", "hello", "fixture.user"),),
        {},
    )


class RedactionTest(unittest.TestCase):
    def test_positive_controls_and_idempotence(self) -> None:
        redactor = Redactor.from_spec(RedactionSpec(True, "default", ()))
        dirty = "token gsk-SYNTHETIC000000CANARY"
        once, counts = redactor.apply(dirty)
        twice, _ = redactor.apply(once)
        self.assertTrue(counts)
        self.assertNotIn("SYNTHETIC000000CANARY", once)
        self.assertEqual(once, twice)

    def test_custom_pattern_requires_a_working_canary(self) -> None:
        spec = RedactionSpec(
            True,
            "none",
            (
                {
                    "name": "custom",
                    "regex": r"PRIVATE-[0-9]+",
                    "canary": "does-not-match",
                },
            ),
        )
        with self.assertRaises(RedactionError):
            Redactor.from_spec(spec)

    def test_custom_pattern_is_visible_and_idempotent(self) -> None:
        spec = RedactionSpec(
            True,
            "none",
            (
                {
                    "name": "consumer-token",
                    "regex": r"PRIVATE-[0-9]{6}",
                    "canary": "PRIVATE-123456",
                },
            ),
        )
        redactor = Redactor.from_spec(spec)
        once, counts = redactor.apply("value PRIVATE-654321")
        twice, _ = redactor.apply(once)
        self.assertEqual(once, "value [REDACTED:consumer-token]")
        self.assertEqual(counts, {"consumer-token": 1})
        self.assertEqual(once, twice)


class NamingAndLayoutTest(unittest.TestCase):
    def test_cross_tool_and_cross_node_collisions_are_identity_aware(self) -> None:
        sessions = (
            session(harness="codex", node="node-a"),
            session(harness="claude-code", node="node-a"),
            session(harness="codex", node="node-b"),
        )
        names = allocate_filenames(sessions)
        self.assertEqual(len(set(names.values())), 3)
        self.assertIn("--claude-code", names[sessions[1].identity])
        self.assertIn("--codex-node-a", names[sessions[0].identity])
        self.assertIn("--codex-node-b", names[sessions[2].identity])

    def test_flat_and_monthly_paths(self) -> None:
        value = session()
        self.assertEqual(
            relative_output_path("History", "flat", value, "x.md"), "History/x.md"
        )
        self.assertEqual(
            relative_output_path("History", "monthly", value, "x.md"),
            "History/2026-01/x.md",
        )

    def test_configured_filename_strategies_match_stable_legacy_shapes(self) -> None:
        claude = session(
            harness="claude-code", session_id="12345678-abcd-0000-0000-000000000000"
        )
        codex = session(
            harness="codex", session_id="12345678-abcd-0000-0000-fedcba987654"
        )
        opencode = session(harness="opencode", session_id="session-abcdefgh")
        dsh = session(harness="dsh", session_id="complete-session-id")
        values = (claude, codex, opencode, dsh)
        strategies = {
            claude.identity: "session-prefix-8",
            codex.identity: "session-last-component-prefix-8",
            opencode.identity: "session-suffix-8",
            dsh.identity: "node-session-sha256-12",
        }
        names = allocate_filenames(
            values,
            strategies=strategies,
            destinations={value.identity: value.harness for value in values},
        )
        self.assertEqual(names[claude.identity], "2026-01-02_12345678.md")
        self.assertEqual(names[codex.identity], "2026-01-02_fedcba98.md")
        self.assertEqual(names[opencode.identity], "2026-01-02_abcdefgh.md")
        self.assertRegex(names[dsh.identity], r"^2026-01-02_[0-9a-f]{12}\.md$")

    def test_prompt_truncation_closes_bounded_code_block(self) -> None:
        value = "before\n```python\n" + ("x" * 400) + "\n```\nafter"
        result = truncate_prompt(value, maximum=120, code_block_maximum=40)
        self.assertLessEqual(len(result), 120)
        self.assertEqual(result.count("```"), 2)
        self.assertIn("[truncated]", result)

    def test_total_limit_does_not_cut_the_closing_fence(self) -> None:
        value = "```text\n" + ("x" * 200)
        result = truncate_prompt(value, maximum=64, code_block_maximum=56)
        self.assertLessEqual(len(result), 64)
        self.assertEqual(result.count("```") % 2, 0)
        self.assertTrue(result.endswith("[truncated]"))

    def test_rendered_markdown_removes_event_line_end_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            manifest = load_manifest(
                write_manifest(
                    root / "manifest.json",
                    manifest_data(source, output),
                ),
                environ={"HOME": str(root)},
            )
            timestamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
            value = replace(
                session(),
                events=(
                    Event(
                        0,
                        timestamp,
                        "exact",
                        "user",
                        "first line  \nsecond line\t",
                        "fixture.user",
                    ),
                    Event(
                        1,
                        timestamp,
                        "exact",
                        "assistant",
                        "answer  \nnext\t",
                        "fixture.assistant",
                    ),
                ),
            )

            for rendered in (
                render_history(value, manifest.output),
                render_prompts(value, manifest.output),
            ):
                self.assertTrue(
                    all(line == line.rstrip() for line in rendered.splitlines())
                )

    def test_truncated_title_does_not_end_with_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            manifest = load_manifest(
                write_manifest(
                    root / "manifest.json",
                    manifest_data(source, output),
                ),
                environ={"HOME": str(root)},
            )
            timestamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
            value = replace(
                session(),
                events=(
                    Event(
                        0,
                        timestamp,
                        "exact",
                        "user",
                        ("x" * 69) + " more words",
                        "fixture.user",
                    ),
                ),
            )

            for rendered in (
                render_history(value, manifest.output),
                render_prompts(value, manifest.output),
            ):
                heading = rendered.splitlines()[0]
                self.assertEqual(heading, heading.rstrip())


class SessionDeduplicationTest(unittest.TestCase):
    @staticmethod
    def _with_events(
        value: Session,
        source_ref: str,
        texts: tuple[str, ...],
        *,
        minute: int,
    ) -> Session:
        events = tuple(
            Event(
                index,
                datetime(2026, 1, 2, 3, minute, index, tzinfo=UTC),
                "approximate",
                "user" if index % 2 == 0 else "assistant",
                text,
                "fixture.message",
            )
            for index, text in enumerate(texts)
        )
        return replace(
            value,
            source_ref=source_ref,
            started_at=events[0].timestamp,
            ended_at=events[-1].timestamp,
            events=events,
        )

    def test_strict_prefix_selects_complete_generation_without_diagnostic(self) -> None:
        base = session()
        partial = self._with_events(
            base, "mirror/session.jsonl", ("first",), minute=4
        )
        complete = self._with_events(
            base,
            "owner/session.jsonl",
            ("first", "second", "third"),
            minute=5,
        )

        selected, diagnostics = deduplicate_sessions([partial, complete])

        self.assertEqual(selected, (complete,))
        self.assertEqual(diagnostics, ())

    def test_non_prefix_content_conflict_remains_visible(self) -> None:
        base = session()
        first = self._with_events(
            base, "source-a/session.jsonl", ("first", "answer-a"), minute=4
        )
        second = self._with_events(
            base, "source-b/session.jsonl", ("first", "answer-b"), minute=5
        )

        selected, diagnostics = deduplicate_sessions([first, second])

        self.assertEqual(selected, (first,))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].code, "DUPLICATE_SESSION_DIVERGENCE")
        self.assertEqual(diagnostics[0].count, 2)


class CleanupTest(unittest.TestCase):
    def _manifest(self, root: Path, *, scope: str, ownership: str = "owner"):
        source = root / "source"
        source.mkdir()
        output = root / "output"
        output.mkdir()
        data = manifest_data(source, output, cleanup=scope, ownership=ownership)
        return load_manifest(
            write_manifest(root / "manifest.json", data), environ={"HOME": str(root)}
        )

    def test_owner_preserves_other_node_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root, scope="owner")
            local = session(node="node-a")
            other = session(node="node-b", session_id="session-000000000002")
            output = replace(manifest.output, encryption_attributes={})
            inventory = OutputInventory(
                (
                    entry_from_content(
                        "History/local.md", render_history(local, output).encode()
                    ),
                    entry_from_content(
                        "History/other.md", render_history(other, output).encode()
                    ),
                )
            )
            outcomes = (SourceOutcome("source-a", "node-a", "success", 1, 0),)
            removals = plan_cleanup(manifest, inventory, (), outcomes)
            self.assertEqual([item.identity for item in removals], [local.identity])

    def test_failed_aggregator_source_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root, scope="aggregator", ownership="aggregator")
            old = session(node="node-a")
            inventory = OutputInventory(
                (
                    entry_from_content(
                        "History/old.md", render_history(old, manifest.output).encode()
                    ),
                )
            )
            outcomes = (SourceOutcome("source-a", "node-a", "invalid", 0, 0),)
            self.assertEqual(plan_cleanup(manifest, inventory, (), outcomes), ())

    def test_one_successful_tool_cannot_authorize_node_wide_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root, scope="owner")
            second = replace(manifest.sources[0], source_id="source-b")
            manifest = replace(manifest, sources=(*manifest.sources, second))
            old = session(node="node-a")
            inventory = OutputInventory(
                (
                    entry_from_content(
                        "History/old.md", render_history(old, manifest.output).encode()
                    ),
                )
            )
            outcomes = (
                SourceOutcome("source-a", "node-a", "success", 1, 0),
                SourceOutcome("source-b", "node-a", "invalid", 0, 0),
            )
            self.assertEqual(plan_cleanup(manifest, inventory, (), outcomes), ())


class PreservedOutputTest(unittest.TestCase):
    def _manifest(self, root: Path):
        source = root / "source"
        source.mkdir()
        output = root / "output"
        output.mkdir()
        data = manifest_data(
            source,
            output,
            cleanup="none",
            indexes="every-node",
        )
        return load_manifest(
            write_manifest(root / "manifest.json", data),
            environ={"HOME": str(root)},
        )

    def test_indexes_include_preserved_and_new_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            preserved = session(node="node-b", session_id="preserved-session")
            current = session(node="node-a", session_id="current-session")
            preserved_content = render_history(preserved, manifest.output).encode()
            current_content = render_history(current, manifest.output).encode()
            inventory = OutputInventory(
                (entry_from_content("History/2026-01/preserved.md", preserved_content),)
            )
            plan = PublicationPlan(
                (
                    PlannedFile(
                        "History/2026-01/current.md",
                        current_content,
                        current.identity,
                        "history",
                    ),
                ),
                (),
            )
            indexed = add_indexes(manifest, inventory, plan)
            history_index = next(
                item
                for item in indexed.writes
                if item.relative_path == "History/README.md"
            )
            rendered = history_index.content.decode()
            self.assertIn("preserved-session", rendered)
            self.assertIn("current-session", rendered)

    def test_harness_history_routes_are_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            manifest = replace(
                manifest,
                output=replace(
                    manifest.output,
                    history_directory_by_harness={
                        "claude-code": "Claude-History",
                        "codex": "Codex-History",
                    },
                    filename_strategy_by_harness={
                        "claude-code": "session-prefix-8",
                        "codex": "session-last-component-prefix-8",
                    },
                ),
            )
            values = (
                session(
                    harness="claude-code",
                    session_id="12345678-0000-0000-0000-000000000000",
                ),
                session(
                    harness="codex",
                    session_id="12345678-0000-0000-0000-fedcba987654",
                ),
            )
            snapshot = ExtractionSnapshot(
                values,
                (SourceOutcome("source-a", "node-a", "success", 2, 2),),
                {},
            )
            plan = build_publication_plan(
                manifest,
                snapshot,
                OutputInventory(()),
                Redactor.from_spec(manifest.redaction),
            )
            paths = {item.relative_path for item in plan.writes}
            self.assertIn("Claude-History/2026-01/2026-01-02_12345678.md", paths)
            self.assertIn("Codex-History/2026-01/2026-01-02_fedcba98.md", paths)

    def test_prompt_project_policy_does_not_remove_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            manifest = replace(
                manifest,
                project_policy=replace(
                    manifest.project_policy,
                    prompt_by_harness={
                        "codex": {
                            "mode": "allowlist",
                            "unknown": "drop",
                            "allowlist": ("another-project",),
                            "denylist": (),
                        }
                    },
                ),
            )
            current = session(harness="codex", project="demo")
            snapshot = ExtractionSnapshot(
                (current,),
                (SourceOutcome("source-a", "node-a", "success", 1, 1),),
                {},
            )
            plan = build_publication_plan(
                manifest,
                snapshot,
                OutputInventory(()),
                Redactor.from_spec(manifest.redaction),
            )
            self.assertEqual([item.kind for item in plan.writes], ["history"])

    def test_legacy_markdown_is_adopted_in_place_without_bulk_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            history = output / "Codex-History" / "2026-01"
            prompts = output / "Prompts" / "2026-01"
            history.mkdir(parents=True)
            prompts.mkdir(parents=True)
            filename = "2026-01-02_00000000.md"
            (history / filename).write_text(
                """# hello

- Session ID: `session-000000000001`
- Host: `node-a`
- Project: `demo`
- Tool: `codex`

---

### 2026-01-02 03:04:00Z — user

> hello
""",
                encoding="utf-8",
            )
            (prompts / filename).write_text(
                """# hello

- Tool: codex
- Project: demo
- Host: node-a
- Prompts: 1

---

### 2026-01-02 03:04:00Z

hello

---
""",
                encoding="utf-8",
            )
            data = manifest_data(source, output, cleanup="none")
            data["output"]["history_directory"] = "Codex-History"
            data["output"]["history_directory_by_harness"] = {
                "codex": "Codex-History"
            }
            data["output"]["prompt_directory"] = "Prompts"
            data["output"]["compatibility"]["rule_version"] = (
                "legacy-agent-markdown/v1"
            )
            data["publisher"]["owned_subtrees"] = ["Codex-History", "Prompts"]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            inventory = scan_inventory(manifest)
            current = session()
            snapshot = ExtractionSnapshot(
                (current,),
                (SourceOutcome("source-a", "node-a", "success", 1, 1),),
                {},
            )
            plan = build_publication_plan(
                manifest,
                snapshot,
                inventory,
                Redactor.from_spec(manifest.redaction),
            )
            self.assertEqual(plan.writes, ())
            self.assertEqual(plan.removals, ())
            identities = {
                entry.identity for entry in inventory.entries if entry.identity
            }
            self.assertEqual(identities, {current.identity})


if __name__ == "__main__":
    unittest.main()
