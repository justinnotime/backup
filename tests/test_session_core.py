from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.audit import OutputInventory, entry_from_content
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
    def _manifest(self, root: Path, *, migration: str = "none"):
        source = root / "source"
        source.mkdir()
        output = root / "output"
        output.mkdir()
        data = manifest_data(
            source,
            output,
            cleanup="none",
            indexes="every-node",
            migration=migration,
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

    def test_flat_to_monthly_migration_requires_explicit_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root, migration="flat-to-monthly")
            current = session()
            inventory = OutputInventory(
                (
                    entry_from_content(
                        "History/legacy.md",
                        render_history(current, manifest.output).encode(),
                    ),
                    entry_from_content(
                        "Prompts/legacy.md",
                        render_prompts(current, manifest.output).encode(),
                    ),
                )
            )
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
            self.assertEqual(
                {item.relative_path for item in plan.removals},
                {"History/legacy.md", "Prompts/legacy.md"},
            )
            self.assertTrue(
                all("/2026-01/" in item.relative_path for item in plan.writes)
            )


if __name__ == "__main__":
    unittest.main()
