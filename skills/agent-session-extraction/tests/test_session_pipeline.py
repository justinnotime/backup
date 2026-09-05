from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions import pipeline as pipeline_module
from agent_skills.sessions.api import reconcile, run
from agent_skills.sessions.manifest import load_manifest
from agent_skills.sessions.pipeline import PipelineError, evaluate_pipeline
from agent_skills.sessions.publish import PublishError


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


def git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return process.stdout


def git_worktree_manifest(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    write_claude(source / "session.jsonl")
    output = root / "output"
    output.mkdir()
    git(output, "init", "-q")
    git(output, "config", "user.name", "Synthetic Test")
    git(output, "config", "user.email", "synthetic@example.invalid")
    (output / "History").mkdir()
    (output / "Prompts").mkdir()
    (output / "History/.keep").write_text("history\n", encoding="utf-8")
    (output / "Prompts/.keep").write_text("prompts\n", encoding="utf-8")
    (output / "outside-tracked.txt").write_text("base\n", encoding="utf-8")
    (output / ".gitignore").write_text(
        "History/ignored.md\noutside-ignored.txt\n", encoding="utf-8"
    )
    git(output, "add", ".")
    git(output, "commit", "-qm", "synthetic output baseline")
    return write_manifest(
        root / "manifest.json",
        manifest_data(source, output, publisher="git-worktree"),
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

    def test_exact_superseded_candidate_is_visible_and_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            old_directory = source / "old-project"
            current_directory = source / "current-project"
            old_directory.mkdir(parents=True)
            current_directory.mkdir(parents=True)
            old_candidate = old_directory / "session.jsonl"
            current_candidate = current_directory / "session.jsonl"
            write_claude(old_candidate, text="superseded synthetic request")
            write_claude(current_candidate, text="current synthetic request")
            old_digest = hashlib.sha256(old_candidate.read_bytes()).hexdigest()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["discovery"]["superseded_sha256"] = [old_digest]
            manifest_path = write_manifest(root / "manifest.json", data)

            snapshot, _inventory, _plan, reconciliation, _redactor = (
                evaluate_pipeline(
                    load_manifest(manifest_path, environ={"HOME": str(root)})
                )
            )

            self.assertTrue(reconciliation.ok)
            self.assertEqual(len(snapshot.sessions), 1)
            self.assertEqual(
                {item.code for item in snapshot.diagnostics},
                {"SOURCE_CANDIDATE_SUPERSEDED"},
            )
            report = run(manifest_path, dry_run=True, environ={"HOME": str(root)})
            self.assertEqual(report.status, "ok")
            self.assertIn("SOURCE_CANDIDATE_SUPERSEDED", report.diagnostic_codes)

            write_claude(old_candidate, text="changed divergent synthetic request")
            with self.assertRaisesRegex(
                PipelineError, "RECONCILIATION_FAILURE"
            ):
                run(manifest_path, dry_run=True, environ={"HOME": str(root)})

    def test_superseded_hash_does_not_bypass_candidate_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            outside = root / "outside.jsonl"
            write_claude(outside, text="outside synthetic request")
            (source / "escaped.jsonl").symlink_to(outside)
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["discovery"]["superseded_sha256"] = [
                hashlib.sha256(outside.read_bytes()).hexdigest()
            ]

            with self.assertRaisesRegex(PipelineError, "SOURCE_FAILURE"):
                run(
                    write_manifest(root / "manifest.json", data),
                    dry_run=True,
                    environ={"HOME": str(root)},
                )

    def test_git_worktree_rejects_owned_output_that_differs_from_head(self) -> None:
        for difference in (
            "tracked",
            "untracked",
            "ignored",
            "assume-unchanged",
            "skip-worktree",
        ):
            with (
                self.subTest(difference=difference),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                manifest = git_worktree_manifest(root)
                output = root / "output"
                if difference == "tracked":
                    (output / "History/.keep").write_text(
                        "changed\n", encoding="utf-8"
                    )
                elif difference == "untracked":
                    (output / "History/untracked.md").write_text(
                        "untracked\n", encoding="utf-8"
                    )
                else:
                    if difference == "ignored":
                        (output / "History/ignored.md").write_text(
                            "ignored\n", encoding="utf-8"
                        )
                    elif difference == "assume-unchanged":
                        git(
                            output,
                            "update-index",
                            "--assume-unchanged",
                            "History/.keep",
                        )
                    else:
                        git(
                            output,
                            "update-index",
                            "--skip-worktree",
                            "History/.keep",
                        )

                with self.assertRaises(PipelineError) as caught:
                    run(
                        manifest,
                        dry_run=True,
                        environ={"HOME": str(root)},
                    )

                self.assertEqual(
                    caught.exception.code,
                    "GIT_WORKTREE_OUTPUT_NOT_AT_HEAD",
                )

    def test_git_worktree_allows_dirt_outside_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = git_worktree_manifest(root)
            output = root / "output"
            (output / "outside-tracked.txt").write_text(
                "changed outside\n", encoding="utf-8"
            )
            (output / "outside-untracked.txt").write_text(
                "untracked outside\n", encoding="utf-8"
            )
            (output / "outside-ignored.txt").write_text(
                "ignored outside\n", encoding="utf-8"
            )

            report = run(
                manifest,
                dry_run=True,
                environ={"HOME": str(root)},
            )

            self.assertEqual(report.session_count, 1)

    def test_git_worktree_rechecks_after_inventory_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = git_worktree_manifest(root)
            output = root / "output"
            destination = root / "prepared"
            original_prepare = pipeline_module.prepare_git_worktree

            def change_owned_output_then_prepare(manifest, plan, worktree):
                (output / "History/.keep").write_text(
                    "changed after inventory scan", encoding="utf-8"
                )
                return original_prepare(manifest, plan, worktree)

            with (
                mock.patch.object(
                    pipeline_module,
                    "prepare_git_worktree",
                    side_effect=change_owned_output_then_prepare,
                ),
                self.assertRaises(PublishError),
            ):
                run(
                    manifest,
                    dry_run=False,
                    git_worktree_destination=destination,
                    environ={"HOME": str(root)},
                )

            self.assertFalse(destination.exists())

    def test_git_worktree_detects_change_during_inventory_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = git_worktree_manifest(root)
            output = root / "output"
            original_scan = pipeline_module.scan_inventory

            def scan_then_change(manifest):
                inventory = original_scan(manifest)
                (output / "History/.keep").write_text(
                    "changed during inventory scan", encoding="utf-8"
                )
                return inventory

            with (
                mock.patch.object(
                    pipeline_module,
                    "scan_inventory",
                    side_effect=scan_then_change,
                ),
                self.assertRaises(PipelineError) as caught,
            ):
                run(
                    manifest_path,
                    dry_run=True,
                    environ={"HOME": str(root)},
                )

            self.assertEqual(caught.exception.code, "GIT_WORKTREE_OUTPUT_NOT_AT_HEAD")

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

    def test_source_event_policy_overrides_global_retention_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            write_claude(source / "session.jsonl", text="short")
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["event_policy"] = {
                "min_direct_user_events": 5,
                "min_user_chars": 30,
            }
            report = run(
                write_manifest(root / "manifest.json", data),
                dry_run=True,
                environ={"HOME": str(root)},
            )
            self.assertEqual(report.session_count, 0)

    def test_project_resolver_uses_private_manifest_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            project = source / "encoded-project-one"
            project.mkdir(parents=True)
            write_claude(project / "session.jsonl")
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["project_policy"]["resolvers"] = [
                {
                    "source_ids": ["source-a"],
                    "field": "source_ref",
                    "pattern": r"^[^/]+/encoded-(?P<project>[^/]+)/",
                }
            ]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            snapshot, _inventory, _plan, _reconcile, _redactor = evaluate_pipeline(
                manifest
            )
            self.assertEqual(snapshot.sessions[0].project, "project-one")

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
            (source / "marker-only.jsonl").write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "claude-future-user-format",
                        "message": {
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "synthetic future direct request",
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
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

    def test_opencode_immutable_snapshot_reads_without_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "opencode.db"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER);
                    CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
                    CREATE TABLE part (id TEXT, message_id TEXT, time_created INTEGER, data TEXT);
                    """
                )
                connection.execute(
                    "INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
                    ("immutable-session", "Synthetic", "/srv/example/project-one", 1, 2),
                )
                connection.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    ("message", "immutable-session", 1, json.dumps({"role": "user"})),
                )
                connection.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?)",
                    (
                        "part",
                        "message",
                        1,
                        json.dumps({"type": "text", "text": "synthetic request"}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                database,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="sqlite-immutable",
            )
            manifest = write_manifest(root / "manifest.json", data)
            before = tree_digest(root)
            report = run(
                manifest,
                dry_run=True,
                environ={"HOME": str(root)},
            )
            after = tree_digest(root)
            self.assertEqual(before, after)
            self.assertEqual(report.session_count, 1)
            self.assertFalse(Path(f"{database}-wal").exists())
            self.assertFalse(Path(f"{database}-shm").exists())

    def test_opencode_immutable_snapshot_rejects_nonempty_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "opencode.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE session (id TEXT)")
            connection.commit()
            connection.close()
            Path(f"{database}-wal").write_bytes(b"synthetic nonempty WAL")
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                database,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="sqlite-immutable",
            )
            with self.assertRaises(PipelineError):
                run(
                    write_manifest(root / "manifest.json", data),
                    dry_run=True,
                    environ={"HOME": str(root)},
                )


if __name__ == "__main__":
    unittest.main()
