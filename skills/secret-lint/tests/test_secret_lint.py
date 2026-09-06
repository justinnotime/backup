from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("secret_lint", PACKAGE / "src/secret_lint.py")
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def token(prefix="sk-"):
    return prefix + "syntheticOnlyForTests" * 3


def cli(*args, input=None, package=PACKAGE, env=None):
    return subprocess.run(
        [sys.executable, "-B", str(package / "src/secret_lint.py"), *map(str, args)],
        input=input,
        capture_output=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize(
    "prefix,category",
    [
        ("sk-", "openai-anthropic-key"),
        ("ghp_", "github-token"),
        ("xoxb-", "slack-token"),
        ("gsk-", "provider-api-key"),
        ("glpat-", "provider-api-key"),
        ("ya29.", "provider-api-key"),
    ],
)
def test_provider_shapes_and_mask_idempotency(prefix, category):
    secret = token(prefix)
    findings = scan.scan_text("value: " + secret)
    assert any(f[1] == category for f in findings)
    masked, count = scan.mask_text("value: " + secret, {"hard"})
    assert count > 0 and secret not in masked
    assert len(masked) == len("value: " + secret)
    assert scan.mask_text(masked, {"hard"})[0] == masked


@pytest.mark.parametrize(
    "text",
    [
        "password: <your-password>",
        "token: ${ACCESS_TOKEN}",
        "secret: REDACTED",
        "client_secret: YOUR_SECRET_HERE",
        "ordinary-product-name-and-description",
        "https://example.invalid/docs/ordinary-long-document-path",
        "12345678-1234-4234-9234-123456789abc",
    ],
)
def test_obvious_placeholders_and_identifiers_are_not_candidates(text):
    assert scan.scan_text(text) == []
    assert scan.mask_text(text, {"hard", "ctx", "heur"}) == (text, 0)


def test_authorization_and_cookies_are_masked():
    secret = "opaqueSyntheticCredential123"
    raw = f"Authorization: Bearer {secret}\nCookie: session={secret}; SameSite=Lax\n"
    categories = {f[1] for f in scan.scan_text(raw)}
    assert {"authorization-token", "session-cookie"} <= categories
    masked, _ = scan.mask_text(raw, {"hard"})
    assert secret not in masked
    assert "SameSite=Lax" in masked
    assert "Authorization: Bearer" in masked


def test_inline_image_does_not_hide_unrelated_secret():
    raw = "![picture](data:image/png;base64,c3ludGhldGlj) key=" + token()
    masked, _ = scan.mask_text(raw, {"hard"})
    assert "data:image/png;base64,c3ludGhldGlj" in masked
    assert token() not in masked


@pytest.mark.parametrize("kind", ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY"])
def test_private_key_blocks_keep_delimiters_and_hide_body(kind):
    body = "syntheticBodyNeverARealKey" * 4
    raw = f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----\n"
    masked, count = scan.mask_text(raw, {"hard"})
    assert count >= 1
    assert body not in masked
    assert len(masked) == len(raw)
    assert "-----BEGIN " + kind in masked
    assert scan.mask_text(masked, {"hard"})[0] == masked


def test_known_value_is_redacted_at_unlabeled_occurrences():
    secret = "synthetic~Credential~123"
    raw = f"client_secret: '{secret}'\nvalue: '{secret}'\n"
    masked, _ = scan.mask_text(raw, {"hard"})
    assert secret not in masked


def test_url_credentials_and_query_parameters_keep_structure():
    secret = "syntheticPassword123"
    raw = f"https://reader:{secret}@example.invalid/file?token={secret}ABCDEF&version=2026\n"
    masked, _ = scan.mask_text(raw, {"hard"})
    assert secret not in masked
    assert "https://reader:" in masked
    assert "@example.invalid/file?token=" in masked
    assert "version=2026" in masked


def test_percent_encoded_query_credential_masks_the_original_bytes():
    encoded = "synthetic%2BCredential%2BForTests"
    text = f"https://example.invalid/file?token={encoded}&version=1"
    masked, count = scan.mask_text(text, {"hard"})
    assert count > 0
    assert encoded not in masked
    assert "&version=1" in masked


def test_json_authorization_basic_and_cookie_headers():
    value = "opaqueSynthetic123"
    text = json.dumps({"Authorization": "Basic " + value, "Cookie": "session=" + value})
    masked, count = scan.mask_text(text, {"hard"})
    assert count > 0
    assert value not in masked


def test_repository_scan_includes_hidden_untracked_and_non_markdown(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config/settings.json").write_text(json.dumps({"token": token()}))
    (tmp_path / "script.py").write_text("KEY = '" + token("gsk-") + "'\n")
    (tmp_path / ".git/objects").mkdir(parents=True)
    (tmp_path / ".git/objects/object").write_text(token("ghp_"))
    report = tmp_path.parent / "report.json"
    result = cli("check", tmp_path, "--json", "--output", report)
    assert result.returncode == 1
    parsed = json.loads(result.stdout)
    assert parsed["scanned_files"] == 2
    assert len(parsed["findings"]) >= 2
    assert not parsed["incomplete"]
    assert token().encode() not in result.stdout + result.stderr + report.read_bytes()
    assert token("gsk-").encode() not in result.stdout + result.stderr + report.read_bytes()
    assert all(set(f) == {"file", "line", "category", "tier"} for f in parsed["findings"])


def test_report_alias_does_not_create_old_secret_value_tsvs(tmp_path):
    source = tmp_path / "page.md"
    source.write_text(token())
    result = cli("report", source)
    assert result.returncode == 1
    assert token().encode() not in result.stdout + result.stderr
    assert not (tmp_path / "secret-scan-report.tsv").exists()
    assert not (tmp_path / "secret-scan-unique.tsv").exists()


def test_secret_shaped_filename_is_redacted_in_diagnostics(tmp_path):
    source = tmp_path / (token() + ".txt")
    source.write_text(token("ghp_"))
    result = cli("check", source, "--json")
    assert result.returncode == 1
    assert token().encode() not in result.stdout + result.stderr
    assert token("ghp_").encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["missing", "binary", "encoding", "symlink"])
def test_incomplete_input_returns_two_and_identifies_limit(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "binary":
        path.write_bytes(b"binary\0" + token().encode())
    elif kind == "encoding":
        path.write_bytes(b"\xff\xfe")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text(token())
        path.symlink_to(target)
    result = cli("check", path, "--json")
    assert result.returncode == 2
    assert json.loads(result.stdout)["incomplete"]
    assert token().encode() not in result.stdout + result.stderr


def test_clean_scan_and_duplicate_explicit_inputs(tmp_path):
    source = tmp_path / "page.md"
    source.write_text("A public example document.\n")
    result = cli("check", source, source, "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["scanned_files"] == 1


def test_unreadable_file_is_not_clean(tmp_path, monkeypatch):
    path = tmp_path / "blocked.txt"
    path.write_text("content")

    def unavailable(_path):
        raise PermissionError("private error context")

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    report = scan.inspect_inputs([path], {"hard"})
    assert report["scanned_files"] == 0
    assert report["incomplete"][0]["reason"] == "file unreadable"
    assert "private error context" not in json.dumps(report)


def test_failed_report_write_never_echoes_matched_values(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text(token())
    result = cli("check", source, "--output", tmp_path)
    assert result.returncode == 2
    assert token().encode() not in result.stdout + result.stderr


def test_special_file_is_reported_without_reading(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    report = scan.inspect_inputs([path], {"hard"})
    assert report["incomplete"] and report["scanned_files"] == 0


def test_mask_default_creates_private_copy_and_keeps_source(tmp_path):
    source = tmp_path / "input.md"
    source.write_text(token())
    result = cli("mask", source)
    assert result.returncode == 0
    assert source.read_text() == token()
    assert token() not in source.with_suffix(".md.masked").read_text()
    assert source.with_suffix(".md.masked").stat().st_mode & 0o777 == 0o600
    assert token().encode() not in result.stdout + result.stderr


def test_mask_in_place_preserves_mode_and_is_idempotent(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text(token())
    source.chmod(0o640)
    result = cli("mask", source, "--in-place", "--tier", "hard")
    assert result.returncode == 0
    first = source.read_bytes()
    assert token().encode() not in first
    assert source.stat().st_mode & 0o777 == 0o640
    assert cli("mask", source, "--in-place").returncode == 0
    assert source.read_bytes() == first


def test_mask_preserves_utf8_bom_and_line_endings(tmp_path):
    source = tmp_path / "input.md"
    source.write_bytes(b"\xef\xbb\xbf" + token().encode() + b"\r\n")
    assert cli("mask", source).returncode == 0
    output = source.with_suffix(".md.masked").read_bytes()
    assert output.startswith(b"\xef\xbb\xbf") and output.endswith(b"\r\n")


def test_directory_mask_keeps_explicit_markdown_scope(tmp_path):
    (tmp_path / "document.md").write_text(token())
    (tmp_path / "configuration.json").write_text(token())
    assert cli("mask", tmp_path).returncode == 0
    assert (tmp_path / "document.md.masked").exists()
    assert not (tmp_path / "configuration.json.masked").exists()


def test_stdin_redaction_returns_only_content_or_explicit_json():
    raw = ("hello\npassword: " + token() + "\n").encode()
    result = cli("redact", "--tier", "hard,ctx", input=raw)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout.startswith(b"hello\npassword: ")
    assert token().encode() not in result.stdout
    result = cli("redact", "--json", input=raw)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout)["replacements"] > 0
    assert token().encode() not in result.stdout


def test_stdin_invalid_encoding_never_echoes_input():
    result = cli("redact", input=b"\xff" + token().encode())
    assert result.returncode == 2 and not result.stdout
    assert token().encode() not in result.stderr


def test_unknown_tier_cannot_silently_disable_redaction():
    assert cli("redact", "--tier", "hrd", input=token().encode()).returncode == 2
    with pytest.raises(ValueError):
        scan.mask_text(token(), {"hrd"})


def test_package_copy_runs_without_siblings_or_private_configuration(tmp_path):
    copied = tmp_path / "package"
    shutil.copytree(
        PACKAGE,
        copied,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache"),
    )
    env = dict(
        os.environ,
        HOME=str(tmp_path / "empty-home"),
        XDG_CONFIG_HOME=str(tmp_path / "empty-config"),
    )
    result = cli("redact", input=token().encode(), package=copied, env=env)
    assert result.returncode == 0
    assert token().encode() not in result.stdout + result.stderr
