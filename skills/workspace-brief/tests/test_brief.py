from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from workspace_brief import brief, install

NOW = datetime(2025, 2, 14, 4, tzinfo=timezone.utc)


def write(path, text, days=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = NOW.timestamp() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def test_projects_and_latest_are_selected_sorted_and_read_only(tmp_path):
    root = tmp_path / "workspace"
    write(root / "Projects/older/README.md", "title: Older\nstatus: active\n", 40)
    write(root / "Projects/older/2025-01-01.md", "# Old detail\n", 35)
    write(root / "Projects/recent/README.md", "title: 'Recent project'\n", 5)
    write(root / "Projects/recent/2025-02-10.md", "# New detail\n", 1)
    write(root / "Projects/retired/README.md", "# Retired\nstatus: archived\n")
    write(root / "Projects/no-index/nested/detail.md", "# No index\n", 3)
    write(root / "Threads/source.md", "# Source thread\n", 2)
    write(root / "Threads/ORIGIN.md", "# Ignore newest marker\n")
    outside = write(tmp_path / "excluded/private.md", "# Do not read outside\n")
    (root / "Projects/recent/escape.md").symlink_to(outside)
    (root / "Projects/linked").symlink_to(outside.parent, target_is_directory=True)
    config = {
        "repository_root": root,
        "header": ["Brief"],
        "projects": {
            "directory": "Projects",
            "heading": "Projects {shown}/{total}",
            "exclude_status_prefixes": ["archived"],
            "limit": 2,
        },
        "latest": {"directory": "Threads", "exclude": ["ORIGIN.md"], "heading": "Latest"},
        "footer": ["End @root@"],
    }
    before = snapshot(tmp_path)
    output = brief.render(config, now=NOW)
    assert "Projects 2/4" in output
    assert "[5d  ] recent/README.md: Recent project" in output
    assert "recent/2025-02-10.md: New detail" in output
    assert "no-index/" in output
    assert "Older" not in output and "Retired" not in output
    assert "Source thread" in output and "Do not read" not in output
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "moment,period,expected",
    [
        ("2025-02-14T02:00:00+00:00", "daily", "2025-02-12"),
        ("2025-02-14T03:00:00+00:00", "daily", "2025-02-13"),
        ("2025-02-14T02:00:00+00:00", "weekly", "2025-02-06"),
        ("2025-02-14T03:00:00+00:00", "weekly", "2025-02-13"),
        ("2025-02-13T20:00:00+00:00", "weekly", "2025-02-06"),
        ("2025-02-17T20:00:00+00:00", "weekly", "2025-02-13"),
    ],
)
def test_expected_artifact_dates_preserve_grace_window(moment, period, expected):
    assert (
        brief.expected_date(
            {"period": period, "ready_hour_utc": 3, "period_end_weekday": 3},
            datetime.fromisoformat(moment),
        )
        == expected
    )


def test_marker_log_fallback_and_output_checks_are_independent(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    marker = write(tmp_path / "state/failure", "A selected failure\nsecret later line\n")
    job = write(
        tmp_path / "job.json", json.dumps({"schema_version": "job/v1", "failure": str(marker)})
    )
    old_log = write(tmp_path / "old.log", "old activity", 2)
    new_log = write(tmp_path / "new.log", "recent activity")
    config = {
        "heading": "Health",
        "marker_source": {
            "path": str(job),
            "schema_version": "job/v1",
            "field": "failure",
            "line": "FAIL {detail} ({path})",
        },
        "logs": [
            {"name": "selected", "paths": [str(new_log), str(old_log)], "cadence_minutes": 30}
        ],
        "healthy_line": "Healthy",
        "overdue_line": "Late {name} {age_minutes}",
        "artifacts": [
            {"period": "daily", "path": "Reports/{date}.md", "missing_line": "Missing {path}"}
        ],
    }
    lines = brief.health_lines(config, root, NOW)
    assert any("A selected failure" in line for line in lines)
    assert "secret later line" not in "\n".join(lines)
    assert "Healthy" in lines and not any(line.startswith("Late") for line in lines)
    assert "Missing Reports/2025-02-13.md" in lines
    new_log.unlink()
    assert "Late selected 2880" in brief.health_lines(config, root, NOW)
    job.write_text('{"schema_version":"job/v1","failure":"relative-file"}')
    assert "  WARN configured marker unavailable" in brief.health_lines(config, root, NOW)


def test_queue_executes_only_explicit_read_command_and_failed_output_is_suppressed(tmp_path):
    config = {"heading": "Queue", "argv": [sys.executable, "-B", "-c", "print('Open task')"]}
    before = snapshot(tmp_path)
    assert brief.queue_lines(config, tmp_path) == ["Queue", "  Open task", ""]
    config["argv"][-1] = "import sys; print('sensitive exception'); sys.exit(1)"
    output = brief.queue_lines(config, tmp_path)
    assert "sensitive exception" not in "\n".join(output)
    config["argv"][-1] = "import time; time.sleep(1)"
    config["timeout_seconds"] = 0.02
    assert "WARN queue unavailable" in "\n".join(brief.queue_lines(config, tmp_path))
    assert snapshot(tmp_path) == before


def test_empty_or_missing_log_observations_never_report_health(tmp_path):
    config = {
        "heading": "Health",
        "logs": [{"name": "writer", "paths": [str(tmp_path / "missing")], "cadence_minutes": 30}],
        "healthy_line": "All observed",
        "overdue_line": "Late",
    }
    assert brief.health_lines(config, tmp_path, NOW) == [
        "Health",
        "  WARN configured logs unavailable",
    ]
    config["logs"] = []
    assert "All observed" not in brief.health_lines(config, tmp_path, NOW)


def test_unrelated_empty_hook_groups_are_unchanged():
    original = {
        "hooks": {
            "SessionStart": [
                {"matcher": "other", "hooks": [], "extra": True},
                {"matcher": "empty", "extra": True},
            ]
        }
    }
    result = install.update(original, "selected", [], 5, "uninstall")
    assert result == original


def test_missing_marker_configuration_is_unknown_but_absent_failure_file_is_normal(tmp_path):
    log = write(tmp_path / "writer.log", "A current run")
    job = tmp_path / "job.json"
    config = {
        "heading": "Health",
        "logs": [{"name": "writer", "paths": [str(log)], "cadence_minutes": 30}],
        "healthy_line": "Observed logs current",
        "overdue_line": "Late",
        "marker_source": {
            "path": str(job),
            "schema_version": "job/v1",
            "field": "failure",
            "line": "Failure {detail}",
        },
    }
    assert "Observed logs current" not in brief.health_lines(config, tmp_path, NOW)
    assert "  WARN configured marker unavailable" in brief.health_lines(config, tmp_path, NOW)
    job.write_text(
        json.dumps({"schema_version": "job/v1", "failure": str(tmp_path / "missing.failure")})
    )
    assert "Observed logs current" in brief.health_lines(config, tmp_path, NOW)


def test_git_worktree_health_never_updates_index_or_repository(tmp_path):
    root, worktree = tmp_path / "repository", tmp_path / "working copy"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Example")
    git("config", "user.email", "example@example.invalid")
    write(root / "record.txt", "Original")
    git("add", "record.txt")
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "Initial"],
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2025-02-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2025-02-01T00:00:00Z",
        },
        check=True,
    )
    git("worktree", "add", "-qb", "task", str(worktree))
    write(worktree / "record.txt", "Changed")
    before = snapshot(tmp_path)
    lines = brief.worktree_lines(
        {"idle_days": 2, "base_ref": "main", "line": "{name}: {idle_days}, {dirty}, {ahead}"},
        root,
        NOW,
    )
    assert lines == ["working copy: 13, 1, 0"]
    assert snapshot(tmp_path) == before


def test_storage_threshold_uses_portable_statvfs(tmp_path, monkeypatch):
    class Values:
        f_files = 1000
        f_ffree = 20
        f_favail = 20

    monkeypatch.setattr(os, "statvfs", lambda _: Values())
    config = {
        "path": str(tmp_path),
        "minimum_free_inodes": 30,
        "line": "Free {free}, used {used_percent}",
    }
    assert brief.storage_lines(config, tmp_path) == ["Free 20, used 98%", ""]
    config["minimum_free_inodes"] = 20
    assert brief.storage_lines(config, tmp_path) == []


def test_cli_doctor_has_no_external_command_or_writes_and_hook_errors_do_not_block(tmp_path):
    config = write(
        tmp_path / "config.json",
        json.dumps(
            {
                "schema_version": "workspace-brief/v1",
                "repository_root": str(tmp_path),
                "queue": {
                    "argv": [
                        sys.executable,
                        "-B",
                        "-c",
                        "raise RuntimeError('synthetic failed queue')",
                    ],
                    "heading": "Queue",
                },
                "footer": ["Retained footer"],
            }
        ),
    )
    script = Path(__file__).parents[1] / "scripts/brief"
    before = snapshot(tmp_path)
    for extra in (["--doctor"], []):
        result = subprocess.run(
            [str(script), "--config", str(config), *extra],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
    assert snapshot(tmp_path) == before
    config.write_text("{")
    assert (
        subprocess.run(
            [str(script), "--config", str(config)],
            input="{}",
            text=True,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            [str(script), "--config", str(config), "--doctor"], capture_output=True, check=False
        ).returncode
        == 1
    )


def test_doctor_never_calls_selected_programs(tmp_path, monkeypatch):
    config = write(
        tmp_path / "config.json",
        json.dumps(
            {
                "schema_version": "workspace-brief/v1",
                "repository_root": str(tmp_path),
                "queue": {"argv": [sys.executable]},
            }
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor executed an external program")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert brief.main(["--config", str(config), "--doctor"]) == 0


def test_many_worktrees_share_one_timeout_budget(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    paths = [tmp_path / f"work-{number}" for number in range(30)]
    for path in paths:
        path.mkdir()
    clock = iter([0, 0, 0.6, 1.2, 1.8, 2.4])
    monkeypatch.setattr(brief.time, "monotonic", lambda: next(clock))
    calls = []

    def fake_git(path, *args, timeout):
        calls.append((path, args, timeout))
        if args[0] == "worktree":
            return "\n".join("worktree " + str(path) for path in paths)
        return str(int(NOW.timestamp()))

    monkeypatch.setattr(brief, "git", fake_git)
    result = brief.render(
        {
            "repository_root": root,
            "worktrees": {"budget_seconds": 2, "line": "unused"},
            "footer": ["Footer survives timeout"],
        },
        now=NOW,
    )
    assert "WARN workspace-brief: worktrees unavailable" in result
    assert "Footer survives timeout" in result
    assert len(calls) == 4
    assert all(0 < call[2] <= 2 for call in calls)


def test_install_preserves_other_commands_and_entry_fields_and_is_idempotent(tmp_path):
    command = "'/path with spaces/brief' --config /private/config.json"
    other = {
        "type": "command",
        "command": "another-repository/session-start.sh",
        "timeout": 9,
        "extra": "preserve",
    }
    original = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "extra": "entry",
                    "hooks": [{"type": "command", "command": "old-exact-entry"}, other],
                }
            ],
            "Stop": [{"hooks": [other]}],
        },
        "other": True,
    }
    migrated = install.update(original, command, ["old-exact-entry"], 5, "install")
    assert migrated["hooks"]["SessionStart"][0] == {
        "matcher": "startup",
        "extra": "entry",
        "hooks": [{"type": "command", "command": command, "timeout": 5}, other],
    }
    assert install.update(migrated, command, ["old-exact-entry"], 5, "install") == migrated
    removed = install.update(migrated, command, ["old-exact-entry"], 5, "uninstall")
    assert removed["hooks"]["SessionStart"][0]["hooks"] == [other]
    assert removed["hooks"]["Stop"] == original["hooks"]["Stop"]


def test_install_real_settings_backup_permissions_check_and_uninstall(tmp_path):
    settings = write(tmp_path / "settings.json", '{"other": true}\n')
    settings.chmod(0o600)
    config = write(
        tmp_path / "config.json",
        json.dumps(
            {
                "schema_version": "workspace-brief/v1",
                "repository_root": str(tmp_path),
                "hook": {
                    "settings_path": str(settings),
                    "argv": ["/synthetic path/brief", "--config", "/selected/config.json"],
                    "timeout_seconds": 5,
                },
            }
        ),
    )
    before = snapshot(tmp_path)
    assert install.main(["--config", str(config), "check"]) == 0
    assert snapshot(tmp_path) == before
    assert install.main(["--config", str(config)]) == 0
    backups = list(tmp_path.glob("settings.json.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == '{"other": true}\n'
    assert settings.stat().st_mode & 0o777 == 0o600
    assert backups[0].stat().st_mode & 0o777 == 0o600
    before = snapshot(tmp_path)
    assert install.main(["--config", str(config)]) == 0
    assert snapshot(tmp_path) == before
    assert install.main(["--config", str(config), "uninstall"]) == 0
    assert json.loads(settings.read_text()) == {"other": True}


def test_portable_marker_config_is_expanded_without_running_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "different home"))
    marker = write(tmp_path / "different home/state/failure", "retained failure\n")
    job = write(
        tmp_path / "job.json",
        json.dumps(
            {
                "schema_version": "job/v1",
                "expand_environment": True,
                "environment": {"EXAMPLE_STATE": "$HOME/state"},
                "failure": "${EXAMPLE_STATE}/failure",
            }
        ),
    )
    config = {
        "heading": "Health",
        "marker_source": {
            "path": str(job),
            "schema_version": "job/v1",
            "field": "failure",
            "line": "FAIL {detail} ({path})",
        },
    }
    before = snapshot(tmp_path)
    lines = brief.health_lines(config, tmp_path, NOW)
    assert f"FAIL retained failure ({marker})" in lines
    assert snapshot(tmp_path) == before
    value = json.loads(job.read_text())
    value["environment"]["EXAMPLE_STATE"] = "$UNSET_EXAMPLE_VARIABLE/state"
    job.write_text(json.dumps(value))
    assert "  WARN configured marker unavailable" in brief.health_lines(config, tmp_path, NOW)
