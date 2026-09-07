import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime_install import git_hooks

PACKAGE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "another user"))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.invalid")


def repository(tmp_path):
    root = tmp_path / "another user/project main"
    root.mkdir(parents=True)
    git_hooks.git(root, "init", "-b", "trunk")
    git_hooks.git(root, "commit", "--allow-empty", "-m", "synthetic seed")
    return root


def config_for(root, tmp_path):
    return {
        "schema": "runtime-install/v1",
        "kind": "git-hooks",
        "repository": str(root),
        "lock": str(tmp_path / "installation lock/hooks"),
        "backup_directory": str(tmp_path / "backup hooks"),
        "hooks": [
            {
                "name": "pre-commit",
                "source": str(PACKAGE / "scripts/main-worktree-guard"),
            }
        ],
        "main_guard": {
            "when_environment": "EXAMPLE_AGENT",
            "bypass_environment": "EXAMPLE_OVERRIDE",
        },
    }


def commit(root, **environment):
    return subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "test commit"],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )


def test_preview_does_not_write_and_real_git_guard_follows_worktree_metadata(
    tmp_path, capsys
):
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    task = root.parent / "project task"
    git_hooks.git(root, "worktree", "add", "-b", "task", str(task))
    before = (root / ".git/config").read_bytes()
    git_hooks.install(config, dry_run=True)
    assert json.loads(capsys.readouterr().out)[0]["action"] == "link"
    assert not Path(config["lock"]).exists()
    assert not Path(config["backup_directory"]).exists()
    assert (root / ".git/config").read_bytes() == before
    git_hooks.install(config)
    result = commit(root, EXAMPLE_AGENT="yes")
    assert result.returncode == 1 and "commit refused" in result.stderr
    assert commit(task, EXAMPLE_AGENT="yes").returncode == 0
    assert commit(root).returncode == 0
    assert commit(root, EXAMPLE_AGENT="yes", EXAMPLE_OVERRIDE="yes").returncode == 0
    git_hooks.git(root, "switch", "-c", "another-branch")
    assert commit(root, EXAMPLE_AGENT="yes").returncode == 1
    git_hooks.git(root, "checkout", "--detach")
    assert commit(root, EXAMPLE_AGENT="yes").returncode == 1
    assert (root / ".git/hooks/pre-commit").is_symlink()
    assert not os.path.isabs(os.readlink(root / ".git/hooks/pre-commit"))
    git_hooks.install(config)
    assert len(list(Path(config["backup_directory"]).iterdir())) == 1


def test_guard_in_bare_repository_worktree_and_nondefault_home(tmp_path):
    root = repository(tmp_path)
    bare = tmp_path / "another user/source.git"
    subprocess.run(
        ["git", "clone", "--bare", str(root), str(bare)],
        check=True,
        capture_output=True,
    )
    main = tmp_path / "another user/moved checkout"
    git_hooks.git(bare, "worktree", "add", str(main), "trunk")
    task = tmp_path / "another user/moved task"
    git_hooks.git(bare, "worktree", "add", "-b", "separate", str(task))
    git_hooks.install(config_for(main, tmp_path))
    assert commit(main, EXAMPLE_AGENT="yes").returncode == 1
    assert commit(task, EXAMPLE_AGENT="yes").returncode == 0
    git_hooks.git(main, "checkout", "--detach")
    assert commit(main, EXAMPLE_AGENT="yes").returncode == 1


def test_unknown_hooks_are_preserved_and_do_not_receive_guard_policy(tmp_path):
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    target = root / ".git/hooks/pre-commit"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o751)
    before = target.read_bytes()
    git_hooks.install(config)
    assert target.read_bytes() == before and not target.is_symlink()
    assert not Path(config["backup_directory"]).exists()
    assert not git_hooks.git(
        root, "config", "--get", git_hooks.GUARD_KEY, missing_ok=(1,)
    )
    target.unlink()
    target.symlink_to("/example/foreign-hook")
    git_hooks.install(config)
    assert os.readlink(target) == "/example/foreign-hook"


def test_explicit_legacy_digest_is_backed_up_and_all_sources_validated_first(tmp_path):
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    target = root / ".git/hooks/pre-commit"
    original = b"#!/bin/sh\n# selected legacy hook\nexit 0\n"
    target.write_bytes(original)
    target.chmod(0o751)
    config["hooks"][0]["replace_sha256"] = [hashlib.sha256(original).hexdigest()]
    config["hooks"].append(
        {"name": "prepare-commit-msg", "source": str(tmp_path / "missing")}
    )
    with pytest.raises(git_hooks.InstallError):
        git_hooks.install(config)
    assert target.read_bytes() == original
    assert not Path(config["backup_directory"]).exists()
    config["hooks"].pop()
    git_hooks.install(config)
    saved = next(Path(config["backup_directory"]).iterdir())
    assert (saved / "pre-commit").read_bytes() == original
    assert (saved / "pre-commit").stat().st_mode & 0o777 == 0o751
    assert saved.stat().st_mode & 0o777 == 0o700
    assert json.loads((saved / "snapshot.json").read_text())["guard_values"] == []


def test_later_install_failure_restores_file_link_and_repository_policy(
    tmp_path, monkeypatch
):
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    target = root / ".git/hooks/pre-commit"
    original = b"#!/bin/sh\nexit 0\n"
    target.write_bytes(original)
    target.chmod(0o751)
    config["hooks"][0]["replace_sha256"] = [hashlib.sha256(original).hexdigest()]
    second = root / ".git/hooks/prepare-commit-msg"
    second.symlink_to("/example/old-trailer")
    config["hooks"].append(
        {
            "name": "prepare-commit-msg",
            "source": config["hooks"][0]["source"],
            "replace_targets": ["/example/old-trailer"],
        }
    )
    real = git_hooks.replace_link

    def fail_second(destination, source):
        if destination == second:
            raise OSError("synthetic failure")
        real(destination, source)

    monkeypatch.setattr(git_hooks, "replace_link", fail_second)
    with pytest.raises(
        git_hooks.InstallError, match="previous hooks and policy restored"
    ):
        git_hooks.install(config)
    assert target.read_bytes() == original and not target.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o751
    assert os.readlink(second) == "/example/old-trailer"
    assert not git_hooks.git(
        root, "config", "--get", git_hooks.GUARD_KEY, missing_ok=(1,)
    )


def test_shared_external_hooks_directory_requires_explicit_selection(tmp_path):
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    shared = tmp_path / "shared hooks"
    git_hooks.git(root, "config", "core.hooksPath", str(shared))
    with pytest.raises(git_hooks.InstallError, match="custom hooks directory"):
        git_hooks.install(config)
    assert not shared.exists()
    config["hooks_directory"] = str(shared)
    git_hooks.install(config)
    assert commit(root, EXAMPLE_AGENT="yes").returncode == 1


def test_missing_guard_policy_refuses_commit_instead_of_silently_disabling_guard(
    tmp_path,
):
    root = repository(tmp_path)
    (root / ".git/hooks/pre-commit").symlink_to(PACKAGE / "scripts/main-worktree-guard")
    result = commit(root, EXAMPLE_AGENT="yes")
    assert result.returncode == 1 and "unable to verify" in result.stderr


def test_isolated_copied_package_installs_and_runs_direct_hook(tmp_path):
    copied = tmp_path / "standalone package"
    shutil.copytree(
        PACKAGE,
        copied,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    root = repository(tmp_path)
    config = config_for(root, tmp_path)
    config["hooks"][0]["source"] = str(copied / "scripts/main-worktree-guard")
    filename = tmp_path / "install.json"
    filename.write_text(json.dumps(config))
    result = subprocess.run(
        [str(copied / "scripts/git-hooks"), "--config", str(filename)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert commit(root, EXAMPLE_AGENT="yes").returncode == 1
