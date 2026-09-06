"""Explicit local moves with preflight checks, shared locks and bounded services."""

from __future__ import annotations

import contextlib
import fcntl
import glob
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .paths import Layout


class MigrationError(RuntimeError):
    pass


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def ancestor(path: Path) -> Path:
    while not path.exists():
        if path == path.parent:
            raise MigrationError("no existing ancestor")
        path = path.parent
    return path


class Migrator:
    def __init__(self, layout: Layout):
        self.layout = layout
        self.config = layout.config.get("migration", {})
        self.root = layout.root()
        if not self.root.is_absolute() or self.root == Path(self.root.anchor):
            raise MigrationError("migration root must be an absolute non-root directory")
        if self.root.is_symlink() or (exists(self.root) and not self.root.is_dir()):
            raise MigrationError("migration root must be a real directory")
        self.environment = dict(os.environ)
        for name, value in self.config.get("environment", {}).items():
            self.environment.setdefault(name, str(self.path(value)))

    def path(self, value: str, *, name: str = "") -> Path:
        return self.layout.expand(value.replace("{name}", name).replace("{uid}", str(os.getuid())))

    def command(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        argv = [
            str(self.path(arg)) if "{" in arg or arg.startswith("~") else arg for arg in command
        ]
        result = subprocess.run(
            argv,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=self.config.get("command_timeout", 30),
            check=False,
        )
        if check and result.returncode:
            raise MigrationError(f"configured command failed with exit {result.returncode}")
        return result

    def lock_pairs(self) -> list[tuple[Path, Path]]:
        pairs = [
            (self.path(row["legacy"]), self.path(row["current"]))
            for row in self.config.get("locks", [])
        ]
        if not pairs:
            raise MigrationError("migration requires an explicit complete writer lock set")
        seen: set[Path] = set()
        for old, new in pairs:
            if (
                not old.is_absolute()
                or not new.is_relative_to(self.root)
                or old.is_relative_to(self.root)
                or ".." in new.parts
            ):
                raise MigrationError("lock locations must bracket the runtime root")
            if old in seen or new in seen:
                raise MigrationError("duplicate lock location")
            seen.update((old, new))
            if old.is_symlink() or new.is_symlink():
                raise MigrationError("lock symlinks are not supported")
            if exists(old) and not old.is_file() or exists(new) and not new.is_file():
                raise MigrationError("lock must be a regular file")
            if exists(old) and exists(new) and not os.path.samefile(old, new):
                raise MigrationError(
                    "existing old and new locks have different inodes; no files changed"
                )
            if ancestor(old.parent).stat().st_dev != ancestor(self.root.parent).stat().st_dev:
                raise MigrationError("shared migration locks require one filesystem")
        return pairs

    def plan(self) -> dict:
        self.lock_pairs()
        actions, skipped = [], []
        for item in self.config.get("items", []):
            kind = item["kind"]
            if kind not in {"move", "contents", "glob", "directories", "worktree"}:
                raise MigrationError("unsupported migration item")
            source = self.path(item["source"])
            if kind in {"glob", "directories"}:
                sources = [
                    Path(value)
                    for value in sorted(glob.glob(str(source)))
                    if Path(value).name not in item.get("exclude", [])
                ]
                if kind == "directories":
                    sources = [
                        value for value in sources if value.is_dir() and not value.is_symlink()
                    ]
            else:
                sources = [source]
            for selected in sources:
                destination = self.path(item["destination"], name=selected.name)
                entries = [(selected, destination)]
                if kind in {"contents", "directories"}:
                    if selected.is_symlink():
                        raise MigrationError("content source must not be a symlink")
                    entries = (
                        [(entry, destination / entry.name) for entry in sorted(selected.iterdir())]
                        if selected.is_dir()
                        else []
                    )
                for src, dst in entries:
                    if not exists(src):
                        skipped.append(str(src))
                        continue
                    if not dst.is_relative_to(self.root) or dst == self.root or ".." in dst.parts:
                        raise MigrationError(
                            "every move destination must be inside the runtime root"
                        )
                    if src == dst or src in dst.parents or dst in src.parents:
                        raise MigrationError("overlapping source and destination")
                    if exists(dst):
                        raise MigrationError(f"destination already exists: {dst}")
                    if ancestor(dst.parent).resolve() != ancestor(dst.parent).absolute():
                        raise MigrationError("destination ancestor is a symlink")
                    if src.lstat().st_dev != ancestor(self.root.parent).stat().st_dev:
                        raise MigrationError("cross-filesystem moves are not supported")
                    record = {
                        "kind": "worktree" if kind == "worktree" else "move",
                        "source": str(src),
                        "destination": str(dst),
                    }
                    if kind == "worktree":
                        repository = self.path(item["repository"])
                        result = self.command(
                            ["git", "-C", str(repository), "worktree", "list", "--porcelain"]
                        )
                        blocks = result.stdout.strip().split("\n\n")
                        match = next(
                            (
                                block
                                for block in blocks
                                if block.splitlines()[0] == "worktree " + str(src.resolve())
                            ),
                            None,
                        )
                        if not match or any(
                            line.startswith("locked") for line in match.splitlines()
                        ):
                            raise MigrationError("worktree is unregistered or locked")
                        record["repository"] = str(repository)
                    if item.get("service"):
                        record["service"] = item["service"]
                    actions.append(record)
        sources, destinations = set(), set()
        for action in actions:
            src, dst = Path(action["source"]), Path(action["destination"])
            if src in sources or dst in destinations:
                raise MigrationError("duplicate move entry")
            if any(previous in src.parents or src in previous.parents for previous in sources):
                raise MigrationError("overlapping move sources")
            if any(previous in dst.parents or dst in previous.parents for previous in destinations):
                raise MigrationError("overlapping move destinations")
            sources.add(src)
            destinations.add(dst)
        return {
            "root": str(self.root),
            "activate_root": not self.root.exists(),
            "actions": actions,
            "skipped_missing": skipped,
            "services": sorted({row["service"] for row in actions if "service" in row}),
            "after_commands": self.config.get("after_commands", []),
        }

    @contextlib.contextmanager
    def locks(self, stage: Path):
        handles = []
        try:
            for old, current in self.lock_pairs():
                target = stage / current.relative_to(self.root)
                selected = old if exists(old) else current if exists(current) else old
                selected.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(selected, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                handles.append(fd)
                end = time.monotonic() + self.config.get("lock_timeout", 600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            raise MigrationError("timed out waiting for a writer lock")
                        time.sleep(0.05)
                for alias in (old, target):
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    if not exists(alias):
                        os.link(selected, alias)
                    if not os.path.samefile(selected, alias):
                        raise MigrationError(
                            "lock changed during acquisition; no migration performed"
                        )
            yield
        finally:
            for fd in reversed(handles):
                os.close(fd)

    def apply(self) -> dict:
        proposal = self.plan()
        initial = not self.root.exists()
        self.root.parent.mkdir(parents=True, exist_ok=True)
        stage = (
            Path(tempfile.mkdtemp(prefix=".runtime-layout-", dir=self.root.parent))
            if initial
            else self.root
        )
        moved, stopped = [], []
        activated = False
        try:
            with self.locks(stage):
                # Refresh sources only after all selected writers are excluded.
                proposal = self.plan()
                if bool(proposal["activate_root"]) != initial:
                    raise MigrationError("runtime activation changed while waiting for locks")
                try:
                    for name in proposal["services"]:
                        service = self.config["services"][name]
                        status = self.command(service["active"], check=False).returncode
                        if status == 0:
                            stopped.append(service)
                            self.command(service["stop"])
                        elif status not in service.get("inactive_codes", [3]):
                            raise MigrationError("service status is unknown")
                    for relative in self.config.get("directories", []):
                        path = stage / relative
                        if not path.is_relative_to(stage) or ".." in Path(relative).parts:
                            raise MigrationError("invalid directory path")
                        path.mkdir(parents=True, exist_ok=True)
                    for relative in self.config.get("private_directories", ["."]):
                        path = stage / relative
                        if ".." in Path(relative).parts or Path(relative).is_absolute():
                            raise MigrationError("invalid private directory path")
                        path.mkdir(parents=True, exist_ok=True)
                        path.chmod(0o700)
                    for action in proposal["actions"]:
                        src = Path(action["source"])
                        dst = stage / Path(action["destination"]).relative_to(self.root)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if exists(dst):
                            raise MigrationError("destination appeared after preflight")
                        if action["kind"] == "worktree":
                            self.command(
                                [
                                    "git",
                                    "-C",
                                    action["repository"],
                                    "worktree",
                                    "move",
                                    str(src),
                                    str(dst),
                                ]
                            )
                        else:
                            os.rename(src, dst)
                        moved.append((action, dst))
                    if initial:
                        if exists(self.root):
                            raise MigrationError("runtime root appeared after preflight")
                        os.rename(stage, self.root)
                        activated = True
                    # Renaming the containing directory needs Git's registered path repaired.
                    for item in self.config.get("items", []):
                        if item["kind"] == "worktree":
                            destination = self.path(item["destination"])
                            if destination.exists():
                                self.command(
                                    [
                                        "git",
                                        "-C",
                                        str(self.path(item["repository"])),
                                        "worktree",
                                        "repair",
                                        str(destination),
                                    ]
                                )
                    for link in self.config.get("symlinks", []):
                        path = self.path(link["path"])
                        if path.is_symlink() and os.readlink(path).startswith(
                            str(self.path(link["old_prefix"]))
                        ):
                            descriptor, temporary = tempfile.mkstemp(
                                prefix=".runtime-layout-link-", dir=path.parent
                            )
                            os.close(descriptor)
                            os.unlink(temporary)
                            try:
                                os.symlink(self.path(link["target"]), temporary)
                                os.replace(temporary, path)
                            finally:
                                if os.path.lexists(temporary):
                                    os.unlink(temporary)
                    for command in self.config.get("after_commands", []):
                        self.command(command)
                    for value in self.config.get("empty_directories", []):
                        try:
                            self.path(value).rmdir()
                        except OSError:
                            pass
                except Exception:
                    if not activated:
                        for action, destination in reversed(moved):
                            source = Path(action["source"])
                            if exists(source):
                                raise MigrationError(
                                    "rollback source conflict; inspect retained staging directory"
                                )
                            source.parent.mkdir(parents=True, exist_ok=True)
                            if action["kind"] == "worktree":
                                self.command(
                                    [
                                        "git",
                                        "-C",
                                        action["repository"],
                                        "worktree",
                                        "move",
                                        str(destination),
                                        str(source),
                                    ]
                                )
                            else:
                                os.rename(destination, source)
                    raise
                finally:
                    restart_failed = False
                    for service in reversed(stopped):
                        try:
                            self.command(service["start"])
                            if self.command(service["active"], check=False).returncode != 0:
                                restart_failed = True
                        except (OSError, RuntimeError, subprocess.SubprocessError):
                            restart_failed = True
                    if restart_failed:
                        raise MigrationError(
                            "one or more services failed to restart; every stopped service was attempted"
                        )
        finally:
            if initial and stage.exists():
                # Never recursively delete retained payload after a failed recovery.
                for pair in self.config.get("locks", []):
                    old = self.path(pair["legacy"])
                    alias = stage / self.path(pair["current"]).relative_to(self.root)
                    if exists(alias) and exists(old) and os.path.samefile(alias, old):
                        alias.unlink()
                for directory in sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if directory.is_dir() and not directory.is_symlink():
                        try:
                            directory.rmdir()
                        except OSError:
                            pass
                try:
                    stage.rmdir()
                except OSError:
                    pass
        return {**proposal, "applied": True, "moved": len(moved)}
