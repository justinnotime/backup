import json
import subprocess
from pathlib import Path

import pytest

from runtime_layout import Layout
from runtime_layout.shell import emit


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = tmp_path / "reader home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SAMPLE_ROOT", raising=False)
    monkeypatch.delenv("SAMPLE_TOKEN", raising=False)
    config = {
        "schema": "runtime-layout/v1",
        "root": {"default": "~/.sample-runtime", "environment": "SAMPLE_ROOT"},
        "repository": {"environment": "SAMPLE_REPOSITORY", "branch": "main"},
        "paths": {
            "root": {"kind": "root"},
            "active": {"kind": "active_flag"},
            "repository": {"kind": "repository"},
            "packages": {"kind": "sibling", "name": "packages"},
            "token": {
                "kind": "file",
                "path": "{root}/credentials/read",
                "legacy": ["~/.sample-old/read"],
                "environment": ["SAMPLE_TOKEN"],
            },
            "write_token": {
                "kind": "file",
                "path": "{root}/credentials/write",
                "legacy": ["~/.sample-old/write"],
            },
            "lock": {
                "kind": "active",
                "path": "{root}/locks/{0}",
                "legacy": ["~/.sample-old/{0}.lock"],
                "arguments": 1,
            },
            "progress": {
                "kind": "content",
                "path": "{root}/state/{0}",
                "legacy": ["~/.sample-old/{0}"],
                "arguments": 1,
            },
            "tokens": {
                "kind": "glob",
                "path": "{root}/credentials",
                "legacy": ["~/.sample-old"],
                "pattern": "key-*",
            },
            "alias": {"kind": "alias", "target": "token"},
        },
        "shell_functions": {
            "sample_root": "root",
            "sample_token": "token",
            "sample_lock": "lock",
            "sample_progress": "progress",
            "sample_tokens": "tokens",
            "sample_repository": "repository",
            "sample_packages": "packages",
        },
    }
    return home, config, Layout(config, repository_source=tmp_path)


def shell(layout, commands, env=None):
    return subprocess.run(
        ["bash", "-eu", "-c", emit(layout) + commands], env=env, capture_output=True, text=True
    )


def test_file_priority_distinct_credentials_and_no_creation(case):
    home, _, layout = case
    assert layout.resolve("token") == home / ".sample-runtime/credentials/read"
    assert not (home / ".sample-runtime").exists()
    old = home / ".sample-old/read"
    old.parent.mkdir()
    old.write_text("synthetic")
    assert layout.resolve("token") == old
    assert layout.resolve("write_token") != old
    new = home / ".sample-runtime/credentials/read"
    new.parent.mkdir(parents=True)
    new.write_text("new synthetic")
    assert layout.resolve("token") == new
    assert layout.resolve("alias") == new


def test_environment_override_remains_dynamic(case, monkeypatch):
    home, _, layout = case
    monkeypatch.setenv("SAMPLE_TOKEN", "~/selected")
    assert layout.resolve("token") == home / "selected"
    code = 'sample_root; SAMPLE_ROOT="$HOME/another"; sample_root; sample_lock writer\n'
    result = shell(layout, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(home / ".sample-runtime"),
        str(home / "another"),
        str(home / "another/locks/writer"),
    ]


def test_empty_environment_semantics_are_explicit(case, monkeypatch):
    home, _, layout = case
    monkeypatch.setenv("SAMPLE_ROOT", "")
    assert layout.active() is True
    assert layout.root() == Path(".")
    result = shell(layout, "sample_root; sample_lock writer\n")
    assert result.stdout.splitlines() == [
        str(home / ".sample-runtime"),
        str(home / ".sample-old/writer.lock"),
    ]


def test_root_activation_and_content_preference_match_shell(case):
    home, _, layout = case
    old = home / ".sample-old/archive"
    old.mkdir(parents=True)
    (old / "progress").write_text("watermark")
    for active in (False, True):
        if active:
            layout.root().mkdir()
        result = shell(layout, "sample_lock writer; sample_progress archive\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            str(layout.resolve("lock", "writer")),
            str(layout.resolve("progress", "archive")),
        ]
    new = layout.root() / "state/archive"
    new.mkdir(parents=True)
    (new / "progress").write_text("watermark")
    assert layout.resolve("progress", "archive") == new


def test_glob_and_legacy_notice(case, capsys):
    home, _, layout = case
    old = home / ".sample-old"
    old.mkdir()
    (old / "key-a").write_text("synthetic")
    assert layout.resolve("tokens") == old
    assert layout.resolve("tokens") == old
    assert capsys.readouterr().err.count("NOTE") == 1
    result = shell(layout, "sample_tokens\n")
    assert result.stdout.strip() == str(old)
    assert not layout.root().exists()


def test_generated_shell_quotes_policy_values(case, tmp_path):
    _, config, _ = case
    injected = tmp_path / "must-not-exist"
    text = f"literal $(touch {injected}) ' ; test"
    config["paths"]["literal"] = {"kind": "fixed", "path": str(tmp_path / text)}
    config["shell_functions"]["sample_literal"] = "literal"
    layout = Layout(config, repository_source=tmp_path)
    result = shell(layout, "sample_literal\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / text)
    assert not injected.exists()


def test_main_checkout_and_override_follow_git_metadata(case, tmp_path, monkeypatch):
    _, config, _ = case
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    main = tmp_path / "project"
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=writer@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "project-task"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "task", str(work)],
        check=True,
        capture_output=True,
    )
    layout = Layout(config, repository_source=work)
    assert layout.repository() == main
    result = shell(layout, "sample_repository; sample_packages\n")
    assert result.stdout.splitlines() == [str(main), str(tmp_path / "packages")]
    monkeypatch.setenv("SAMPLE_REPOSITORY", str(tmp_path / "selected"))
    assert layout.repository() == tmp_path / "selected"
    assert shell(layout, "sample_repository\n").stdout.strip() == str(tmp_path / "selected")


def test_shell_queries_do_not_spawn_python_or_git_again(case, monkeypatch):
    _, _, layout = case
    code = emit(layout)
    code += "\nPATH=/nonexistent\nsample_root\nsample_lock writer\nsample_repository\n"
    result = subprocess.run(["/bin/bash", "-eu", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bad_argument_count_fails_python_and_shell(case):
    _, _, layout = case
    with pytest.raises(ValueError):
        layout.resolve("lock")
    assert shell(layout, "sample_lock\n").returncode == 2


def test_cli_is_read_only(case, tmp_path):
    home, config, _ = case
    selected = tmp_path / "config.json"
    selected.write_text(json.dumps(config))
    script = Path(__file__).resolve().parents[1] / "scripts/paths"
    result = subprocess.run(
        [str(script), "--config", str(selected), "token"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert not list(home.iterdir())


@pytest.mark.parametrize("value", [-1, True, "0; touch unwanted", 1.5])
def test_invalid_argument_count_cannot_enter_generated_shell(case, value):
    _, config, _ = case
    config["paths"]["lock"]["arguments"] = value
    with pytest.raises(ValueError, match="nonnegative integer"):
        emit(Layout(config, repository_source=Path.cwd()))


@pytest.mark.parametrize("target", ["missing", "root; touch unwanted"])
def test_unknown_alias_cannot_enter_generated_shell(case, target):
    _, config, _ = case
    config["paths"]["alias"]["target"] = target
    with pytest.raises(ValueError, match="alias"):
        emit(Layout(config, repository_source=Path.cwd()))


def test_configured_home_syntax_is_shared_and_other_variables_are_literal(case, monkeypatch):
    home, config, _ = case
    monkeypatch.setenv("SOME_PRIVATE_VARIABLE", "must-not-be-interpolated")
    config["paths"]["value"] = {"kind": "fixed", "path": "${HOME}/$SOME_PRIVATE_VARIABLE/value"}
    config["shell_functions"]["sample_value"] = "value"
    layout = Layout(config, repository_source=home)
    expected = home / "$SOME_PRIVATE_VARIABLE/value"
    assert layout.resolve("value") == expected
    assert shell(layout, "sample_value\n").stdout.strip() == str(expected)
