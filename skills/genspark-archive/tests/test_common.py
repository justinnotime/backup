import json
import sys

import pytest

from genspark_archive.common import (
    ArchiveError,
    Client,
    load_config,
    output_path,
    read_state,
    write_state,
    write_text,
)


def configuration(tmp_path, **overrides):
    root = tmp_path / "repository"
    root.mkdir(exist_ok=True)
    data = {
        "schema": "genspark-archive/v1",
        "repository_root": str(root),
        "rate_delay": 0,
        "emails": {
            "account": "reader@example.invalid",
            "output_directory": "archive/email",
            "state_file": str(tmp_path / "state.json"),
        },
    }
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_home_and_transaction_paths_preserve_external_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = configuration(tmp_path, repository_root="~/repository", command=["$HOME/bin/gsk"])
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    settings = load_config(path, "emails", root=transaction)
    assert settings.output_directory == transaction / "archive/email"
    assert settings.state_file == tmp_path / "state.json"
    assert settings.command == (str(tmp_path / "bin/gsk"),)


@pytest.mark.parametrize("target", ["config.json", "repository/state.json"])
def test_cli_state_cannot_overwrite_config_or_repository(tmp_path, target):
    path = configuration(tmp_path)
    with pytest.raises(ArchiveError):
        load_config(path, "emails", state_file=tmp_path / target)


def test_configured_output_cannot_follow_escape_symlink(tmp_path):
    path = configuration(tmp_path)
    (tmp_path / "repository/archive").symlink_to(tmp_path)
    with pytest.raises(ArchiveError):
        load_config(path, "emails")


def test_output_file_cannot_escape_or_follow_symlink(tmp_path):
    settings = load_config(configuration(tmp_path), "emails")
    settings.output_directory.mkdir(parents=True)
    (settings.output_directory / "link.md").symlink_to(tmp_path / "outside.md")
    for name in ["../outside.md", "/tmp/outside.md", "link.md"]:
        with pytest.raises(ArchiveError):
            output_path(settings, name)


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout": 0},
        {"rate_delay": -1},
        {"timeout": float("nan")},
        {"command": []},
        {"command": "gsk"},
        {"schema": "other/v1"},
        {"emails": {"output_directory": "archive"}},
    ],
)
def test_invalid_configuration_fails(tmp_path, overrides):
    with pytest.raises(ArchiveError):
        load_config(configuration(tmp_path, **overrides), "emails")


@pytest.mark.parametrize(
    "program",
    [
        "print('SENSITIVE response'); raise SystemExit(2)",
        "print('SENSITIVE invalid JSON')",
        'print(\'{"error": "SENSITIVE service error"}\')',
        'print(\'{"success": false, "message": "SENSITIVE"}\')',
        'print(\'{"status": "failed", "message": "SENSITIVE"}\')',
    ],
)
def test_real_command_failure_is_sanitized(tmp_path, program):
    settings = load_config(
        configuration(tmp_path, command=[sys.executable, "-c", program]), "emails"
    )
    with pytest.raises(ArchiveError) as error:
        Client(settings).call([])
    assert "SENSITIVE" not in str(error.value)


def test_real_cli_banner_and_json_are_supported(tmp_path):
    settings = load_config(
        configuration(
            tmp_path,
            command=[
                sys.executable,
                "-c",
                "print('[INFO] ready'); print('{\"session_state\": {\"emails\": []}}')",
            ],
        ),
        "emails",
    )
    assert Client(settings).call([]) == {"session_state": {"emails": []}}


def test_missing_executable_has_sanitized_diagnostic(tmp_path):
    settings = load_config(configuration(tmp_path, command=[str(tmp_path / "missing")]), "emails")
    with pytest.raises(ArchiveError, match="could not complete"):
        Client(settings).call([])


def test_state_keeps_legacy_fields_and_sorts_ids(tmp_path):
    path = tmp_path / "progress/state.json"
    assert read_state(path) == {"synced_ids": []}
    write_state(path, {"synced_ids": ["b", "a", "a"], "last_after": "2026-01-01"})
    assert read_state(path) == {"synced_ids": ["a", "b"], "last_after": "2026-01-01"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_existing_state_is_not_reset(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"synced_ids": "invalid"}')
    with pytest.raises(ArchiveError):
        read_state(path)
    assert path.read_text() == '{"synced_ids": "invalid"}'


def test_atomic_write_refuses_symlink(tmp_path):
    target = tmp_path / "existing.md"
    target.write_text("original")
    link = tmp_path / "linked.md"
    link.symlink_to(target)
    with pytest.raises(ArchiveError):
        write_text(link, "replacement")
    assert target.read_text() == "original"
