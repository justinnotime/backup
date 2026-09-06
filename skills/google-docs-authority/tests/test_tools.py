import http.server
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

from google_docs_authority import auth, export_compare, oauth, render


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GOOGLE_DOCS_AUTHORITY_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    for module in (oauth, render, export_compare):
        monkeypatch.setattr(
            module,
            "open_request",
            Mock(side_effect=AssertionError("unexpected network")),
        )
    monkeypatch.setattr(
        auth.webbrowser, "open", Mock(side_effect=AssertionError("unexpected browser"))
    )


@pytest.fixture
def credential(tmp_path):
    path = tmp_path / "credential.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "example-client",
                "client_secret": "test-client-secret",
                "refresh_token": "test-refresh",
            }
        )
    )
    return path


@pytest.fixture
def mirror(tmp_path):
    root = tmp_path / "mirror"
    directory = root / "example"
    directory.mkdir(parents=True)
    (directory / "manifest.yaml").write_text(
        'docId: example-document\ntabs:\n  - title: "Tab &amp; One"\n'
    )
    (directory / "README.md").write_text("# Tab & One\nHello world\n")
    return root


@pytest.mark.parametrize(
    "groups",
    ["drive,docs-write", "chat,docs-create", "people,drive-share", "unknown", ""],
)
def test_scope_groups_reject_invalid_or_mixed_capabilities(groups):
    with pytest.raises(ValueError):
        auth.select_scopes(groups)


@pytest.mark.parametrize("group", sorted(auth.SCOPE_GROUPS))
def test_all_existing_scope_groups_remain_available(group):
    groups, scopes = auth.select_scopes(group)
    assert groups == {group}
    assert set(scopes) == set(auth.SCOPE_GROUPS[group])


def test_write_output_cannot_alias_read_token(credential, tmp_path):
    alias = tmp_path / "alias.json"
    os.link(credential, alias)
    settings = {"read_token_file": credential}
    with pytest.raises(ValueError, match="distinct"):
        auth.output_path(settings, alias, {"docs-write"}, replace=True)
    with pytest.raises(ValueError, match="distinct"):
        auth.output_path({}, Path("./gdocs-token.json"), {"docs-write"})


def test_write_token_needs_explicit_destination():
    with pytest.raises(ValueError, match="required"):
        auth.output_path({}, None, {"docs-write"})


def test_atomic_tokens_are_private_exclusive_and_not_followed(tmp_path):
    path = tmp_path / "tokens/read.json"
    auth.write_token(path, {"refresh_token": "synthetic"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        auth.write_token(path, {"refresh_token": "replacement"})
    assert json.loads(path.read_text())["refresh_token"] == "synthetic"
    link = tmp_path / "alias.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        auth.output_path({}, link, {"drive"}, replace=True)
    assert list(path.parent.glob(".read.json*")) == []


def test_replacement_refuses_opposite_capability(credential):
    credential.write_text(
        json.dumps({"scope": "https://www.googleapis.com/auth/drive"})
    )
    with pytest.raises(ValueError, match="opposite"):
        auth.output_path({}, credential, {"drive"}, replace=True)


def test_auth_cli_uses_configured_write_file(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema": "google-docs-authority/v1",
                "read_token_file": "read.json",
                "write_token_file": "write.json",
            }
        )
    )
    client = tmp_path / "client.json"
    client.write_text(
        json.dumps({"installed": {"client_id": "example", "client_secret": "test"}})
    )
    authorize = Mock(return_value={"refresh_token": "synthetic"})
    monkeypatch.setattr(auth, "authorize", authorize)
    assert (
        auth.main([str(client), "--config", str(config), "--scopes", "docs-create"])
        == 0
    )
    assert (tmp_path / "write.json").exists()
    assert not (tmp_path / "read.json").exists()
    assert (
        auth.main([str(client), "--config", str(config), "--scopes", "docs-create"])
        == 3
    )
    assert authorize.call_count == 1


def test_manual_consent_validates_state_and_uses_pkce(monkeypatch, capsys):
    def paste(_):
        output = capsys.readouterr().out
        url = next(line for line in output.splitlines() if line.startswith("https://"))
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert params["code_challenge_method"] == ["S256"]
        return (
            params["redirect_uri"][0]
            + "/?"
            + urllib.parse.urlencode(
                {"code": "synthetic-code", "state": params["state"][0]}
            )
        )

    monkeypatch.setattr("builtins.input", paste)
    exchange = Mock(return_value={"refresh_token": "synthetic-refresh"})
    monkeypatch.setattr(auth, "exchange", exchange)
    result = auth.authorize(
        {"client_id": "example", "client_secret": "test"},
        auth.SCOPE_GROUPS["drive"],
        manual=True,
    )
    assert result["scope"] == auth.SCOPE_GROUPS["drive"][0]
    assert len(exchange.call_args.args[0]["code_verifier"]) >= 43
    assert exchange.call_args.args[0]["code"] == "synthetic-code"
    auth.webbrowser.open.assert_not_called()


@pytest.mark.parametrize(
    "callback",
    [
        "http://127.0.0.1:1234/?code=one",
        "http://127.0.0.1:1234/?code=one&state=wrong",
        "http://example.invalid/?code=one&state=expected",
        "http://127.0.0.1:1234/?code=one&code=two&state=expected",
        "plain-code",
    ],
)
def test_invalid_callbacks_are_rejected(callback):
    with pytest.raises(ValueError):
        auth.code_from_callback(callback, "expected", "http://127.0.0.1:1234")


def test_configured_missing_file_does_not_silently_use_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_DOCS_AUTHORITY_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(OSError):
        auth.settings_for()


def test_refresh_validates_credentials_and_scope(credential, monkeypatch):
    monkeypatch.setattr(
        oauth,
        "exchange",
        Mock(
            return_value={
                "access_token": "synthetic",
                "scope": "https://www.googleapis.com/auth/drive.readonly",
            }
        ),
    )
    assert (
        oauth.refresh_access_token(credential, forbidden_scopes=auth.WRITE_SCOPES)
        == "synthetic"
    )
    with pytest.raises(oauth.OAuthError, match="required-scope"):
        oauth.refresh_access_token(credential, required_any_scopes=auth.WRITE_SCOPES)
    oauth.exchange.return_value["scope"] = "https://www.googleapis.com/auth/drive"
    with pytest.raises(oauth.OAuthError, match="write-scope"):
        oauth.refresh_access_token(credential, forbidden_scopes=auth.WRITE_SCOPES)
    credential.write_text("[]")
    with pytest.raises(oauth.OAuthError, match="token-file"):
        oauth.refresh_access_token(credential)


def test_http_diagnostics_never_include_response_secrets(monkeypatch, tmp_path):
    body = b"private document and secret material"
    for module in (oauth, render, export_compare):
        failure = urllib.error.HTTPError(
            "https://example.invalid/secret",
            403,
            "sensitive message",
            {},
            io.BytesIO(body),
        )
        monkeypatch.setattr(module, "open_request", Mock(side_effect=failure))
    with pytest.raises(oauth.OAuthError) as result:
        oauth.exchange({"refresh_token": "test"})
    assert str(result.value) == "token-exchange-http-403"
    with pytest.raises(oauth.OAuthError) as result:
        render.export_pdf("example-document", "test", tmp_path / "doc.pdf")
    assert str(result.value) == "pdf-export-http-403"
    assert export_compare.export("example-document", "text/markdown", "test") == (
        None,
        "export-http-403",
    )


def test_redirect_handler_refuses_credential_forwarding():
    request = urllib.request.Request(
        "https://www.googleapis.com/export",
        headers={"Authorization": "Bearer synthetic"},
    )
    with pytest.raises(urllib.error.HTTPError, match="redirect-refused"):
        oauth.NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://other.invalid/capture"
        )


def test_real_http_redirect_does_not_reach_target():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/capture"
                )
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/start",
            headers={"Authorization": "Bearer synthetic"},
        )
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.build_opener(oauth.NoRedirect()).open(request, timeout=2)
        assert failure.value.code == 302
        assert requests == ["/start"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize(
    "value",
    [
        "../secrets",
        "example?token=leak",
        "https://other.invalid/document/d/example",
        "https://docs.google.com@other.invalid/document/d/example",
        "https://docs.google.com:4430/document/d/example",
        "https://docs.google.com/document/d/../example",
        "example\n",
    ],
)
def test_document_ids_reject_paths_and_foreign_urls(value):
    with pytest.raises(ValueError):
        render.doc_id(value)


def test_document_urls_and_ids():
    assert render.doc_id("example-document_1") == "example-document_1"
    assert (
        render.doc_id(
            "https://docs.google.com/document/d/example-document/edit?tab=t.1"
        )
        == "example-document"
    )
    assert (
        render.doc_id("https://docs.google.com/document/u/2/d/example-document/edit")
        == "example-document"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--pages", "0"],
        ["--pages", "3-1"],
        ["--pages", "one"],
        ["--dpi", "0"],
        ["--dpi", "9999999"],
    ],
)
def test_render_invalid_ranges_fail_before_network(arguments, credential):
    assert (
        render.main(["example-document", "--token", str(credential), *arguments]) == 2
    )
    render.open_request.assert_not_called()
    oauth.open_request.assert_not_called()


def mock_render(monkeypatch, *, fail=False, empty=False):
    monkeypatch.setattr(render, "access_token", Mock(return_value="synthetic"))
    monkeypatch.setattr(
        render, "export_pdf", lambda doc, token, path: path.write_bytes(b"%PDF-1.4\n")
    )

    def run(argv, **kwargs):
        stem = Path(argv[-1])
        if not fail and not empty:
            stem.with_name(stem.name + "-1.png").write_bytes(
                b"\x89PNG\r\n\x1a\nsynthetic"
            )
        return subprocess.CompletedProcess(
            argv, 1 if fail else 0, b"", b"sensitive source text"
        )

    monkeypatch.setattr(render.subprocess, "run", run)


@pytest.mark.parametrize("fail,empty", [(True, False), (False, True)])
def test_failed_render_never_reports_old_pages(
    tmp_path, monkeypatch, capsys, fail, empty
):
    out = tmp_path / "out"
    out.mkdir()
    old = out / "page-99.png"
    old.write_bytes(b"old")
    mock_render(monkeypatch, fail=fail, empty=empty)
    assert (
        render.main(
            [
                "example",
                "--token",
                "token",
                "--out",
                str(out),
                "--pdftoppm-command",
                json.dumps([sys.executable]),
            ]
        )
        == 4
    )
    output = capsys.readouterr()
    assert "page-99" not in output.out
    assert "sensitive source text" not in output.err
    assert old.read_bytes() == b"old"
    assert list(out.glob(".render-*")) == []


def test_successful_render_replaces_only_owned_output(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "page-99.png").write_bytes(b"old")
    (out / "page-not-owned.png").write_bytes(b"preserve")
    mock_render(monkeypatch)
    assert (
        render.main(
            [
                "example",
                "--token",
                "token",
                "--out",
                str(out),
                "--pdftoppm-command",
                json.dumps([sys.executable]),
            ]
        )
        == 0
    )
    assert not (out / "page-99.png").exists()
    assert (out / "page-1.png").read_bytes().startswith(b"\x89PNG")
    assert (out / "page-not-owned.png").read_bytes() == b"preserve"
    assert (out / "doc.pdf").read_bytes().startswith(b"%PDF-")


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler-utils required")
def test_real_rasterizer_renders_synthetic_pdf(tmp_path, monkeypatch):
    stream = b"BT /F1 16 Tf 20 100 Td (Synthetic example) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n"
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    monkeypatch.setattr(render, "access_token", Mock(return_value="synthetic"))
    monkeypatch.setattr(
        render, "export_pdf", lambda doc, token, path: path.write_bytes(pdf)
    )
    out = tmp_path / "images"
    assert (
        render.main(
            [
                "example",
                "--token",
                "synthetic.json",
                "--pages",
                "1",
                "--dpi",
                "72",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    image = (out / "page-1.png").read_bytes()
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", image[16:24]) == (200, 200)


def test_render_refuses_to_overwrite_credentials(tmp_path, monkeypatch):
    credential = tmp_path / "out/doc.pdf"
    credential.parent.mkdir()
    credential.write_text("credential")
    assert (
        render.main(
            [
                "example",
                "--token",
                str(credential),
                "--out",
                str(credential.parent),
                "--pdftoppm-command",
                json.dumps([sys.executable]),
            ]
        )
        == 2
    )
    assert credential.read_text() == "credential"
    oauth.open_request.assert_not_called()


def test_pdf_export_rejects_non_pdf_content(tmp_path, monkeypatch):
    monkeypatch.setattr(
        render, "open_request", Mock(return_value=io.BytesIO(b"HTML error"))
    )
    with pytest.raises(oauth.OAuthError, match="pdf-export-invalid"):
        render.export_pdf("example", "test", tmp_path / "doc.pdf")
    assert not (tmp_path / "doc.pdf").exists()


def test_comparison_metrics_and_safe_divergence():
    text = "# Example\n| a | b |\n| --- | --- |\n| 1 | 2 |\n![image](example.png)\n<img src='example.png'>\n"
    measured = export_compare.measure(text)
    assert measured["images"] == 2
    assert measured["table_rules"] == 1
    assert measured["table_rows"] == 2
    assert "SECRET" not in export_compare.first_divergence("SECRET", "other")
    assert "SECRET" in export_compare.first_divergence(
        "SECRET", "other", include_content=True
    )
    assert export_compare.tab_titles('tabs:\n  - title: "A &amp; B"\n') == ["A & B"]
    assert export_compare.tab_coverage("# A & B", ["A & B"]) == (1, 1)


def comparison_setup(monkeypatch, *, refused=False, bad_conversion=False):
    monkeypatch.setattr(export_compare, "access_token", Mock(return_value="synthetic"))

    def export(ident, mime, token):
        if refused and mime == "text/markdown":
            return None, "export-http-403"
        return b"# Tab & One\nHello world\n", None

    monkeypatch.setattr(export_compare, "export", export)
    monkeypatch.setattr(
        export_compare.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                1 if bad_conversion else 0,
                b"# Tab & One\nHello world\n",
                b"private text",
            )
        ),
    )


def test_complete_comparison_and_json_omit_full_document_text(
    mirror, credential, tmp_path, monkeypatch
):
    comparison_setup(monkeypatch)
    output = tmp_path / "report.json"
    keep = tmp_path / "exports"
    assert (
        export_compare.main(
            [
                "--mirror-directory",
                str(mirror),
                "--token",
                str(credential),
                "--json",
                str(output),
                "--keep",
                str(keep),
            ]
        )
        == 0
    )
    records = json.loads(output.read_text())
    assert records[0]["fp_vs_repo"] == "same"
    assert records[0]["tabs_in_native"] == 1
    assert records[0]["native"]["fingerprint"].startswith("sha256:")
    assert "Hello" not in output.read_text()
    assert "Hello" in (keep / "example.native.md").read_text()


@pytest.mark.parametrize("refused,bad_conversion", [(True, False), (False, True)])
def test_partial_comparison_failure_is_preserved_and_exits_nonzero(
    mirror, credential, tmp_path, monkeypatch, refused, bad_conversion
):
    comparison_setup(monkeypatch, refused=refused, bad_conversion=bad_conversion)
    output = tmp_path / "report.json"
    assert (
        export_compare.main(
            [
                "--mirror-directory",
                str(mirror),
                "--token",
                str(credential),
                "--json",
                str(output),
            ]
        )
        == 3
    )
    records = json.loads(output.read_text())
    assert records[0]["errors"]
    assert "private text" not in output.read_text()


def test_bad_manifest_does_not_silently_disappear(
    mirror, credential, tmp_path, monkeypatch
):
    comparison_setup(monkeypatch)
    (mirror / "bad").mkdir()
    (mirror / "bad/manifest.yaml").write_text("docId: ../../outside\n")
    output = tmp_path / "report.json"
    assert (
        export_compare.main(
            [
                "--mirror-directory",
                str(mirror),
                "--token",
                str(credential),
                "--json",
                str(output),
            ]
        )
        == 3
    )
    assert len(json.loads(output.read_text())) == 2


def test_comparison_refuses_output_inside_mirror(mirror, credential):
    assert (
        export_compare.main(
            [
                "--mirror-directory",
                str(mirror),
                "--token",
                str(credential),
                "--json",
                str(mirror / "example/README.md"),
            ]
        )
        == 2
    )
    assert (mirror / "example/README.md").read_text().startswith("# Tab")
    oauth.open_request.assert_not_called()


def test_tabbed_comparison_reads_ordered_nested_content_not_index(
    mirror, credential, tmp_path, monkeypatch
):
    comparison_setup(monkeypatch)
    directory = mirror / "example"
    (directory / "tabs/nested").mkdir(parents=True)
    (directory / "manifest.yaml").write_text(
        'docId: example-document\nlayout: tabs\ntabs:\n  - title: "Tab & One"\n    path: tabs/first.md\n  - title: "Nested"\n    path: tabs/nested/second.md\n'
    )
    (directory / "README.md").write_text("# Index only\n[First](tabs/first.md)\n")
    (directory / "tabs/first.md").write_text(
        "<!-- mechanical header -->\n# Tab & One\nHello "
    )
    (directory / "tabs/nested/second.md").write_text(
        "<!-- mechanical header -->\nworld\n"
    )
    output = tmp_path / "report.json"
    assert (
        export_compare.main(
            [
                "--mirror-directory",
                str(mirror),
                "--token",
                str(credential),
                "--json",
                str(output),
            ]
        )
        == 0
    )
    record = json.loads(output.read_text())[0]
    assert record["fp_vs_repo"] == "same"
    assert record["committed"]["fingerprint"] == record["native"]["fingerprint"]


@pytest.mark.parametrize(
    "relative",
    ["../outside.md", "/outside.md", "missing.md", "alias.md", "linked/other.md"],
)
def test_tabbed_comparison_refuses_missing_escaping_or_symlink_content(
    mirror, tmp_path, relative
):
    directory = mirror / "example"
    outside = tmp_path / "outside.md"
    outside.write_text("private source")
    (directory / "alias.md").symlink_to(outside)
    (directory / "linked").symlink_to(tmp_path, target_is_directory=True)
    (directory / "manifest.yaml").write_text(
        f'docId: example-document\nlayout: tabs\ntabs:\n  - title: Example\n    path: "{relative}"\n'
    )
    with pytest.raises((ValueError, OSError)):
        export_compare.read_manifest(directory, mirror)


def test_comparison_uses_configured_paths(mirror, credential, tmp_path, monkeypatch):
    comparison_setup(monkeypatch)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema": "google-docs-authority/v1",
                "read_token_file": str(credential),
                "registry": {
                    "repository_root": str(tmp_path),
                    "output": "registry.json",
                    "mirror_directory": str(mirror),
                },
            }
        )
    )
    assert export_compare.main(["--config", str(config)]) == 0
    export_compare.access_token.assert_called_once_with(credential)


@pytest.mark.parametrize("script", ["auth", "render", "compare-exports"])
def test_public_scripts_help_without_account_or_config(script, tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "GOOGLE_DOCS_AUTHORITY_PYTHON": sys.executable,
        "HOME": str(tmp_path),
    }
    process = subprocess.run(
        [str(root / "scripts" / script), "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert process.returncode == 0, process.stderr
    assert "--config" in process.stdout
