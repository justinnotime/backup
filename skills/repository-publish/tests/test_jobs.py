"""Configured jobs use the same isolated publication transaction as argv writers."""

import json
import os
import subprocess
import sys

import pytest
from test_publish import PACKAGE, Project, success


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


def config(project, steps=None):
    data = {
        "schema": "repository-publish-job/v1",
        "repo": str(project.repo),
        "task": "configured",
        "paths": ["archive"],
        "subject": "sync: configured example",
        "state_dir": str(project.state),
        "lock": str(project.base / "task.lock"),
        "scratch": str(project.scratch),
        "publish_lock": str(project.base / "publish.lock"),
        "retry_delay": 0,
        "steps": steps or [{"id": "collect", "argv": project.counter_writer()}],
    }
    return data


def run(project, data, *extra):
    path = project.base / "job.json"
    path.write_text(json.dumps(data))
    return subprocess.run(
        [str(PACKAGE / "scripts/publish"), "--config", str(path), *extra],
        env=project.env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_configured_publication_and_state(project):
    result = run(project, config(project))
    success(result)
    assert (project.state / "count").read_text() == "1"
    assert project.git("show", "main:archive/message-1.md", root=project.remote) == "message 1"
    project.assert_clean()


def test_failed_later_step_does_not_publish_or_advance(project):
    data = config(project)
    data["steps"].append({"id": "second", "argv": [sys.executable, "-c", "raise SystemExit(23)"]})
    result = run(project, data)
    assert result.returncode != 0 and "exited 23" in result.stderr
    assert not (project.state / "count").exists()
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    project.assert_clean()


def test_explicit_continue_policy_and_group_selection(project):
    data = config(project)
    data["steps"] = [
        {
            "id": "mirror",
            "group": "documents",
            "argv": [sys.executable, "-c", "raise SystemExit(9)"],
            "on_error": "continue",
        },
        {"id": "index", "group": "documents", "argv": project.counter_writer()},
        {"id": "unselected", "argv": ["/unavailable-example-command"]},
    ]
    result = run(project, data, "--steps", "documents")
    success(result)
    assert "WARN:" in result.stdout
    assert (project.state / "count").read_text() == "1"
    project.assert_clean()


@pytest.mark.parametrize("mode", ["--doctor", "--dry-run"])
def test_inspection_has_no_writer_or_lock_effects(project, mode):
    result = run(project, config(project), mode)
    success(result)
    assert json.loads(result.stdout)["steps"] == ["collect"]
    assert not (project.base / "task.lock").exists()
    assert not project.scratch.exists()
    assert list(project.state.iterdir()) == []
    project.assert_clean()


def test_unset_or_misspelled_selection_fails_before_mutation(project):
    result = run(project, config(project), "--steps", "missing")
    assert result.returncode != 0
    assert "unknown step" in result.stderr
    assert not project.scratch.exists()


def test_private_environment_overrides_paths_without_shell_evaluation(project):
    data = config(project)
    data["environment"] = {
        "EXAMPLE_ROOT": {"env": "EXAMPLE_ROOT", "default": "~"},
        "EXAMPLE_VALUE": "literal $(touch should-not-exist)",
    }
    data["steps"] = [
        {
            "id": "check",
            "argv": project.writer(
                "assert os.environ['EXAMPLE_ROOT'] == os.environ['HOME']\n"
                "assert os.environ['EXAMPLE_VALUE'] == 'literal $(touch should-not-exist)'\n"
                "(root/'archive/output.md').write_text('configured')\n"
            ),
        }
    ]
    success(run(project, data))
    assert not (project.repo / "should-not-exist").exists()


def test_selected_step_environment_is_used_by_doctor(project):
    executable = project.base / "bin/example-command"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    data = config(project, [{"id": "custom", "argv": ["example-command"]}])
    data["environment"] = {"PATH": str(executable.parent) + os.pathsep + project.env["PATH"]}
    success(run(project, data, "--doctor"))


def copy_config(project, name="report.md"):
    source = project.base / "report.md"
    source.write_text("example handoff")
    return config(
        project,
        [{"id": "copy", "copy": {"source": str(source), "directory": "archive", "name": name}}],
    )


def test_copy_publication_and_source_retained(project):
    success(run(project, copy_config(project)))
    assert project.git("show", "main:archive/report.md", root=project.remote) == "example handoff"
    assert (project.base / "report.md").read_text() == "example handoff"
    project.assert_clean()


@pytest.mark.parametrize("name", ["../outside.md", "/absolute.md", ".hidden", "", "a/b", r"a\b"])
def test_copy_rejects_destination_escape(project, name):
    result = run(project, copy_config(project, name))
    assert result.returncode != 0 and "filename" in result.stderr
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    project.assert_clean()


def test_copy_rejects_symlink_destination(project):
    outside = project.base / "outside"
    outside.mkdir()
    (project.repo / "archive/link").symlink_to(outside, target_is_directory=True)
    project.git("add", "archive/link")
    project.git("commit", "-m", "example linked directory")
    project.git("push", "origin", "main")
    data = copy_config(project)
    data["steps"][0]["copy"]["directory"] = "archive/link"
    result = run(project, data)
    assert result.returncode != 0 and "escapes" in result.stderr
    assert list(outside.iterdir()) == []


def test_failed_publish_retains_copy_source(project):
    hook = project.remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    data = copy_config(project)
    data["attempts"] = 1
    result = run(project, data)
    assert result.returncode != 0
    assert (project.base / "report.md").read_text() == "example handoff"
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    project.assert_clean()


def test_native_options_with_external_writer_and_cli_paths(project):
    data = config(project)
    data.pop("steps")
    data.pop("paths")
    success(run(project, data, "--paths", "archive", "--", *project.counter_writer()))
    assert (project.state / "count").read_text() == "1"
    project.assert_clean()


@pytest.mark.parametrize("field", ["paths", "sparse"])
def test_literal_path_arrays_do_not_widen_scope(project, field):
    data = config(project)
    data[field] = ["archive reports"]
    result = run(project, data)
    assert result.returncode != 0 and "whitespace" in result.stderr
    assert not project.scratch.exists()


def test_step_cannot_replace_transaction_alias(project):
    data = config(project)
    data["worktree_env"] = "EXAMPLE_OUTPUT_ROOT"
    data["steps"][0]["environment"] = {"EXAMPLE_OUTPUT_ROOT": str(project.repo)}
    result = run(project, data)
    assert result.returncode != 0 and "transaction environment aliases" in result.stderr
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
