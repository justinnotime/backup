import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from document_facts import runtime
from document_facts.config import ExtractionError, load_config
from document_facts.content import make_chunks
from document_facts.provider import call_llm, make_client
from document_facts.reports import collect_dated_events
from document_facts.runtime import Extractor, main
from document_facts.storage import (
    FIELDS,
    infer_year_context,
    load_documents,
    parse_response,
)


def facts(**updates):
    result = {key: [] for key in FIELDS}
    result.update(updates)
    return result


def task(text="Test task"):
    return {
        "task": text,
        "status": "shipped",
        "subtasks": ["Test subtask"],
        "blockers": [],
        "solution": "Confirmed",
        "files_touched": ["module.py"],
        "related_to": ["feature"],
    }


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [json.dumps(facts())])
        self.requests = []
        self.messages = self
        self.closed = False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        value = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=value)],
            usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            stop_reason="end_turn",
        )

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repository"
    source = repo / "Raw" / "documents" / "renamed-title--document"
    source.mkdir(parents=True)
    (source / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "docId": "document-full-id",
                "title": 'A "quoted" title: 2031',
                "sourceUrl": "https://docs.example.invalid/document/test",
                "layout": "single",
            }
        )
    )
    (source / "README.md").write_text(
        "# Start\nFeature work on 2031-04-12.\n\n# Next\nFeature revision.\n"
    )
    data = {
        "schema": "document-facts/v1",
        "repository_root": str(repo),
        "source_directory": "Raw/documents",
        "output_directory": "Wiki/facts",
        "documents": [{"id": "document-full-id", "output_slug": "old-title"}],
        "year_range": [2030, 2032],
        "llm": {
            "model": "synthetic-model",
            "base_url": "https://provider.example.invalid/api",
            "api_key_env": "SYNTHETIC_KEY",
            "max_attempts": 1,
        },
        "threads": [
            {
                "slug": "feature",
                "title": "Feature history",
                "what_it_covers": "Feature evolution",
                "search_terms": ["feature"],
                "exclude_terms": [],
            }
        ],
        "metadata": {
            "project": "synthetic-project",
            "timeline_title": "Synthetic timeline",
        },
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data))
    return SimpleNamespace(repo=repo, source=source, data=data, config=config)


def configured(setup):
    setup.config.write_text(json.dumps(setup.data))
    return load_config(setup.config)


def test_stable_id_preserves_output_and_loads_new_directory(setup):
    doc = load_documents(configured(setup))[0]
    assert doc.slug == "old-title"
    assert doc.source_slug == setup.source.name
    assert doc.doc_id == "document-full-id"
    assert infer_year_context(doc, (2030, 2032)) == "2031"


@pytest.mark.parametrize(
    "selection",
    [
        [{"id": "missing"}],
        [{"slug": "absent-title"}],
        [{"id": "document-full-id"}, {"id": "document-full-id"}],
    ],
)
def test_explicit_missing_or_duplicate_sources_fail(setup, selection):
    setup.data["documents"] = selection
    with pytest.raises(ExtractionError):
        load_documents(configured(setup))


def test_legacy_slug_unique_id_prefix_and_ambiguity(setup):
    setup.data["documents"] = ["document--old-heading"]
    assert load_documents(configured(setup))[0].doc_id == "document-full-id"
    second = setup.source.parent / "another"
    second.mkdir()
    (second / "manifest.yaml").write_text("docId: document-other-id\n")
    with pytest.raises(ExtractionError, match="ambiguous"):
        load_documents(configured(setup))


def test_tabs_preserve_order_and_strip_only_mechanical_comment(setup):
    manifest = yaml.safe_load((setup.source / "manifest.yaml").read_text())
    manifest.update(layout="tabs", tabs=[{"path": "tabs/b.md"}, {"path": "tabs/a.md"}])
    (setup.source / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (setup.source / "tabs").mkdir()
    (setup.source / "tabs/b.md").write_text(
        "<!-- mechanical -->\n# Beta\nSecond named tab first.\n"
    )
    (setup.source / "tabs/a.md").write_text("# Alpha\nFirst named tab second.\n")
    doc = load_documents(configured(setup))[0]
    assert [c["heading"] for c in make_chunks(doc.slug, doc.content)] == [
        "Beta",
        "Alpha",
    ]
    assert "mechanical" not in doc.content
    (setup.source / "tabs/a.md").unlink()
    with pytest.raises(ExtractionError, match="missing"):
        load_documents(configured(setup))


@pytest.mark.parametrize("tab", ["../../outside.md", "/tmp/outside.md", "tabs/link.md"])
def test_tabs_reject_escape_and_symlink(setup, tmp_path, tab):
    manifest = yaml.safe_load((setup.source / "manifest.yaml").read_text())
    manifest.update(layout="tabs", tabs=[{"path": tab}])
    (setup.source / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (setup.source / "tabs").mkdir()
    (setup.source / "tabs/link.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ExtractionError):
        load_documents(configured(setup))


@pytest.mark.parametrize(
    "key,value",
    [
        ("output_directory", "../escape"),
        ("output_directory", "Raw/documents/output"),
        ("documents", [{"id": "document-full-id", "output_slug": "../escape"}]),
        ("year_range", [2032, 2030]),
        ("budget", {"soft_split_chars": 17000}),
        ("llm", {"base_url": "https://key@provider.example.invalid"}),
    ],
)
def test_config_rejects_invalid_paths_and_values(setup, key, value):
    setup.data[key] = value
    with pytest.raises(ExtractionError):
        configured(setup)


def test_root_remaps_data_but_not_external_credential(setup, tmp_path):
    setup.data["llm"]["credential_file"] = "credentials.json"
    setup.data["source_directory"] = str(setup.repo / "Raw/documents")
    configured(setup)
    other = tmp_path / "worktree"
    settings = load_config(setup.config, other)
    assert settings.source_directory == other / "Raw/documents"
    assert settings.llm["credential_file"] == tmp_path / "credentials.json"


def test_chunking_retains_names_and_covers_long_document():
    text = "# Same\n" + "alpha " * 30 + "\n# Same\nsecond\n"
    chunks = make_chunks("test", text, max_chars=80, soft_chars=60)
    assert chunks[0]["chunk_id"] == "000-same"
    assert chunks[1]["chunk_id"] == "001-same-2"
    assert chunks[-1]["body"] == "second"
    assert all(len(c["body"]) <= 80 for c in chunks)
    assert "".join(c["body"].replace(" ", "") for c in chunks[:-1]) == "alpha" * 30


def test_dry_run_no_client_no_credentials_no_writes(setup, monkeypatch, capsys):
    setup.data["llm"]["credential_file"] = "missing.json"
    configured(setup)
    monkeypatch.setattr(
        runtime, "make_client", lambda *_: pytest.fail("client created in dry run")
    )
    before = sorted(str(p.relative_to(setup.repo)) for p in setup.repo.rglob("*"))
    assert main(["--config", str(setup.config), "--dry-run"]) == 0
    after = sorted(str(p.relative_to(setup.repo)) for p in setup.repo.rglob("*"))
    assert before == after
    output = json.loads(capsys.readouterr().out)
    assert output["processed"] == 2
    assert output["estimated_input_tokens"] > 0
    assert output["maximum_output_tokens"] == 16000


def test_doctor_unknown_only_fails(setup):
    assert main(["--config", str(setup.config), "--doctor"]) == 0
    assert main(["--config", str(setup.config), "--only", "missing", "--dry-run"]) == 1


def test_success_checkpoint_resume_from_yaml_when_state_lost(setup):
    settings = configured(setup)
    extractor = Extractor(settings)
    client = FakeClient([json.dumps(facts(tasks=[task()], dates_found=["2031-04-12"]))])
    assert extractor.extraction(client)["processed"] == 2
    settings.state_file.unlink()
    result = extractor.extraction(FakeClient([RuntimeError("must not call")]))
    assert result["skipped"] == 2
    assert result["processed"] == 0
    assert not settings.state_file.exists()
    readme = settings.output_directory / "old-title/README.md"
    frontmatter = yaml.safe_load(readme.read_text().split("---", 2)[1])
    assert frontmatter["title"] == 'Extraction: A "quoted" title: 2031'
    assert frontmatter["sources"] == ["Raw/documents/renamed-title--document/"]
    assert frontmatter["project"] == "synthetic-project"


def test_legacy_yaml_without_signature_resumes(setup):
    settings = configured(setup)
    extractor = Extractor(settings)
    extractor.extraction(FakeClient())
    for path in settings.output_directory.glob("*/*.yaml"):
        data = yaml.safe_load(path.read_text())
        data.pop("extraction_signature")
        data.pop("doc_id")
        path.write_text(yaml.safe_dump(data))
    settings.state_file.unlink()
    assert extractor.extraction(FakeClient([RuntimeError()]))["skipped"] == 2


def test_changed_prompt_or_source_forces_only_necessary_reextraction(setup):
    settings = configured(setup)
    Extractor(settings).extraction(FakeClient())
    setup.data["prompts"] = {"extract": "Use a different extraction prompt."}
    settings = configured(setup)
    assert Extractor(settings).extraction(FakeClient())["processed"] == 2
    (setup.source / "README.md").write_text(
        "# Start\nFeature changed.\n\n# Next\nFeature revision.\n"
    )
    result = Extractor(settings).extraction(FakeClient())
    assert result["processed"] == 1
    assert result["skipped"] == 1


@pytest.mark.parametrize(
    "response",
    [
        RuntimeError("private provider diagnostic"),
        "{}",
        "[]",
        "<html>provider error</html>",
        json.dumps(facts(dates_found=["2031-99-99"])),
        json.dumps({**facts(), "input_sha1": "forged"}),
    ],
)
def test_failure_never_creates_checkpoint_or_digest(setup, response, capsys):
    settings = configured(setup)
    result = Extractor(settings).extraction(FakeClient([response]))
    assert result["errors"] == 2
    assert not settings.state_file.exists()
    assert not settings.output_directory.exists()
    assert "private provider diagnostic" not in capsys.readouterr().err


def test_partial_failure_checkpoint_only_success_then_retry(setup):
    settings = configured(setup)
    extractor = Extractor(settings)
    result = extractor.extraction(FakeClient([json.dumps(facts()), RuntimeError()]))
    assert result["errors"] == 1
    state = json.loads(settings.state_file.read_text())
    assert list(state["old-title"]) == ["000-start"]
    assert not (settings.output_directory / "old-title/README.md").exists()
    assert extractor.extraction(FakeClient())["processed"] == 1


def test_limit_caps_actual_calls_and_does_not_write_partial_digest(setup):
    settings = configured(setup)
    client = FakeClient()
    result = Extractor(settings).extraction(client, limit=1)
    assert len(client.requests) == result["processed"] == 1
    assert not (settings.output_directory / "old-title/README.md").exists()


def test_digest_timeline_offline_and_inherited_dates(setup, monkeypatch):
    settings = configured(setup)
    Extractor(settings).extraction(
        FakeClient(
            [
                json.dumps(facts(tasks=[task()], dates_found=["2031-04-12"])),
                json.dumps(facts(tasks=[task("Second feature")])),
            ]
        )
    )
    monkeypatch.setattr(
        runtime, "make_client", lambda *_: pytest.fail("offline mode created client")
    )
    assert main(["--config", str(setup.config), "--digests-only"]) == 0
    assert main(["--config", str(setup.config), "--build-timeline"]) == 0
    text = settings.timeline_file.read_text()
    assert "# Synthetic timeline" in text
    assert text.count("## 2031-04-12") == 1
    assert "Second feature" in text
    assert "facts/old-title/000-start.yaml" in text
    extractor = Extractor(settings)
    dated, undated = collect_dated_events(
        extractor.documents,
        {d.slug: extractor.read_chunks(d) for d in extractor.documents},
    )
    assert len(dated) == 2 and not undated


def test_removed_chunk_is_excluded_from_report(setup):
    settings = configured(setup)
    Extractor(settings).extraction(
        FakeClient(
            [
                json.dumps(facts(tasks=[task()])),
                json.dumps(facts(tasks=[task("obsolete")])),
            ]
        )
    )
    (setup.source / "README.md").write_text("# Start\nFeature work on 2031-04-12.\n")
    Extractor(settings).extraction(FakeClient())
    assert (
        "obsolete"
        not in (settings.output_directory / "old-title/README.md").read_text()
    )


def test_saved_snapshot_reports_work_after_source_changes_without_model_calls(
    setup, monkeypatch
):
    settings = configured(setup)
    Extractor(settings).extraction(
        FakeClient([json.dumps(facts(tasks=[task("Feature from saved extraction")]))])
    )
    readme = settings.output_directory / "old-title/README.md"
    saved = {path.name: path.read_bytes() for path in readme.parent.glob("*.yaml")}
    (setup.source / "README.md").write_text("# Start\nNew content.\n")
    monkeypatch.setattr(
        runtime, "make_client", lambda *_: pytest.fail("snapshot report created client")
    )
    assert main(["--config", str(setup.config), "--digests-only"]) == 0
    assert "Feature from saved extraction" in readme.read_text()
    assert main(["--config", str(setup.config), "--build-timeline"]) == 0
    assert "Feature from saved extraction" in settings.timeline_file.read_text()
    assert main(["--config", str(setup.config), "--build-threads", "--dry-run"]) == 0
    assert {
        path.name: path.read_bytes() for path in readme.parent.glob("*.yaml")
    } == saved
    assert not settings.threads_directory.exists()
    # These modes need source identity from the manifest, not current text.
    (setup.source / "README.md").unlink()
    assert main(["--config", str(setup.config), "--digests-only"]) == 0


def test_snapshot_identity_failure_preserves_existing_report(setup):
    settings = configured(setup)
    Extractor(settings).extraction(FakeClient())
    readme = settings.output_directory / "old-title/README.md"
    before = readme.read_bytes()
    chunk = readme.parent / "000-start.yaml"
    value = yaml.safe_load(chunk.read_text())
    value["doc_slug"] = "another-document"
    chunk.write_text(yaml.safe_dump(value))
    assert main(["--config", str(setup.config), "--digests-only"]) == 1
    assert readme.read_bytes() == before


def test_thread_budget_caps_evidence_without_dropping_source_listing(setup):
    setup.data["budget"] = {"thread_prompt_chars": 1000}
    setup.data["threads"][0]["what_it_covers"] = "Synthetic theme description. " * 100
    settings = configured(setup)
    extractor = Extractor(settings)
    extractor.extraction(FakeClient([json.dumps(facts(tasks=[task("Feature work")]))]))
    theme = settings.threads[0]
    chunks = extractor.thread_chunks(theme)
    prompt, included = extractor.thread_prompt(
        theme, chunks, settings.threads_directory / "feature.md"
    )
    assert included > 0
    assert len(prompt) > settings.budget["thread_prompt_chars"]
    assert "old-title/000-start" in prompt and "old-title/001-next" in prompt


def test_threads_synthesis_resume_changes_and_offline_preview(setup, monkeypatch):
    settings = configured(setup)
    Extractor(settings).extraction(
        FakeClient([json.dumps(facts(tasks=[task("Feature work")]))])
    )
    extractor = Extractor(settings)
    client = FakeClient(["# Feature history\n\nThe evidence records a feature."])
    result = extractor.threads(client)
    assert result["wrote"] == 1
    assert extractor.threads(FakeClient([RuntimeError()]))["skipped"] == 1
    text = (settings.threads_directory / "feature.md").read_text()
    meta = yaml.safe_load(text.split("---", 2)[1])
    assert meta["sources_chunks"] == 2
    assert meta["input_sha256"]
    assert len(meta["sources"]) == 2
    assert "cache_control" in client.requests[0]["system"][0]
    monkeypatch.setattr(
        runtime, "make_client", lambda *_: pytest.fail("thread dry run called provider")
    )
    assert (
        main(["--config", str(setup.config), "--build-threads", "--dry-run", "--force"])
        == 0
    )
    assert (settings.threads_directory / "feature.md").read_text() == text


def test_thread_failure_does_not_create_page_or_index(setup):
    settings = configured(setup)
    extractor = Extractor(settings)
    extractor.extraction(FakeClient([json.dumps(facts(tasks=[task("Feature work")]))]))
    assert extractor.threads(FakeClient([RuntimeError()]))["errors"] == 1
    assert not settings.threads_directory.exists()


def test_explicit_missing_credentials_cannot_fall_back_to_home(
    setup, monkeypatch, tmp_path
):
    monkeypatch.delenv("SYNTHETIC_KEY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-do-not-discover")
    with pytest.raises(ExtractionError, match="missing"):
        make_client(configured(setup))
    setup.data["llm"]["required"] = False
    assert make_client(configured(setup)) is None


def test_real_sdk_client_disables_redirects_and_uses_only_explicit_config(
    setup, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_KEY", "synthetic-test-key")
    client = make_client(configured(setup))
    try:
        assert client.api_key == "synthetic-test-key"
        assert str(client.base_url) == "https://provider.example.invalid/api/"
        assert client._client.follow_redirects is False
        assert client.max_retries == 0
    finally:
        client.close()


def test_truncated_provider_response_is_not_success(setup):
    client = FakeClient()
    client.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(facts()))],
        usage=SimpleNamespace(),
        stop_reason="max_tokens",
    )
    client.messages = client
    with pytest.raises(ExtractionError, match="incomplete"):
        call_llm(configured(setup), client, "system", "user", 20)


def test_fenced_and_repaired_json_remains_schema_checked():
    assert parse_response("```json\n" + json.dumps(facts()) + "\n```") == facts()
    with pytest.raises(ExtractionError):
        parse_response('{"tasks": []}')


def test_cli_provider_failure_returns_nonzero_and_closes(setup, monkeypatch):
    client = FakeClient([RuntimeError()])
    monkeypatch.setattr(runtime, "make_client", lambda *_: client)
    assert main(["--config", str(setup.config)]) == 1
    assert client.closed


def test_independent_copied_package_launcher_offline(setup, tmp_path):
    package = Path(__file__).resolve().parents[1]
    copied = tmp_path / "copied" / "document-facts"
    shutil.copytree(
        package,
        copied,
        ignore=shutil.ignore_patterns(
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "*.egg-info",
            ".migration-evidence",
        ),
    )
    home = tmp_path / "empty-home"
    home.mkdir()
    result = subprocess.run(
        [str(copied / "scripts/extract"), "--config", str(setup.config), "--dry-run"],
        cwd=home,
        text=True,
        capture_output=True,
        env={
            "PATH": os.defpath,
            "HOME": str(home),
            "DOCUMENT_FACTS_PYTHON": sys.executable,
        },
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["processed"] == 2
    assert not (setup.repo / "Wiki").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("state_file", "Wiki/facts/old-title/000-start.yaml"),
        ("state_file", "Wiki/timeline.md"),
        ("threads_directory", "Wiki/facts/old-title"),
    ],
)
def test_output_collisions_fail_before_calls(setup, key, value):
    setup.data[key] = value
    with pytest.raises(ExtractionError, match="collide"):
        Extractor(configured(setup))


def test_no_pending_extraction_or_thread_does_not_create_client(setup, monkeypatch):
    settings = configured(setup)
    Extractor(settings).extraction(FakeClient())
    monkeypatch.setattr(
        runtime,
        "make_client",
        lambda *_: pytest.fail("no pending work must not read credentials"),
    )
    assert main(["--config", str(setup.config)]) == 0
    assert main(["--config", str(setup.config), "--build-threads"]) == 0


def test_credential_file_cannot_be_overwritten_by_generated_output(setup):
    setup.data["llm"]["credential_file"] = str(
        setup.repo / "Wiki/facts/old-title/000-start.yaml"
    )
    with pytest.raises(ExtractionError, match="credential"):
        Extractor(configured(setup))


def test_renamed_output_directory_reuses_explicit_historical_slug_without_rewriting(
    setup,
):
    settings = configured(setup)
    Extractor(settings).extraction(
        FakeClient([json.dumps(facts(tasks=[task("Feature work")]))])
    )
    old_dir = settings.output_directory / "old-title"
    new_dir = settings.output_directory / "current-title"
    old_dir.rename(new_dir)
    saved = {path.name: path.read_bytes() for path in new_dir.glob("*.yaml")}
    settings.state_file.unlink()
    setup.data["documents"][0].update(
        output_slug="current-title", previous_slugs=["old-title"]
    )
    extractor = Extractor(configured(setup))
    result = extractor.extraction(FakeClient([RuntimeError("must not call model")]))
    assert result["skipped"] == 2 and result["processed"] == 0
    assert {path.name: path.read_bytes() for path in new_dir.glob("*.yaml")} == saved
    assert not settings.state_file.exists()
    extractor.reports(timeline=True)
    assert "facts/current-title/000-start.yaml" in settings.timeline_file.read_text()
    assert (
        extractor.thread_chunks(setup.data["threads"][0])[0]["doc_slug"]
        == "current-title"
    )
    setup.data["documents"][0].pop("previous_slugs")
    with pytest.raises(ExtractionError, match="identity"):
        Extractor(configured(setup)).extraction(FakeClient())


@pytest.mark.parametrize(
    "field,value", [("chunk_id", "wrong-chunk"), ("doc_id", "wrong-document")]
)
def test_historical_slug_cannot_hide_wrong_identity(setup, field, value):
    settings = configured(setup)
    extractor = Extractor(settings)
    extractor.extraction(FakeClient())
    path = settings.output_directory / "old-title/000-start.yaml"
    data = yaml.safe_load(path.read_text())
    data.update(doc_slug="historical-title", **{field: value})
    path.write_text(yaml.safe_dump(data))
    setup.data["documents"][0]["previous_slugs"] = ["historical-title"]
    with pytest.raises(ExtractionError):
        Extractor(configured(setup)).extraction(FakeClient())
