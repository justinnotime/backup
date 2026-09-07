"""Install explicitly selected Git hook links and protect a configured checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from .install import InstallError, lock, parents_ok, path, string

GUARD_KEY = "runtimeinstall.mainGuard"


def git(repository, *arguments, missing_ok=()):
    environment = dict(os.environ)
    command = ["git"]
    if repository is not None:
        command += ["-C", str(repository)]
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            environment.pop(name, None)
    result = subprocess.run(
        command + list(arguments), env=environment, capture_output=True, check=False
    )
    if result.returncode and result.returncode not in missing_ok:
        raise InstallError("Git could not read or update the selected repository")
    return result.stdout


def guard_policy(value, *, installed=False):
    required = {"when_environment", "bypass_environment"}
    if installed:
        required.add("worktree")
    if not isinstance(value, dict) or set(value) != required:
        raise InstallError("invalid main-worktree guard policy")
    if (
        installed
        and Path(string(value["worktree"], "protected worktree")).is_absolute()
    ):
        raise InstallError(
            "guard worktree must be relative to the common Git directory"
        )
    for key in ("when_environment", "bypass_environment"):
        if value[key] is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", string(value[key])
        ):
            raise InstallError("invalid guard environment variable")
    return value


def main_guard():
    """A direct pre-commit hook; policy comes from this repository's local config."""
    try:
        policy = guard_policy(
            json.loads(git(None, "config", "--local", "--get", GUARD_KEY)),
            installed=True,
        )
        when, bypass = policy["when_environment"], policy["bypass_environment"]
        if (when and not os.environ.get(when)) or (bypass and os.environ.get(bypass)):
            return 0
        top = Path(
            os.fsdecode(git(None, "rev-parse", "--show-toplevel")).strip()
        ).resolve()
        common = Path(
            os.fsdecode(
                git(None, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).strip()
        )
        if (common / policy["worktree"]).resolve() == top:
            print(
                "FAIL: commit refused in the configured shared checkout; "
                "use a separate worktree",
                file=sys.stderr,
            )
            return 1
        return 0
    except (InstallError, OSError, ValueError, TypeError):
        print(
            "FAIL: unable to verify the configured main-worktree guard", file=sys.stderr
        )
        return 1


def snapshot(target):
    if target.is_symlink():
        return {"kind": "symlink", "target": os.readlink(target)}
    if not target.exists():
        return {"kind": "absent"}
    if not target.is_file():
        return {"kind": "other"}
    return {
        "kind": "file",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "mode": stat.S_IMODE(target.stat().st_mode),
    }


def plan(config):
    if (
        not isinstance(config, dict)
        or config.get("schema") != "runtime-install/v1"
        or config.get("kind") != "git-hooks"
    ):
        raise InstallError("invalid Git hook installation configuration")
    repository = path(config["repository"])
    common = Path(
        os.fsdecode(
            git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).strip()
    ).resolve()
    directory = Path(
        os.fsdecode(
            git(
                repository, "rev-parse", "--path-format=absolute", "--git-path", "hooks"
            )
        ).strip()
    ).resolve()
    approved = (
        path(config["hooks_directory"]).resolve()
        if "hooks_directory" in config
        else common / "hooks"
    )
    if directory != approved:
        raise InstallError(
            "custom hooks directory requires explicit hooks_directory configuration"
        )
    if not isinstance(config.get("hooks"), list) or not config["hooks"]:
        raise InstallError("at least one Git hook is required")
    operations = []
    for hook in config["hooks"]:
        name = string(hook["name"], "hook name")
        if not re.fullmatch(r"[a-z][a-z-]*", name):
            raise InstallError("invalid hook name")
        source = path(hook["source"]).resolve()
        if not source.is_file() or not os.access(source, os.X_OK):
            raise InstallError("configured hook source is not executable")
        target = directory / name
        parents_ok(target)
        before = snapshot(target)
        installed = before["kind"] == "symlink" and target.resolve() == source
        hashes = hook.get("replace_sha256", [])
        targets = hook.get("replace_targets", [])
        if not isinstance(hashes, list) or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in hashes
        ):
            raise InstallError("invalid replacement digest")
        if not isinstance(targets, list) or any(
            not isinstance(value, str) or not value for value in targets
        ):
            raise InstallError("invalid replacement link target")
        replace = (before["kind"] == "file" and before["sha256"] in hashes) or (
            before["kind"] == "symlink" and before["target"] in targets
        )
        action = (
            "link"
            if before["kind"] == "absent"
            else "replace"
            if replace and not installed
            else "keep"
        )
        operations.append(
            {
                "path": str(target),
                "source": str(source),
                "action": action,
                "managed": installed or action != "keep",
                "before": before,
            }
        )
    if len({item["path"] for item in operations}) != len(operations):
        raise InstallError("duplicate hook destination")
    policy = config.get("main_guard")
    if policy is not None:
        guard_policy(policy)
        if not any(Path(item["path"]).name == "pre-commit" for item in operations):
            raise InstallError("main_guard requires a configured pre-commit hook")
    path(config["lock"])
    parents_ok(path(config["backup_directory"]) / "backup")
    return repository, common, directory, operations


def replace_link(target, source):
    fd, temporary = tempfile.mkstemp(prefix=".runtime-install-hook-", dir=target.parent)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(os.path.relpath(source, target.parent), temporary)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def write_policy(repository, values):
    git(repository, "config", "--local", "--unset-all", GUARD_KEY, missing_ok=(1, 5))
    for value in values:
        git(repository, "config", "--local", "--add", GUARD_KEY, value)


def install(config, dry_run=False):
    repository, common, directory, operations = plan(config)
    if dry_run:
        print(json.dumps(operations, indent=2, sort_keys=True))
        return
    with lock(config["lock"]):
        repository, common, directory, operations = plan(config)
        old_policy = (
            os.fsdecode(
                git(
                    repository,
                    "config",
                    "--local",
                    "--null",
                    "--get-all",
                    GUARD_KEY,
                    missing_ok=(1,),
                )
            )
            .rstrip("\0")
            .split("\0")
        )
        if old_policy == [""]:
            old_policy = []
        policy = config.get("main_guard")
        manage_guard = policy is not None and any(
            Path(item["path"]).name == "pre-commit" and item["managed"]
            for item in operations
        )
        if manage_guard:
            protected = Path(
                os.fsdecode(git(repository, "rev-parse", "--show-toplevel")).strip()
            ).resolve()
            installed_policy = {
                **policy,
                "worktree": os.path.relpath(protected, common),
            }
            desired = [json.dumps(installed_policy, sort_keys=True)]
        else:
            desired = old_policy
        changes = [item for item in operations if item["action"] != "keep"]
        if not changes and old_policy == desired:
            print("OK no selected hook changes; existing hooks preserved")
            return
        backup_root = path(config["backup_directory"])
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = Path(
            tempfile.mkdtemp(prefix="git-hooks-before-install-", dir=backup_root)
        )
        for item in changes:
            target = Path(item["path"])
            if item["before"]["kind"] == "file":
                shutil.copy2(target, backup / target.name)
        (backup / "snapshot.json").write_text(
            json.dumps(
                {
                    "repository": str(repository),
                    "hooks_directory": str(directory),
                    "guard_key": GUARD_KEY,
                    "guard_values": old_policy,
                    "hooks": changes,
                },
                indent=2,
            )
            + "\n"
        )
        changed = []
        policy_touched = False
        created_directory = not directory.exists()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if old_policy != desired:
                policy_touched = True
                write_policy(repository, desired)
            for item in changes:
                target = Path(item["path"])
                if snapshot(target) != item["before"]:
                    raise InstallError("hook changed during installation")
                replace_link(target, item["source"])
                changed.append(item)
                if not target.is_symlink() or target.resolve() != Path(item["source"]):
                    raise InstallError("installed hook verification failed")
        except Exception:  # noqa: BLE001 - Restore all earlier changes after any installation failure.
            try:
                for item in reversed(changed):
                    target, before = Path(item["path"]), item["before"]
                    if before["kind"] == "file":
                        target.unlink()
                        shutil.copy2(backup / target.name, target)
                    elif before["kind"] == "symlink":
                        target.unlink()
                        os.symlink(before["target"], target)
                    else:
                        target.unlink()
                if policy_touched:
                    write_policy(repository, old_policy)
                if created_directory:
                    directory.rmdir()
            except (OSError, InstallError):
                raise InstallError(
                    "hook installation failed; inspect backup for incomplete restoration"
                ) from None
            raise InstallError(
                "hook installation failed; previous hooks and policy restored"
            ) from None
        preserved = sum(not item["managed"] for item in operations)
        print(
            f"OK applied configured Git hooks; {preserved} unrecognized hooks preserved; backup: {backup}"
        )


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments)
    try:
        from .config import load_config

        install(load_config(options.config), dry_run=options.dry_run)
        return 0
    except (InstallError, OSError, ValueError, TypeError, KeyError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, InstallError)
            else "Git hook installation configuration or operation failed"
        )
        print(f"FAIL: {detail}", file=sys.stderr)
        return 1
