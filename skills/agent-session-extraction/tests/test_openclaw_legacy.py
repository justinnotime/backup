from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.api import run
from agent_skills.sessions.audit import AuditError, entry_from_content, scan_inventory
from agent_skills.sessions.manifest import ManifestError, load_manifest
from agent_skills.sessions.pipeline import evaluate_pipeline

SESSION = "01234567-89ab-cdef-0123-456789abcdef"


def records(*, extra=False, session_id=SESSION, header_time="2024-01-01T23:59:00Z"):
    rows = [
        {"type": "session", "id": session_id, "timestamp": header_time},
        {
            "type": "message",
            "timestamp": "2024-01-02T10:00:00Z",
            "message": {"role": "user", "content": "A synthetic request"},
        },
        {
            "type": "message",
            "timestamp": "2024-01-02T10:01:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "A synthetic response"}],
            },
        },
    ]
    if extra:
        rows.append(
            {
                "type": "message",
                "timestamp": "2024-01-03T10:00:00Z",
                "message": {"role": "user", "content": "A later synthetic request"},
            }
        )
    return "".join(json.dumps(row) + "\n" for row in rows)


def legacy(session_id=SESSION):
    return (
        f"# Claw Session {session_id[:8]}\n\n"
        "- **Date:** 2024-01-01 23:59 UTC\n"
        f"- **Session ID:** `{session_id}`\n"
        "- **Channel:** synthetic\n"
        "- **Messages:** 2 (user + assistant text only)\n\n---\n\n"
        "## \U0001f464 User (10:00)\n\nA synthetic request\n\n"
        "## \U0001f916 Assistant (10:01)\n\nA synthetic response\n"
    )


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "session.jsonl").write_text(records())
    output = tmp_path / "output"
    directory = output / "History/2024-01"
    directory.mkdir(parents=True)
    path = directory / "session-2024-01-01_01234567.md"
    path.write_text(legacy())
    data = manifest_data(
        source,
        output,
        harness="openclaw",
        cleanup="none",
        publisher="filesystem-atomic",
        decoder={
            "minimum_user_events": 1,
            "minimum_total_events": 2,
            "include_session_metadata": True,
            "session_metadata_fields": ["timestamp"],
        },
    )
    data["output"].update(
        {
            "history_directory_by_harness": {"openclaw": "History"},
            "filename_strategy_by_harness": {"openclaw": "session-date-prefix-8"},
        }
    )
    data["output"]["compatibility"].update(
        {
            "rule_version": "legacy-agent-markdown-frozen/v1",
            "legacy_openclaw_node": "node-a",
        }
    )
    data["project_policy"]["prompt_by_harness"] = {
        "openclaw": {
            "mode": "allowlist",
            "unknown": "drop",
            "allowlist": [],
            "denylist": [],
        }
    }
    return source, output, path, data, tmp_path / "manifest.json"


def test_existing_legacy_bytes_and_static_files_are_preserved_without_duplicates(setup):
    _source, output, _path, data, config_path = setup
    static = output / "History/notes.md"
    static.write_text("# Synthetic static archive\n")
    data["output"]["compatibility"]["static_paths"] = ["History/notes.md"]
    config = write_manifest(config_path, data)
    before = {
        str(item.relative_to(output)): item.read_bytes()
        for item in output.rglob("*.md")
    }
    manifest = load_manifest(config, environ={})
    snapshot, inventory, plan, report, _ = evaluate_pipeline(manifest)
    assert report.ok
    assert len(snapshot.sessions) == 1
    assert plan.writes == plan.removals == ()
    assert inventory.by_path()["History/notes.md"].kind == "static"
    run(config)
    assert {
        str(item.relative_to(output)): item.read_bytes()
        for item in output.rglob("*.md")
    } == before
    assert not list((output / "Prompts").rglob("*.md"))


def test_growing_legacy_session_updates_same_path_then_reuses_it(setup):
    source, output, path, data, config_path = setup
    (source / "session.jsonl").write_text(records(extra=True))
    config = write_manifest(config_path, data)
    run(config)
    result = path.read_text()
    assert "- Managed-By: agent-session-extraction/v1" in result
    assert "A synthetic request" in result and "A synthetic response" in result
    assert "A later synthetic request" in result
    assert list(output.rglob("*.md")) == [path]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    run(config)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_new_session_uses_selected_header_date_and_original_filename_shape(setup):
    source, output, _path, data, config_path = setup
    (source / "other.jsonl").write_text(
        records(session_id="abcdef01-2222-3333-4444-555555555555")
    )
    run(write_manifest(config_path, data))
    new = output / "History/2024-01/session-2024-01-01_abcdef01.md"
    assert new.is_file()
    assert "Managed-By: agent-session-extraction/v1" in new.read_text()
    assert len(list(output.rglob("*.md"))) == 2


def test_adoption_requires_explicit_node_and_explicit_harness_route(setup):
    _source, _output, _path, data, config_path = setup
    del data["output"]["compatibility"]["legacy_openclaw_node"]
    with pytest.raises(AuditError, match="configured ownership"):
        scan_inventory(load_manifest(write_manifest(config_path, data), environ={}))
    data["output"]["compatibility"]["legacy_openclaw_node"] = "node-a"
    del data["output"]["history_directory_by_harness"]
    with pytest.raises(ManifestError, match="explicit history route"):
        load_manifest(write_manifest(config_path, data), environ={})


@pytest.mark.parametrize(
    "paths",
    [
        ["../secret.md"],
        ["History/*.md"],
        ["Other/file.md"],
        ["History/a.md", "History/a.md"],
    ],
)
def test_static_preservation_is_literal_confined_and_unique(setup, paths):
    _source, _output, _path, data, config_path = setup
    data["output"]["compatibility"]["static_paths"] = paths
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(config_path, data), environ={})


def test_unconfigured_static_file_blocks_publication_and_preserves_everything(setup):
    _source, output, path, data, config_path = setup
    static = output / "History/notes.md"
    static.write_text("# Synthetic static archive\n")
    before = path.read_bytes()
    with pytest.raises(AuditError):
        scan_inventory(load_manifest(write_manifest(config_path, data), environ={}))
    assert path.read_bytes() == before


def test_static_configuration_cannot_hide_session_records(setup):
    _source, _output, path, data, config_path = setup
    data["output"]["compatibility"]["static_paths"] = ["History/2024-01/" + path.name]
    with pytest.raises(AuditError, match="contains session records"):
        scan_inventory(load_manifest(write_manifest(config_path, data), environ={}))


def test_adoption_does_not_infer_node_from_body_or_filename():
    data = legacy().replace("- **Channel:** synthetic", "- **Host:** another-node")
    with pytest.raises(AuditError, match="contradicts"):
        entry_from_content(
            "History/session-2024-01-01_01234567.md",
            data.encode(),
            compatibility_rule="legacy-agent-markdown-frozen/v1",
            legacy_kind="history",
            legacy_harness="openclaw",
            legacy_openclaw_node="node-a",
        )


def configure_metadata(setup, rows):
    source, _output, _path, data, config_path = setup
    metadata = source.parent / "sessions.json"
    metadata.write_text(json.dumps(rows))
    decoder = data["sources"][0]["decoder"]
    decoder.update(
        {
            "sessions_metadata_path": str(metadata),
            "include_channel_metadata": True,
            "channel": "unknown",
            "session_metadata_fields": ["timestamp", "label"],
        }
    )
    root_policy = data["sources"][0]["root_policy"]
    root_policy["allowed_lexical_roots"] = [str(source.parent)]
    root_policy["allowed_resolved_roots"] = [str(source.parent)]
    data["output"]["metadata_headers"] = {"Label": "label", "Channel": "channel"}
    return metadata, write_manifest(config_path, data)


@pytest.mark.parametrize("wrapped", [False, True])
def test_explicit_sidecar_metadata_survives_growth_and_metadata_only_updates(
    setup, wrapped
):
    source, _output, path, _data, _config_path = setup
    rows = [{"id": SESSION, "label": "Synthetic label", "channel": "synthetic"}]
    metadata, config = configure_metadata(
        setup, {"sessions": rows} if wrapped else rows
    )
    path.write_text(
        legacy().replace(
            "- **Channel:**", "- **Label:** Synthetic label\n- **Channel:**"
        )
    )
    before = path.read_bytes()
    run(config)
    assert path.read_bytes() == before
    (source / "session.jsonl").write_text(records(extra=True))
    run(config)
    assert "- Label: Synthetic label\n" in path.read_text()
    assert "- Channel: synthetic\n" in path.read_text()
    rows[0]["label"] = "Updated synthetic label"
    metadata.write_text(json.dumps(rows))
    run(config)
    assert "- Label: Updated synthetic label\n" in path.read_text()


@pytest.mark.parametrize(
    "rows",
    [{}, [None], [{"id": SESSION}, {"id": SESSION}], [{"id": SESSION, "label": []}]],
)
def test_invalid_sidecar_preserves_output_and_fails_source(setup, rows):
    _source, _output, path, _data, _config_path = setup
    _metadata, config = configure_metadata(setup, rows)
    before = path.read_bytes()
    with pytest.raises(Exception, match="SOURCE"):
        run(config)
    assert path.read_bytes() == before


def test_sidecar_cannot_escape_declared_source_root(setup):
    source, _output, path, data, config_path = setup
    _metadata, _config = configure_metadata(setup, [])
    for key in ("allowed_lexical_roots", "allowed_resolved_roots"):
        data["sources"][0]["root_policy"][key] = [str(source)]
    with pytest.raises(Exception, match="SOURCE"):
        run(write_manifest(config_path, data))
    assert path.read_text() == legacy()


def test_static_patterns_preserve_new_matching_files_and_do_not_hide_sessions(setup):
    _source, output, path, data, config_path = setup
    data["output"]["compatibility"]["static_patterns"] = ["History/notes-????-??-??.md"]
    config = write_manifest(config_path, data)
    for date in ("2024-01-01", "2024-01-02"):
        static = output / f"History/notes-{date}.md"
        static.write_text("# Synthetic static document\n")
        run(config)
        assert static.read_text() == "# Synthetic static document\n"
    static.write_text(legacy())
    with pytest.raises(AuditError, match="contains session records"):
        scan_inventory(load_manifest(config, environ={}))
    assert path.read_text() == legacy()


@pytest.mark.parametrize(
    "pattern", ["../*.md", "Other/*.md", "History/**/*.md", "*.md"]
)
def test_static_patterns_are_confined_nonrecursive(setup, pattern):
    _source, _output, _path, data, config_path = setup
    data["output"]["compatibility"]["static_patterns"] = [pattern]
    with pytest.raises(ManifestError):
        load_manifest(write_manifest(config_path, data), environ={})


def test_metadata_headers_cannot_replace_identity(setup):
    _source, _output, _path, data, config_path = setup
    data["output"]["metadata_headers"] = {"Host": "label"}
    with pytest.raises(ManifestError, match="non-reserved"):
        load_manifest(write_manifest(config_path, data), environ={})


def test_doctor_checks_sidecar_path_without_reading_its_contents(setup):
    from agent_skills.sessions.api import doctor

    metadata, config = configure_metadata(setup, [])
    metadata.write_text("not JSON")
    assert doctor(config)["status"] == "ok"
    metadata.unlink()
    assert doctor(config)["status"] == "failed"


def test_added_configuration_matches_json_schema(setup):
    import jsonschema

    _source, _output, _path, data, _config_path = setup
    configure_metadata(setup, [])
    data["output"]["compatibility"]["static_patterns"] = ["History/memo-????-??-??.md"]
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/manifest-v1.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(data)
