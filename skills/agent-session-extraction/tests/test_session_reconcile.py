from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_session_core import session

from agent_skills.sessions.audit import InventoryEntry, OutputInventory
from agent_skills.sessions.model import (
    CleanupAction,
    Diagnostic,
    ExtractionSnapshot,
    FormatObservations,
    PublicationPlan,
    SourceOutcome,
)
from agent_skills.sessions.reconcile import reconcile_snapshot, write_failure_marker


class ReconciliationTest(unittest.TestCase):
    def test_duplicate_source_divergence_blocks_reconciliation(self) -> None:
        current = session(session_id="session-diverged-id")
        inventory = OutputInventory(
            (
                InventoryEntry(
                    "History/current.md",
                    "digest",
                    current.identity,
                    "history",
                    {},
                    "",
                ),
            )
        )
        snapshot = ExtractionSnapshot(
            (current,),
            (SourceOutcome("source-a", "node-a", "success", 1, 1),),
            {"source-a": FormatObservations(accepted_direct_user_events=1)},
            (
                Diagnostic(
                    "DUPLICATE_SESSION_DIVERGENCE",
                    "source-a",
                    current.session_id,
                    2,
                ),
            ),
        )

        report = reconcile_snapshot(snapshot, inventory, PublicationPlan((), ()))

        self.assertFalse(report.ok)
        self.assertEqual(
            [item.code for item in report.diagnostics],
            ["DUPLICATE_SESSION_DIVERGENCE"],
        )

    def test_removing_one_duplicate_path_preserves_the_session_output(self) -> None:
        current = session(session_id="session-migrated-id")
        identity = current.identity
        inventory = OutputInventory(
            (
                InventoryEntry(
                    "History/legacy.md", "digest-a", identity, "history", {}, "",
                ),
                InventoryEntry(
                    "History/2026-01/current.md",
                    "digest-b",
                    identity,
                    "history",
                    {},
                    "",
                ),
            )
        )
        snapshot = ExtractionSnapshot(
            (current,),
            (SourceOutcome("source-a", "node-a", "success", 1, 1),),
            {"source-a": FormatObservations(accepted_direct_user_events=1)},
        )

        report = reconcile_snapshot(
            snapshot,
            inventory,
            PublicationPlan((), (CleanupAction("History/legacy.md", identity),)),
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.checks["missing_outputs"], 0)

    def test_missing_output_markers_and_unknown_formats_are_loud(self) -> None:
        current = session(session_id="session-visible-id")
        snapshot = ExtractionSnapshot(
            (current,),
            (SourceOutcome("source-a", "node-a", "success", 1, 1),),
            {
                "source-a": FormatObservations(
                    recognized_record_counts={"known": 1},
                    unknown_record_counts={"future-message": 2},
                    recognizable_user_markers=1,
                    accepted_direct_user_events=0,
                )
            },
        )
        report = reconcile_snapshot(
            snapshot, OutputInventory(()), PublicationPlan((), ())
        )
        self.assertFalse(report.ok)
        self.assertEqual(
            {item.code for item in report.diagnostics},
            {
                "ACCEPTED_SESSION_WITHOUT_OUTPUT",
                "RECOGNIZED_MARKER_WITHOUT_INPUT",
                "UNKNOWN_MESSAGE_FORMAT",
            },
        )
        self.assertEqual(report.checks["missing_outputs"], 1)
        self.assertEqual(report.checks["unknown_message_records"], 2)

    def test_failure_marker_contains_only_status_ids_and_counts(self) -> None:
        current = session(session_id="session-safe-id")
        snapshot = ExtractionSnapshot(
            (current,),
            (SourceOutcome("source-safe", "node-a", "success", 1, 1),),
            {"source-safe": FormatObservations(unknown_record_counts={"future": 1})},
        )
        report = reconcile_snapshot(
            snapshot, OutputInventory(()), PublicationPlan((), ())
        )
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "failure.json"
            write_failure_marker(marker, report)
            rendered = marker.read_text(encoding="utf-8")
            parsed = json.loads(rendered)
        self.assertEqual(parsed["status"], "failed")
        self.assertEqual(parsed["schema_version"], "agent-session-reconciliation/v1")
        self.assertNotIn("text", rendered)
        self.assertNotIn("/absolute/", rendered)
        self.assertEqual(
            {item["source_id"] for item in parsed["diagnostics"]},
            {"source-a", "source-safe"},
        )


if __name__ == "__main__":
    unittest.main()
