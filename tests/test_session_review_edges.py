from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.api import decode_source_snapshots, run
from agent_skills.sessions.audit import AuditError, entry_from_content, scan_inventory
from agent_skills.sessions.harnesses.opencode import OpenCodeDecoder
from agent_skills.sessions.identity import (
    base_filename,
    identity_digest,
    relative_output_path,
)
from agent_skills.sessions.manifest import ManifestError, load_manifest
from agent_skills.sessions.model import (
    NORMALIZED_SCHEMA_VERSION,
    Event,
    ExtractionSnapshot,
    Session,
    SourceOutcome,
)
from agent_skills.sessions.pipeline import PipelineError, build_publication_plan
from agent_skills.sessions.redact import Redactor
from agent_skills.sessions.sources import (
    SourceAccessError,
    snapshot_candidate,
    validate_configured_path,
)


def _session() -> Session:
    timestamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    return Session(
        NORMALIZED_SCHEMA_VERSION,
        "codex",
        "session-000000000001",
        "source-a/session.jsonl",
        "node-a",
        None,
        "demo",
        timestamp,
        timestamp,
        (Event(0, timestamp, "exact", "user", "hello", "fixture.user"),),
        {},
    )


def _claude_payload(text: str) -> bytes:
    records = [
        {
            "type": "user",
            "sessionId": "claude-session-example",
            "cwd": "/synthetic/project-one",
            "timestamp": "2026-02-03T04:05:06Z",
            "message": {"content": text},
        },
        {
            "type": "assistant",
            "timestamp": "2026-02-03T04:05:07Z",
            "message": {"content": [{"type": "text", "text": "answer"}]},
        },
    ]
    return b"".join(json.dumps(item).encode() + b"\n" for item in records)


def _write_opencode_database(path: Path) -> None:
    connection = sqlite3.connect(path)
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
            ("immutable-session", "Synthetic", "/synthetic/project", 1, 2),
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


def _legacy_prompt(text: str) -> str:
    return f"""# old prompt

- Tool: codex
- Project: demo
- Host: node-a
- Prompts: 1

---

### 2026-01-02 03:04:00Z

{text}

---
"""


def _legacy_history(session_id: str, text: str) -> str:
    return f"""# old history

- Session ID: `{session_id}`
- Host: `node-a`
- Project: `demo`
- Tool: `codex`

---

### 2026-01-02 03:04:00Z — user

> {text}
"""


class FrozenSnapshotApiTest(unittest.TestCase):
    def test_byte_snapshot_is_not_reread_after_the_caller_freezes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            source_file = source / "session.jsonl"
            source_file.write_bytes(_claude_payload("original request"))
            output = root / "output"
            output.mkdir()
            manifest = load_manifest(
                write_manifest(root / "manifest.json", manifest_data(source, output)),
                environ={"HOME": str(root)},
            )
            source_spec = manifest.sources[0]
            validated = validate_configured_path(source_spec)
            frozen = snapshot_candidate(source_spec, validated, source_file)

            source_file.write_bytes(_claude_payload("changed after snapshot"))
            result = decode_source_snapshots(manifest, source_spec, (frozen,))

            user_text = [
                event.text
                for session in result.sessions
                for event in session.events
                if event.role == "user"
            ]
            self.assertEqual(user_text, ["original request"])

            mismatched = replace(frozen, node_label="another-node")
            with self.assertRaises(SourceAccessError):
                decode_source_snapshots(manifest, source_spec, (mismatched,))

    def test_relative_output_override_raises_manifest_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "session.jsonl").write_bytes(_claude_payload("request"))
            output = root / "output"
            output.mkdir()
            manifest_path = write_manifest(
                root / "manifest.json", manifest_data(source, output)
            )
            with self.assertRaises(ManifestError):
                run(
                    manifest_path,
                    dry_run=True,
                    environ={"HOME": str(root)},
                    output_root=Path("relative-output"),
                )


class ImmutableSqliteTest(unittest.TestCase):
    def test_public_frozen_api_rejects_direct_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "opencode.db"
            _write_opencode_database(database)
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                database,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="sqlite-immutable",
            )
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            source = manifest.sources[0]
            direct = snapshot_candidate(
                source,
                validate_configured_path(source),
                database,
            )

            with self.assertRaises(SourceAccessError):
                decode_source_snapshots(manifest, source, (direct,))

    def test_source_change_after_decode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "opencode.db"
            _write_opencode_database(database)
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                database,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="sqlite-immutable",
            )
            manifest_path = write_manifest(root / "manifest.json", data)
            original_decode = OpenCodeDecoder.decode

            def decode_then_change(decoder, snapshot):
                batch = original_decode(decoder, snapshot)
                if snapshot.access_mode == "sqlite-immutable":
                    current = database.stat()
                    os.utime(
                        database,
                        ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000),
                    )
                return batch

            with (
                mock.patch.object(OpenCodeDecoder, "decode", new=decode_then_change),
                self.assertRaises(PipelineError),
            ):
                run(
                    manifest_path,
                    dry_run=True,
                    environ={"HOME": str(root)},
                )


class IndexConvergenceTest(unittest.TestCase):
    def test_second_dry_run_reports_no_index_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "session.jsonl").write_bytes(_claude_payload("request"))
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                source,
                output,
                ownership="aggregator",
                cleanup="aggregator",
                publisher="filesystem-atomic",
                indexes="aggregator-only",
            )
            manifest_path = write_manifest(root / "manifest.json", data)

            run(manifest_path, environ={"HOME": str(root)})
            converged = run(
                manifest_path,
                dry_run=True,
                environ={"HOME": str(root)},
            )

            self.assertEqual(converged.write_count, 0)
            self.assertEqual(converged.removal_count, 0)


class LegacyAdoptionTest(unittest.TestCase):
    def test_legacy_provenance_is_preserved_as_static_content(self) -> None:
        entry = entry_from_content(
            "history/PROVENANCE.md",
            b"# Provenance\n\nRepository-owned source documentation.\n",
            compatibility_rule="legacy-agent-markdown/v1",
            legacy_kind="history",
            legacy_harness="cursor",
        )

        self.assertEqual(entry.kind, "static")
        self.assertTrue(entry.grandfathered)
        self.assertIsNone(entry.identity)

    def _manifest(
        self,
        root: Path,
        *,
        compatibility_rule: str = "legacy-agent-markdown/v1",
        cleanup: str = "none",
        ownership: str = "owner",
    ):
        source = root / "source"
        source.mkdir()
        output = root / "output"
        output.mkdir()
        data = manifest_data(
            source,
            output,
            cleanup=cleanup,
            ownership=ownership,
        )
        data["output"]["compatibility"]["rule_version"] = compatibility_rule
        return load_manifest(
            write_manifest(root / "manifest.json", data),
            environ={"HOME": str(root)},
        )

    def test_duplicate_legacy_identity_fails_instead_of_surviving_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            history = manifest.output.repository_root / "History" / "2026-01"
            history.mkdir(parents=True)
            for name in ("first.md", "second.md"):
                (history / name).write_text(
                    _legacy_history("same-session", name), encoding="utf-8"
                )

            with self.assertRaises(AuditError):
                scan_inventory(manifest)

    def test_excluded_project_removes_matching_legacy_prompt_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            manifest = replace(
                manifest,
                project_policy=replace(
                    manifest.project_policy,
                    prompt_by_harness={
                        "codex": {
                            "mode": "denylist",
                            "unknown": "keep",
                            "allowlist": (),
                            "denylist": ("demo",),
                        }
                    },
                ),
            )
            prompt = manifest.output.repository_root / "Prompts" / "orphan.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text(_legacy_prompt("hello"), encoding="utf-8")
            inventory = scan_inventory(manifest)
            current = _session()
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
                {"Prompts/orphan.md"},
            )
            self.assertEqual(
                [item for item in plan.writes if item.kind == "prompt"],
                [],
            )

    def test_frozen_rule_refreshes_changed_legacy_files_and_never_cleans_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(
                root,
                compatibility_rule="legacy-agent-markdown-frozen/v1",
                cleanup="aggregator",
                ownership="aggregator",
            )
            history = manifest.output.repository_root / "History" / "legacy.md"
            prompt = manifest.output.repository_root / "Prompts" / "legacy.md"
            history.parent.mkdir(parents=True)
            prompt.parent.mkdir(parents=True)
            history.write_text(
                _legacy_history("session-000000000001", "older text"),
                encoding="utf-8",
            )
            prompt.write_text(_legacy_prompt("older text"), encoding="utf-8")
            inventory = scan_inventory(manifest)
            current = _session()
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

            # Legacy files whose session kept growing are rendered again under
            # the current contract at their existing paths.
            history_writes = [item for item in plan.writes if item.kind == "history"]
            self.assertEqual(
                [item.relative_path for item in history_writes],
                ["History/legacy.md"],
            )
            self.assertIn(
                b"- Managed-By: agent-session-extraction/v1",
                history_writes[0].content,
            )
            self.assertIn(b"hello", history_writes[0].content)
            self.assertEqual(
                [item.relative_path for item in plan.writes if item.kind == "prompt"],
                ["Prompts/legacy.md"],
            )
            self.assertEqual(plan.removals, ())

            # Legacy files are never cleaned while the rule is active.
            without_current_session = replace(snapshot, sessions=())
            cleanup_plan = build_publication_plan(
                manifest,
                without_current_session,
                inventory,
                Redactor.from_spec(manifest.redaction),
            )
            self.assertEqual(cleanup_plan.removals, ())

    def test_frozen_rule_adopts_unchanged_legacy_history_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(
                root,
                compatibility_rule="legacy-agent-markdown-frozen/v1",
                cleanup="aggregator",
                ownership="aggregator",
            )
            history = manifest.output.repository_root / "History" / "legacy.md"
            prompt = manifest.output.repository_root / "Prompts" / "legacy.md"
            history.parent.mkdir(parents=True)
            prompt.parent.mkdir(parents=True)
            history.write_text(
                _legacy_history("session-000000000001", "hello"),
                encoding="utf-8",
            )
            prompt.write_text(_legacy_prompt("hello"), encoding="utf-8")
            snapshot = ExtractionSnapshot(
                (_session(),),
                (SourceOutcome("source-a", "node-a", "success", 1, 1),),
                {},
            )

            plan = build_publication_plan(
                manifest,
                snapshot,
                scan_inventory(manifest),
                Redactor.from_spec(manifest.redaction),
            )

            self.assertEqual(
                [item for item in plan.writes if item.kind in {"history", "prompt"}],
                [],
            )
            self.assertEqual(plan.removals, ())

    def test_frozen_rule_still_writes_new_output_under_current_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(
                root,
                compatibility_rule="legacy-agent-markdown-frozen/v1",
            )
            current = _session()
            snapshot = ExtractionSnapshot(
                (current,),
                (SourceOutcome("source-a", "node-a", "success", 1, 1),),
                {},
            )

            plan = build_publication_plan(
                manifest,
                snapshot,
                scan_inventory(manifest),
                Redactor.from_spec(manifest.redaction),
            )

            self.assertEqual({item.kind for item in plan.writes}, {"history", "prompt"})
            self.assertTrue(
                all(b"- Managed-By: agent-session-extraction/v1" in item.content for item in plan.writes)
            )
            prompt_content = next(
                item.content.decode() for item in plan.writes if item.kind == "prompt"
            )
            self.assertIn("### 2026-01-02 03:04:00Z", prompt_content)
            self.assertIn("- Project: demo\n\n---\n\n###", prompt_content)
            self.assertTrue(prompt_content.endswith("\n\n---\n"))

    def test_frozen_semantically_paired_prompt_ignores_project_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(
                root,
                compatibility_rule="legacy-agent-markdown-frozen/v1",
                cleanup="aggregator",
                ownership="aggregator",
            )
            manifest = replace(
                manifest,
                project_policy=replace(
                    manifest.project_policy,
                    prompt_by_harness={
                        "codex": {
                            "mode": "denylist",
                            "unknown": "keep",
                            "allowlist": (),
                            "denylist": ("demo",),
                        }
                    },
                ),
            )
            prompt = manifest.output.repository_root / "Prompts" / "orphan.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text(_legacy_prompt("hello"), encoding="utf-8")
            inventory = scan_inventory(manifest)
            current = _session()
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

            self.assertNotIn(
                "Prompts/orphan.md",
                {item.relative_path for item in plan.removals},
            )
            self.assertNotIn(
                "Prompts/orphan.md",
                {item.relative_path for item in plan.writes},
            )

    def test_reserved_legacy_paths_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            current = _session()
            base = relative_output_path(
                manifest.output.prompt_directory,
                manifest.output.layout,
                current,
                base_filename(current),
            )
            base_path = manifest.output.repository_root / base
            base_path.parent.mkdir(parents=True)
            base_path.write_text(_legacy_prompt("different base"), encoding="utf-8")
            path = Path(base)
            suffixed = (
                f"{path.parent.as_posix()}/{path.stem}--"
                f"{identity_digest(current.identity)}{path.suffix}"
            )
            suffix_path = manifest.output.repository_root / suffixed
            suffix_path.write_text(
                _legacy_prompt("different suffix"), encoding="utf-8"
            )
            inventory = scan_inventory(manifest)
            snapshot = ExtractionSnapshot(
                (current,),
                (SourceOutcome("source-a", "node-a", "success", 1, 1),),
                {},
            )

            try:
                plan = build_publication_plan(
                    manifest,
                    snapshot,
                    inventory,
                    Redactor.from_spec(manifest.redaction),
                )
            except AuditError:
                return
            occupied = {base, suffixed}
            prompt_writes = {
                item.relative_path for item in plan.writes if item.kind == "prompt"
            }
            self.assertTrue(prompt_writes)
            self.assertTrue(prompt_writes.isdisjoint(occupied))


class ManifestPolicyTest(unittest.TestCase):
    def test_output_views_must_not_be_nested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            for key, value in (
                ("prompt_directory", "History/Prompts"),
                ("history_directory_by_harness", {"codex": "History/Codex"}),
            ):
                data = manifest_data(source, output)
                data["output"][key] = value
                with self.subTest(key=key), self.assertRaises(ManifestError):
                    load_manifest(
                        write_manifest(root / f"{key}.json", data),
                        environ={"HOME": str(root)},
                    )

    def test_project_resolver_rejects_unknown_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["project_policy"]["resolvers"] = [
                {
                    "source_ids": ["missing-source"],
                    "field": "source_ref",
                    "pattern": r"^(?P<project>[^/]+)/",
                }
            ]
            with self.assertRaises(ManifestError):
                load_manifest(
                    write_manifest(root / "manifest.json", data),
                    environ={"HOME": str(root)},
                )


if __name__ == "__main__":
    unittest.main()
