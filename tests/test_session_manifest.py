from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.manifest import ManifestError, load_manifest
from agent_skills.sessions.sources import (
    SourceAccessError,
    discover_candidates,
    snapshot_candidate,
    validate_configured_path,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ManifestTest(unittest.TestCase):
    def test_example_validates_against_formal_json_schema(self) -> None:
        schema = json.loads(
            (
                REPOSITORY_ROOT / "agent-session-extraction/schemas/manifest-v1.json"
            ).read_text(encoding="utf-8")
        )
        example = json.loads(
            (
                REPOSITORY_ROOT
                / "agent-session-extraction/references/manifest.example.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(example, schema)

    def test_missing_or_invalid_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ManifestError):
                load_manifest(root / "missing.json", environ={"HOME": str(root)})
            bad = root / "bad.json"
            bad.write_text("{}", encoding="utf-8")
            with self.assertRaises(ManifestError):
                load_manifest(bad, environ={"HOME": str(root)})

    def test_backup_profile_labels_do_not_select_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "configured"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            path = write_manifest(root / "manifest.json", data)
            manifest = load_manifest(
                path,
                environ={
                    "HOME": str(root / "native"),
                    "CLAUDE_PROFILES": "opaque:/must/not/be/read",
                },
            )
            self.assertEqual(manifest.sources[0].path, source)

    def test_native_default_requires_explicit_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "configured"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["path"] = {"kind": "native-default"}
            path = write_manifest(root / "manifest.json", data)
            manifest = load_manifest(path, environ={"HOME": str(root / "home")})
            self.assertEqual(
                manifest.sources[0].path, root / "home" / ".claude/projects"
            )

    def test_decoder_options_are_harness_specific_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "configured"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            for decoder in (
                {"unknown_option": True},
                {"conversation_kind": False},
            ):
                with self.subTest(decoder=decoder):
                    data = manifest_data(source, output, decoder=decoder)
                    with self.assertRaises(ManifestError):
                        load_manifest(
                            write_manifest(root / "manifest.json", data),
                            environ={"HOME": str(root)},
                        )

    def test_opencode_requires_a_read_only_sqlite_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "opencode.db"
            source.write_bytes(b"synthetic")
            output = root / "output"
            output.mkdir()
            data = manifest_data(
                source,
                output,
                harness="opencode",
                discovery_mode="file",
                snapshot="stable-bytes",
            )
            with self.assertRaises(ManifestError):
                load_manifest(
                    write_manifest(root / "manifest.json", data),
                    environ={"HOME": str(root)},
                )

    def test_required_source_suffix_must_not_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "configured"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["root_policy"]["required_suffixes"] = [""]
            with self.assertRaises(ManifestError):
                load_manifest(
                    write_manifest(root / "manifest.json", data),
                    environ={"HOME": str(root)},
                )

    def test_output_paths_and_headers_cannot_take_runtime_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "configured"
            source.mkdir()
            output = root / "output"
            output.mkdir()

            def dot_path(data):
                data["output"]["history_directory"] = "."

            def git_path(data):
                data["publisher"]["owned_subtrees"] = [".git/session", "Prompts"]

            def duplicate_views(data):
                data["output"]["prompt_directory"] = "History"

            def reserved_header(data):
                data["output"]["encryption_attributes"] = {"Tool": "forged"}

            for mutation in (dot_path, git_path, duplicate_views, reserved_header):
                with self.subTest(mutation=mutation.__name__):
                    data = manifest_data(source, output)
                    mutation(data)
                    with self.assertRaises(ManifestError):
                        load_manifest(
                            write_manifest(root / "manifest.json", data),
                            environ={"HOME": str(root)},
                        )


class PathConfinementTest(unittest.TestCase):
    def test_configured_realpath_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            source = allowed / "source"
            source.symlink_to(outside, target_is_directory=True)
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["root_policy"]["allowed_lexical_roots"] = [str(allowed)]
            data["sources"][0]["root_policy"]["allowed_resolved_roots"] = [str(allowed)]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            with self.assertRaises(SourceAccessError):
                validate_configured_path(manifest.sources[0])

    def test_configured_resolved_path_must_also_match_required_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            allowed.mkdir()
            resolved_target = allowed / "resolved-target"
            resolved_target.mkdir()
            source = allowed / "configured-restricted"
            source.symlink_to(resolved_target, target_is_directory=True)
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["root_policy"]["required_suffixes"] = ["-restricted"]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            with self.assertRaises(SourceAccessError):
                validate_configured_path(manifest.sources[0])

    def test_candidate_symlink_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            outside = root / "outside"
            source.mkdir()
            outside.mkdir()
            target = outside / "session.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            (source / "session.jsonl").symlink_to(target)
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["root_policy"]["allowed_resolved_roots"] = [str(source)]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            spec = manifest.sources[0]
            validated = validate_configured_path(spec)
            candidate = discover_candidates(spec, validated)[0]
            with self.assertRaises(SourceAccessError):
                snapshot_candidate(spec, validated, candidate)

    def test_unreadable_glob_tree_is_not_treated_as_authoritative_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["sources"][0]["allow_empty"] = True
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            spec = manifest.sources[0]
            validated = validate_configured_path(spec)

            def unreadable_tree(_path, *, onerror):
                onerror(PermissionError("synthetic access failure"))
                yield ()

            with (
                mock.patch(
                    "agent_skills.sessions.sources.os.walk",
                    side_effect=unreadable_tree,
                ),
                self.assertRaises(SourceAccessError),
            ):
                discover_candidates(spec, validated)


class PublisherManifestTest(unittest.TestCase):
    def test_owned_subtrees_must_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output)
            data["publisher"]["owned_subtrees"] = [
                "History",
                "History/Archive",
                "Prompts",
            ]
            with self.assertRaises(ManifestError):
                load_manifest(
                    write_manifest(root / "manifest.json", data),
                    environ={"HOME": str(root)},
                )


if __name__ == "__main__":
    unittest.main()
