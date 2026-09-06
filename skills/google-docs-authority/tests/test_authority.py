import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from google_docs_authority import config, fingerprint, publisher, registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        publisher,
        "open_request",
        Mock(side_effect=AssertionError("unexpected network")),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "articles").mkdir()
    source = tmp_path / "articles/article.md"
    source.write_text(
        "---\ntitle: Example\ngdoc:\n  id: example-doc\n  mode: published\n  fingerprint: sha256:old\n---\n# Example\nbody\n"
    )
    path = tmp_path / "config.json"
    value = {
        "schema": "google-docs-authority/v1",
        "write_token_file": "token.json",
        "registry": {
            "repository_root": ".",
            "output": "registry.json",
            "source_directories": ["articles"],
            "mirror_directory": "mirrors",
            "source_lists": {},
        },
    }
    path.write_text(json.dumps(value))
    monkeypatch.setenv("GOOGLE_DOCS_AUTHORITY_CONFIG", str(path))
    return source, path, value


def run_update(source, *extra):
    return publisher.main([str(source), "--update", "example-doc", *extra])


def mock_upload(monkeypatch, source, before="sha256:old", after=None):
    monkeypatch.setattr(publisher, "access_token", Mock(return_value="synthetic-token"))
    monkeypatch.setattr(
        publisher,
        "live_fingerprint",
        Mock(
            side_effect=[before, after or fingerprint.fingerprint(source.read_text())]
        ),
    )
    call = Mock(return_value=({"id": "example-doc", "name": "Example"}, None))
    monkeypatch.setattr(publisher, "call", call)
    return call


def test_update_records_actual_export_and_preserves_metadata(setup, monkeypatch):
    source, _, _ = setup
    source.write_text(
        source.read_text().replace("  mode:", "  owner_note: keep\n  mode:")
    )
    expected = fingerprint.fingerprint(source.read_text())
    original_mode = source.stat().st_mode
    call = mock_upload(monkeypatch, source)
    assert run_update(source) == 0
    assert call.call_count == 1
    assert call.call_args.kwargs["method"] == "PATCH"
    state = publisher.recorded_gdoc(source)
    assert state["fingerprint"] == expected
    assert state["owner_note"] == "keep"
    assert source.read_text().endswith("# Example\nbody\n")
    assert source.stat().st_mode == original_mode


@pytest.mark.parametrize(
    "live,force,code", [(None, False, 3), (None, True, 3), ("changed", False, 5)]
)
def test_preflight_failure_never_uploads(setup, monkeypatch, live, force, code):
    source, _, _ = setup
    old = source.read_bytes()
    call = mock_upload(monkeypatch, source, before=live)
    if code == 5:
        assert run_update(source) == code
    else:
        with pytest.raises(SystemExit) as result:
            run_update(source, *(["--force"] if force else []))
        assert result.value.code == code
    call.assert_not_called()
    assert source.read_bytes() == old


def test_explicit_force_can_replace_known_live_drift(setup, monkeypatch):
    source, _, _ = setup
    mock_upload(monkeypatch, source, before="changed")
    assert run_update(source, "--force") == 0


@pytest.mark.parametrize(
    "state,args",
    [
        ("id: different\n  mode: published", ["--update", "example-doc", "--force"]),
        ("id: example-doc\n  mode: handed-off", ["--update", "example-doc", "--force"]),
        (
            'id: example-doc\n  mode: "handed-off"',
            ["--update", "example-doc", "--force"],
        ),
        ("id: example-doc\n  mode: mirror", ["--update", "example-doc"]),
        ("id: example-doc\n  mode: published", []),
        ("mode: handed-off", []),
    ],
)
def test_identity_and_authority_cannot_be_bypassed(setup, state, args):
    source, _, _ = setup
    source.write_text("---\ntitle: Example\ngdoc:\n  " + state + "\n---\n# Example\n")
    with pytest.raises(SystemExit) as result:
        publisher.main([str(source), "--dry-run", *args])
    assert result.value.code == 2


@pytest.mark.parametrize("after", [None, "different"])
def test_post_upload_mismatch_preserves_state_and_reports_id(
    setup, monkeypatch, capsys, after
):
    source, _, _ = setup
    old = source.read_bytes()
    call = mock_upload(monkeypatch, source)
    monkeypatch.setattr(
        publisher, "live_fingerprint", Mock(side_effect=["sha256:old", after])
    )
    with pytest.raises(SystemExit) as result:
        run_update(source)
    assert result.value.code == 3
    assert call.call_count == 1
    assert source.read_bytes() == old
    assert "example-doc" in capsys.readouterr().out


@pytest.mark.parametrize(
    "out", [{}, {"id": None}, {"id": ""}, {"id": "unexpected-doc"}, {"id": "bad\nid"}]
)
def test_malformed_upload_result_preserves_state(setup, monkeypatch, out):
    source, _, _ = setup
    old = source.read_bytes()
    call = mock_upload(monkeypatch, source)
    call.return_value = out, None
    with pytest.raises(SystemExit) as result:
        run_update(source)
    assert result.value.code == 3
    assert source.read_bytes() == old
    assert call.call_count == 1


def test_concurrent_source_edit_is_not_overwritten(setup, monkeypatch, capsys):
    source, _, _ = setup
    expected = fingerprint.fingerprint(source.read_text())
    mock_upload(monkeypatch, source)
    count = 0

    def live(*args):
        nonlocal count
        count += 1
        if count == 1:
            return "sha256:old"
        source.write_text(source.read_text() + "user edit\n")
        return expected

    monkeypatch.setattr(publisher, "live_fingerprint", live)
    with pytest.raises(SystemExit) as result:
        run_update(source)
    assert result.value.code == 2
    assert source.read_text().endswith("user edit\n")
    assert publisher.recorded_gdoc(source)["fingerprint"] == "sha256:old"
    assert "example-doc" in capsys.readouterr().out


def test_dry_run_does_not_authenticate_or_touch_source(setup):
    source, _, _ = setup
    old = source.read_bytes()
    assert run_update(source, "--dry-run") == 0
    assert source.read_bytes() == old
    publisher.open_request.assert_not_called()


def test_create_inserts_tracking_after_verified_upload(setup, monkeypatch):
    source, _, _ = setup
    source.write_text("---\ntitle: Example\n---\n# Example\nbody\n")
    monkeypatch.setattr(publisher, "access_token", Mock(return_value="synthetic"))
    monkeypatch.setattr(
        publisher,
        "live_fingerprint",
        Mock(return_value=fingerprint.fingerprint(source.read_text())),
    )
    upload = Mock(return_value=({"id": "new-example", "name": "Example"}, None))
    monkeypatch.setattr(publisher, "call", upload)
    assert publisher.main([str(source), "--folder", "example-folder"]) == 0
    assert publisher.recorded_gdoc(source)["id"] == "new-example"
    assert upload.call_args.kwargs["method"] == "POST"
    assert b"example-folder" in upload.call_args.kwargs["raw"]


@pytest.mark.parametrize(
    "scope,ok",
    [
        ("drive.readonly", False),
        ("https://www.googleapis.com/auth/drive.readonly", False),
        ("https://www.googleapis.com/auth/drive.file", True),
        ("https://www.googleapis.com/auth/drive", True),
        ("", False),
    ],
)
def test_write_scope_must_match_exactly(tmp_path, monkeypatch, scope, ok):
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "client_id": "example-client",
                "client_secret": "example-secret",
                "refresh_token": "example-refresh",
            }
        )
    )
    monkeypatch.setattr(
        publisher,
        "open_request",
        Mock(
            return_value=io.BytesIO(
                json.dumps({"scope": scope, "access_token": "example-token"}).encode()
            )
        ),
    )
    if ok:
        assert publisher.access_token(token) == "example-token"
    else:
        with pytest.raises(SystemExit) as result:
            publisher.access_token(token)
        assert result.value.code == 3


def test_api_failure_does_not_echo_response_secrets(monkeypatch):
    error = urllib.error.HTTPError(
        "https://example.invalid",
        403,
        "private-error",
        {},
        io.BytesIO(b"private response"),
    )
    monkeypatch.setattr(publisher, "open_request", Mock(side_effect=error))
    assert publisher.call("https://example.invalid", "synthetic") == (None, "http-403")


def test_redirect_is_refused_before_forwarding_credentials():
    request = publisher.urllib.request.Request(
        "https://example.invalid", headers={"Authorization": "Bearer synthetic"}
    )
    with pytest.raises(urllib.error.HTTPError):
        publisher.NoRedirect().redirect_request(
            request, None, 302, "redirect", {}, "https://other.invalid"
        )


def test_registry_round_trip_and_staleness(setup):
    source, path, _ = setup
    assert registry.main(["--config", str(path), "--write"]) == 0
    assert registry.main(["--config", str(path), "--check"]) == 0
    result = json.loads((path.parent / "registry.json").read_text())
    assert result["entries"][0]["path"] == "articles/article.md"
    assert result["entries"][0]["authority"] == "vault"
    source.write_text(source.read_text().replace("Example", "Changed"))
    assert registry.main(["--check"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "mirror-conflict",
        "bad-date",
        "bad-manifest",
        "bad-mode",
        "no-fingerprint",
    ],
)
def test_registry_failure_does_not_replace_previous_result(setup, failure):
    source, path, _ = setup
    out = path.parent / "registry.json"
    out.write_text("preserve\n")
    if failure == "duplicate":
        source.with_name("other.md").write_bytes(source.read_bytes())
    elif failure in ("mirror-conflict", "bad-manifest"):
        mirror = path.parent / "mirrors/example"
        mirror.mkdir(parents=True)
        (mirror / "manifest.yaml").write_text(
            "docId: example-doc\n" if failure == "mirror-conflict" else "docId: [\n"
        )
    elif failure == "bad-date":
        source.write_text(
            source.read_text().replace(
                "  mode:", "  published_at: 2030-01-01T00:00:00Z\n  mode:"
            )
        )
    elif failure == "bad-mode":
        source.write_text(
            source.read_text().replace("mode: published", "mode: unknown")
        )
    else:
        source.write_text(source.read_text().replace("  fingerprint: sha256:old\n", ""))
    assert registry.main(["--write"]) == 1
    assert out.read_text() == "preserve\n"


def test_registry_root_override_keeps_source_list_location(setup, tmp_path):
    _, path, value = setup
    sources = tmp_path / "sources.yaml"
    sources.write_text("docs:\n- id: mirror-doc\n")
    value["registry"]["source_lists"] = {"configured": "sources.yaml"}
    path.write_text(json.dumps(value))
    other = tmp_path / "other"
    (other / "articles").mkdir(parents=True)
    (other / "mirrors/doc").mkdir(parents=True)
    (other / "mirrors/doc/manifest.yaml").write_text(
        "docId: mirror-doc\ntitle: Example\n"
    )
    assert registry.main(["--root", str(other)]) == 0
    entry = json.loads((other / "registry.json").read_text())["entries"][0]
    assert entry["config"] == "configured"
    assert not (tmp_path / "registry.json").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("output", "../outside.json"),
        ("source_directories", ["../outside"]),
        ("mirror_directory", "../outside"),
    ],
)
def test_registry_rejects_paths_outside_selected_root(setup, key, value):
    _, path, data = setup
    data["registry"][key] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        config.load(path)


def test_configuration_is_home_portable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SELECTED_ROOT", str(tmp_path))
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": "google-docs-authority/v1",
                "write_token_file": "~/credentials/token.json",
                "registry": {
                    "repository_root": "$SELECTED_ROOT",
                    "output": "registry.json",
                },
            }
        )
    )
    result = config.load(path)
    assert result["write_token_file"] == tmp_path / "credentials/token.json"
    assert result["registry"]["repository_root"] == tmp_path


def test_fingerprint_presentation_is_ignored_but_words_remain():
    fingerprint.self_test()
    assert fingerprint.fingerprint(
        "---\ntitle: Ignored\n---\n# Example **body**"
    ) == fingerprint.fingerprint("Example body")
    assert fingerprint.fingerprint("```py\nprint(1)\n```") == fingerprint.fingerprint(
        "```python\nprint(1)\n```"
    )
    assert fingerprint.fingerprint("amount 10") != fingerprint.fingerprint("amount 11")
    assert fingerprint.canonical("[Text] stays here") == "Textstayshere"
    assert fingerprint.canonical("<5s clip") == "5sclip"


def test_skill_and_commands_are_package_local(tmp_path):
    text = (ROOT / "SKILL.md").read_text()
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert metadata["name"] == ROOT.name
    assert 1 <= len(metadata["description"]) <= 1024
    for name in ("publish", "registry", "fingerprint"):
        result = subprocess.run(
            [str(ROOT / "scripts" / name), "--help"],
            cwd=tmp_path,
            env={
                "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                "GOOGLE_DOCS_AUTHORITY_PYTHON": sys.executable,
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_inline_tracking_is_rejected_before_upload(setup):
    source, _, _ = setup
    source.write_text(
        "---\ntitle: Example\ngdoc: {id: example-doc, mode: published}\n---\n# Example\n"
    )
    with pytest.raises(SystemExit) as result:
        run_update(source, "--force")
    assert result.value.code == 2
    publisher.open_request.assert_not_called()


def test_nested_unrelated_metadata_is_preserved(setup, monkeypatch):
    source, _, _ = setup
    source.write_text(
        source.read_text().replace(
            "  mode:",
            "  extension:\n    id: keep-this-id\n    mode: keep-this-mode\n  mode:",
        )
    )
    mock_upload(monkeypatch, source)
    assert run_update(source) == 0
    assert publisher.recorded_gdoc(source)["extension"] == {
        "id": "keep-this-id",
        "mode": "keep-this-mode",
    }
