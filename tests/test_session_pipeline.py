from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.api import reconcile, run
from agent_skills.sessions.manifest import load_manifest
from agent_skills.sessions.pipeline import PipelineError, evaluate_pipeline


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def write_claude(path: Path, *, text: str = "synthetic request") -> None:
    records = [
        {
            "type": "user",
            "sessionId": "claude-session-example",
            "cwd": "/srv/example/project-one",
            "timestamp": "2026-02-03T04:05:06Z",
            "message": {"content": text},
        },
        {
            "type": "assistant",
            "timestamp": "2026-02-03T04:05:07Z",
            "message": {"content": [{"type": "text", "text": "synthetic answer"}]},
        },
    ]
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
    )


class DryRunTest(unittest.TestCase):
    def test_full_dry_run_makes_no_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            write_claude(source / "session.jsonl")
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output, publisher="filesystem-atomic")
            manifest = write_manifest(root / "manifest.json", data)
            marker = root / "state" / "failure.json"
            before = tree_digest(root)
            report = run(
                manifest,
                dry_run=True,
                failure_marker=marker,
                environ={"HOME": str(root)},
            )
            after = tree_digest(root)
            self.assertEqual(before, after)
            self.assertFalse(marker.exists())
            self.assertEqual(report.session_count, 1)
            self.assertEqual(report.write_count, 2)

    def test_redaction_happens_before_planned_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            write_claude(source / "session.jsonl", text="use gsk-SYNTHETIC000000CANARY")
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            _snapshot, _inventory, plan, report, _redactor = evaluate_pipeline(manifest)
            self.assertTrue(report.ok)
            combined = b"\n".join(item.content for item in plan.writes)
            self.assertNotIn(b"SYNTHETIC000000CANARY", combined)
            self.assertIn(b"[REDACTED:known-key-prefix]", combined)

    def test_project_denylist_is_configuration_not_a_decoder_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            write_claude(source / "session.jsonl")
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                source, output, decoder={"project_hint": "blocked-project"}
            )
            data["project_policy"]["mode"] = "denylist"
            data["project_policy"]["denylist"] = ["blocked-project"]
            report = run(
                write_manifest(root / "manifest.json", data),
                dry_run=True,
                environ={"HOME": str(root)},
            )
            self.assertEqual(report.session_count, 0)

    def test_reconciliation_failure_does_not_write_marker_during_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            # A known user marker in a future Codex message envelope.
            (source / "future.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-02-03T04:05:06Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "future_user_message",
                            "user_message": "synthetic request",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output, harness="codex")
            marker = root / "failure.json"
            manifest_path = write_manifest(root / "manifest.json", data)
            reconciliation = reconcile(
                manifest_path,
                environ={"HOME": str(root)},
            )
            self.assertFalse(reconciliation.ok)
            self.assertIn(
                "UNKNOWN_MESSAGE_FORMAT",
                {item.code for item in reconciliation.diagnostics},
            )
            with self.assertRaises(PipelineError):
                run(
                    manifest_path,
                    dry_run=True,
                    failure_marker=marker,
                    environ={"HOME": str(root)},
                )
            self.assertFalse(marker.exists())
            self.assertFalse((output / "History").exists())

    def test_reconciliation_detects_one_marker_only_file_among_valid_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            write_claude(source / "real.jsonl")
            write_claude(
                source / "marker-only.jsonl",
                text="<system-reminder>synthetic context only",
            )
            output = root / "output"
            output.mkdir()
            manifest_path = write_manifest(
                root / "manifest.json", manifest_data(source, output)
            )
            report = reconcile(manifest_path, environ={"HOME": str(root)})
            self.assertFalse(report.ok)
            self.assertIn(
                "RECOGNIZED_MARKER_WITHOUT_INPUT",
                {item.code for item in report.diagnostics},
            )

    def test_opencode_read_only_snapshot_includes_committed_wal_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "opencode.db"
            writer = sqlite3.connect(database)
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("PRAGMA wal_autocheckpoint = 0")
                writer.executescript(
                    """
                    CREATE TABLE session (
                      id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
                      directory TEXT, time_created INTEGER, time_updated INTEGER
                    );
                    CREATE TABLE message (
                      id TEXT PRIMARY KEY, session_id TEXT,
                      time_created INTEGER, data TEXT
                    );
                    CREATE TABLE part (
                      id TEXT PRIMARY KEY, message_id TEXT,
                      time_created INTEGER, data TEXT
                    );
                    """
                )
                writer.commit()
                writer.execute(
                    "INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
                    (
                        "session-from-wal",
                        "Synthetic",
                        "/srv/example/project-one",
                        1,
                        2,
                    ),
                )
                writer.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    (
                        "message-from-wal",
                        "session-from-wal",
                        1,
                        json.dumps({"role": "user"}),
                    ),
                )
                writer.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?)",
                    (
                        "part-from-wal",
                        "message-from-wal",
                        1,
                        json.dumps({"type": "text", "text": "synthetic request"}),
                    ),
                )
                writer.commit()
                source_paths = (
                    database,
                    Path(f"{database}-wal"),
                    Path(f"{database}-shm"),
                )
                source_before = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in source_paths
                }
                output = root / "output"
                output.mkdir()
                data = manifest_data(
                    database,
                    output,
                    harness="opencode",
                    discovery_mode="file",
                    snapshot="sqlite-readonly",
                )
                report = run(
                    write_manifest(root / "manifest.json", data),
                    dry_run=True,
                    environ={"HOME": str(root)},
                )
                source_after = {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in source_paths
                }
                self.assertEqual(source_before, source_after)
            finally:
                writer.close()
        self.assertEqual(report.session_count, 1)

    def test_opencode_wal_symlink_escape_fails_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside_temporary,
        ):
            root = Path(temporary)
            database = root / "opencode.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE session (id TEXT)")
            connection.commit()
            connection.close()
            outside = Path(outside_temporary) / "outside-wal"
            outside.write_bytes(b"synthetic outside bytes")
            Path(f"{database}-wal").symlink_to(outside)
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                database,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="sqlite-readonly",
            )
            with self.assertRaises(PipelineError):
                run(
                    write_manifest(root / "manifest.json", data),
                    dry_run=True,
                    environ={"HOME": str(root)},
                )


if __name__ == "__main__":
    unittest.main()
