import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from runtime_layout import Layout
from runtime_layout.migration import MigrationError, Migrator


@pytest.fixture
def migration(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "runtime"
    cfg = {
        "schema": "runtime-layout/v1",
        "root": {"default": str(root)},
        "paths": {},
        "migration": {
            "locks": [{"legacy": str(tmp_path / "writer.lock"), "current": "{root}/locks/writer"}],
            "lock_timeout": 0.1,
            "items": [],
            "directories": ["credentials", "state"],
            "private_directories": [".", "credentials"],
        },
    }
    return tmp_path, cfg, Migrator(Layout(cfg, repository_source=tmp_path))


def add_move(cfg, src, dst, **extra):
    cfg["migration"]["items"].append(
        {"kind": "move", "source": str(src), "destination": dst, **extra}
    )


def test_preview_does_not_create_root_locks_or_call_services(migration, monkeypatch):
    base, cfg, runner = migration
    source = base / "source"
    source.write_text("synthetic")
    add_move(cfg, source, "{root}/credentials/read", service="sample")
    cfg["migration"]["services"] = {"sample": {"active": ["never-run"]}}
    before = set(base.rglob("*"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("preview called a service"))
    plan = runner.plan()
    assert len(plan["actions"]) == 1
    assert set(base.rglob("*")) == before
    assert source.read_text() == "synthetic"


def test_apply_moves_hidden_content_separates_credentials_and_reuses_locks(migration):
    base, cfg, runner = migration
    source = base / "old"
    source.mkdir()
    (source / ".watermark").write_text("progress")
    (source / "normal").write_text("data")
    cfg["migration"]["items"] = [
        {"kind": "contents", "source": str(source), "destination": "{root}/state/job"}
    ]
    token = base / "read"
    token.write_text("synthetic read")
    add_move(cfg, token, "{root}/credentials/read")
    result = runner.apply()
    assert result["moved"] == 3
    assert (runner.root / "state/job/.watermark").read_text() == "progress"
    assert (runner.root / "credentials/read").read_text() == "synthetic read"
    assert not (runner.root / "credentials/write").exists()
    assert (runner.root / "credentials").stat().st_mode & 0o777 == 0o700
    assert os.path.samefile(base / "writer.lock", runner.root / "locks/writer")
    assert runner.apply()["moved"] == 0


def test_existing_destination_or_different_lock_inodes_is_rejected_before_changes(migration):
    base, cfg, runner = migration
    source = base / "old"
    source.write_text("old")
    destination = runner.root / "state/value"
    destination.parent.mkdir(parents=True)
    destination.write_text("new")
    add_move(cfg, source, "{root}/state/value")
    with pytest.raises(MigrationError, match="destination already exists"):
        runner.apply()
    assert source.read_text() == "old" and destination.read_text() == "new"
    cfg["migration"]["items"] = []
    (base / "writer.lock").touch()
    new = runner.root / "locks/writer"
    new.parent.mkdir()
    new.touch()
    old_inode = (base / "writer.lock").stat().st_ino
    with pytest.raises(MigrationError, match="different inodes"):
        runner.apply()
    assert (base / "writer.lock").stat().st_ino == old_inode
    assert not os.path.samefile(base / "writer.lock", new)


def test_other_process_cannot_enter_either_lock_after_activation(migration, monkeypatch):
    base, cfg, runner = migration
    source = base / "old"
    source.write_text("data")
    add_move(cfg, source, "{root}/state/value")
    checks = []
    original = os.rename

    def rename(src, dst):
        original(src, dst)
        if Path(dst) == runner.root:
            for path in (base / "writer.lock", runner.root / "locks/writer"):
                code = "import fcntl,os,sys;f=os.open(sys.argv[1],os.O_RDWR);fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                result = subprocess.run(["python3", "-c", code, str(path)], capture_output=True)
                checks.append(result.returncode)

    monkeypatch.setattr(os, "rename", rename)
    runner.apply()
    assert len(checks) == 2 and all(value != 0 for value in checks)


def test_a_queued_old_lock_holder_and_new_path_share_the_same_inode(migration):
    base, _, runner = migration
    runner.apply()
    fd = os.open(base / "writer.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        command = [
            "python3",
            "-c",
            'import fcntl,sys;f=open(sys.argv[1],"r+");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)',
            str(runner.root / "locks/writer"),
        ]
        assert subprocess.run(command, capture_output=True).returncode != 0
    finally:
        os.close(fd)
    assert subprocess.run(command, capture_output=True).returncode == 0


def test_move_failure_restores_source_and_retry_keeps_progress(migration, monkeypatch):
    base, cfg, runner = migration
    for name in ("a", "b"):
        source = base / name
        source.write_text(name)
        add_move(cfg, source, "{root}/state/" + name)
    original = os.rename

    def fail_second(src, dst):
        if Path(src) == base / "b":
            raise OSError("synthetic move failure")
        return original(src, dst)

    with monkeypatch.context() as temporary:
        temporary.setattr(os, "rename", fail_second)
        with pytest.raises(OSError):
            runner.apply()
    assert not runner.root.exists()
    assert (base / "a").read_text() == "a" and (base / "b").read_text() == "b"
    assert runner.apply()["moved"] == 2
    assert (runner.root / "state/a").read_text() == "a"


def test_bounded_service_stop_and_failure_recovery(migration, monkeypatch):
    base, cfg, runner = migration
    source = base / "value"
    source.write_text("data")
    add_move(cfg, source, "{root}/state/value", service="daemon")
    cfg["migration"]["services"] = {
        "daemon": {"active": ["status"], "stop": ["stop"], "start": ["start"]}
    }
    calls = []

    def command(argv, **kwargs):
        calls.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "command", command)
    original = os.rename

    def fail(src, dst):
        if Path(src) == source:
            raise OSError("synthetic")
        return original(src, dst)

    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(OSError):
        runner.apply()
    assert calls == ["status", "stop", "start", "status"]
    assert source.exists() and not runner.root.exists()


def test_failed_service_stop_moves_nothing(migration, monkeypatch):
    base, cfg, runner = migration
    source = base / "value"
    source.write_text("data")
    add_move(cfg, source, "{root}/state/value", service="daemon")
    cfg["migration"]["services"] = {
        "daemon": {"active": ["status"], "stop": ["stop"], "start": ["start"]}
    }

    def command(argv, **kwargs):
        if argv == ["stop"]:
            raise MigrationError("stop failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(MigrationError, match="stop failed"):
        runner.apply()
    assert source.exists() and not runner.root.exists()


def test_git_worktree_moves_registration_and_preserves_dirty_files(migration):
    base, cfg, runner = migration
    repo = base / "repository"

    def git(*args):
        return subprocess.run(
            ["git", *map(str, args)], check=True, capture_output=True, text=True
        ).stdout

    git("init", "-b", "main", repo)
    git(
        "-C",
        repo,
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
    )
    old = base / "old-worktree"
    git("-C", repo, "worktree", "add", "-b", "task", old)
    (old / "paid-output").write_text("preserved output")
    cfg["migration"]["items"] = [
        {
            "kind": "worktree",
            "source": str(old),
            "destination": "{root}/worktree",
            "repository": str(repo),
        }
    ]
    runner.apply()
    new = runner.root / "worktree"
    assert (new / "paid-output").read_text() == "preserved output"
    assert "worktree " + str(new) in git("-C", repo, "worktree", "list", "--porcelain")
    assert git("-C", new, "status", "--porcelain").strip() == "?? paid-output"
    assert runner.apply()["moved"] == 0


def test_locked_worktree_refuses_before_root_activation(migration):
    base, cfg, runner = migration
    repo = base / "repository"
    for args in [
        ["init", "-b", "main", str(repo)],
        [
            "-C",
            str(repo),
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
    ]:
        subprocess.run(["git", *args], check=True, capture_output=True)
    old = base / "old-worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "task", str(old)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "lock", str(old)], check=True, capture_output=True
    )
    cfg["migration"]["items"] = [
        {
            "kind": "worktree",
            "source": str(old),
            "destination": "{root}/worktree",
            "repository": str(repo),
        }
    ]
    with pytest.raises(MigrationError, match="locked"):
        runner.apply()
    assert old.exists() and not runner.root.exists()


def test_cli_defaults_to_plan_and_requires_apply(migration, tmp_path):
    base, cfg, _ = migration
    source = base / "source"
    source.write_text("data")
    add_move(cfg, source, "{root}/state/value")
    config = tmp_path / "layout.json"
    config.write_text(json.dumps(cfg))
    script = Path(__file__).resolve().parents[1] / "scripts/migrate"
    result = subprocess.run([str(script), "--config", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["activate_root"] is True
    assert source.exists() and not (base / "writer.lock").exists()


def test_parent_and_child_moves_are_rejected_before_activation(migration):
    base, cfg, runner = migration
    parent = base / "parent"
    parent.mkdir()
    (parent / "child").write_text("data")
    add_move(cfg, parent, "{root}/state/parent")
    add_move(cfg, parent / "child", "{root}/state/child")
    with pytest.raises(MigrationError, match="overlapping move sources"):
        runner.apply()
    assert (parent / "child").read_text() == "data" and not runner.root.exists()


def test_all_stopped_services_are_attempted_when_one_restart_fails(migration, monkeypatch):
    base, cfg, runner = migration
    for name in ("one", "two"):
        source = base / name
        source.write_text(name)
        add_move(cfg, source, "{root}/state/" + name, service=name)
    cfg["migration"]["services"] = {
        name: {"active": ["status", name], "stop": ["stop", name], "start": ["start", name]}
        for name in ("one", "two")
    }
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv == ["start", "two"]:
            raise MigrationError("synthetic restart failure")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(MigrationError, match="every stopped service was attempted"):
        runner.apply()
    assert ["start", "two"] in calls and ["start", "one"] in calls
    assert (runner.root / "state/one").read_text() == "one"
    assert (runner.root / "state/two").read_text() == "two"


def test_cross_filesystem_move_is_rejected_without_moving(migration, monkeypatch):
    from types import SimpleNamespace

    base, cfg, runner = migration
    source = base / "source"
    source.write_text("data")
    add_move(cfg, source, "{root}/state/value")
    original = Path.lstat

    def other_device(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return SimpleNamespace(st_dev=value.st_dev + 1) if path == source else value

    monkeypatch.setattr(Path, "lstat", other_device)
    with pytest.raises(MigrationError, match="cross-filesystem"):
        runner.apply()
    assert source.read_text() == "data" and not runner.root.exists()


def test_globs_hidden_content_and_cache_link_preserve_unrelated_link(migration):
    base, cfg, runner = migration
    old = base / "old"
    old.mkdir()
    (old / "entry-a").write_text("a")
    (old / "other").write_text("keep")
    cfg["migration"]["items"] = [
        {"kind": "glob", "source": str(old / "entry-*"), "destination": "{root}/state/{name}"}
    ]
    link = base / "cache"
    link.symlink_to(old)
    other = base / "other-link"
    other.symlink_to(base / "elsewhere")
    cfg["migration"]["symlinks"] = [
        {"path": str(link), "old_prefix": str(old), "target": "{root}/state"},
        {"path": str(other), "old_prefix": str(old), "target": "{root}/state"},
    ]
    runner.apply()
    assert (runner.root / "state/entry-a").read_text() == "a"
    assert (old / "other").read_text() == "keep"
    assert link.readlink() == runner.root / "state"
    assert other.readlink() == base / "elsewhere"


def test_writer_lock_timeout_does_not_activate_root(migration):
    base, _, runner = migration
    fd = os.open(base / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(MigrationError, match="timed out"):
            runner.apply()
    finally:
        os.close(fd)
    assert not runner.root.exists()


def test_worktree_repair_failure_keeps_complete_output_and_can_resume(migration, monkeypatch):
    base, cfg, runner = migration
    repo = base / "repo"
    old = base / "old"
    for args in [
        ["init", "-b", "main", str(repo)],
        [
            "-C",
            str(repo),
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
        ["-C", str(repo), "worktree", "add", "-b", "task", str(old)],
    ]:
        subprocess.run(["git", *args], check=True, capture_output=True)
    (old / "paid").write_text("preserved")
    cfg["migration"]["items"] = [
        {
            "kind": "worktree",
            "source": str(old),
            "destination": "{root}/worktree",
            "repository": str(repo),
        }
    ]
    real_command = runner.command

    def failure(argv, **kwargs):
        if "repair" in argv:
            raise MigrationError("synthetic repair failure")
        return real_command(argv, **kwargs)

    with monkeypatch.context() as temporary:
        temporary.setattr(runner, "command", failure)
        with pytest.raises(MigrationError, match="repair failure"):
            runner.apply()
    assert (runner.root / "worktree/paid").read_text() == "preserved"
    assert not old.exists()
    assert runner.apply()["moved"] == 0
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "worktree " + str(runner.root / "worktree") in result.stdout
