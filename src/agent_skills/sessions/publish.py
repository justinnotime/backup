"""Publication backends; none of them push or deploy."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .audit import GIT_CRYPT_MAGIC
from .manifest import Manifest
from .model import PublicationPlan


class PublishError(RuntimeError):
    pass


def _below(path: str, root: str) -> str | None:
    value = PurePosixPath(path)
    base = PurePosixPath(root)
    if value.is_absolute() or ".." in value.parts:
        raise PublishError("publication path escapes the output root")
    try:
        relative = value.relative_to(base)
    except ValueError:
        return None
    if relative == PurePosixPath("."):
        raise PublishError("publication path names an output subtree directory")
    return relative.as_posix()


def _check_plain_ancestors(root: Path, relative: str) -> None:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("owned output subtree has a symbolic-link ancestor")
        if current.exists() and not current.is_dir():
            raise PublishError("owned output subtree has a non-directory ancestor")


def _make_plain_parents(root: Path, relative: str, created: list[Path]) -> None:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("owned output subtree has a symbolic-link ancestor")
        if current.exists():
            if not current.is_dir():
                raise PublishError("owned output subtree has a non-directory ancestor")
            continue
        current.mkdir()
        created.append(current)


def _build_staged_tree(
    root: Path, subtree: str, plan: PublicationPlan
) -> tuple[Path, Path]:
    target = root / subtree
    _check_plain_ancestors(root, subtree)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.agent-session-stage-", dir=root)
    )
    try:
        if target.is_symlink():
            raise PublishError("owned output subtree is not a plain directory")
        if target.exists():
            if not target.is_dir():
                raise PublishError("owned output subtree is not a plain directory")
            if any(path.is_symlink() for path in target.rglob("*")):
                raise PublishError("owned output subtree contains a symbolic link")
            shutil.copytree(target, stage, dirs_exist_ok=True)
        for removal in plan.removals:
            relative = _below(removal.relative_path, subtree)
            if relative is None:
                continue
            destination = stage / relative
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for planned in plan.writes:
            relative = _below(planned.relative_path, subtree)
            if relative is None:
                continue
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(planned.content)
        return target, stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def publish_filesystem(manifest: Manifest, plan: PublicationPlan) -> None:
    """Swap each owned subtree atomically and roll back cross-tree failures."""
    root = manifest.output.repository_root
    if root.is_symlink() or not root.is_dir():
        raise PublishError("output repository root must be an existing plain directory")
    prepared = []
    backups: list[tuple[Path, Path | None]] = []
    created_parents: list[Path] = []
    try:
        for subtree in manifest.publisher.owned_subtrees:
            prepared.append(_build_staged_tree(root, subtree, plan))
        for target, stage in prepared:
            _make_plain_parents(
                root,
                target.relative_to(root).as_posix(),
                created_parents,
            )
            backup = None
            if target.exists():
                backup = target.with_name(
                    f".{target.name}.agent-session-old-{os.getpid()}"
                )
                if backup.exists():
                    raise PublishError("publication rollback path already exists")
                os.replace(target, backup)
            try:
                os.replace(stage, target)
            except Exception:
                if backup is not None:
                    os.replace(backup, target)
                raise
            backups.append((target, backup))
    except Exception as exc:
        for target, backup in reversed(backups):
            if backup is None:
                if target.exists():
                    shutil.rmtree(target)
            elif backup.exists():
                if target.exists():
                    shutil.rmtree(target)
                os.replace(backup, target)
        for _target, stage in prepared:
            if stage.exists():
                shutil.rmtree(stage)
        for directory in reversed(created_parents):
            try:
                directory.rmdir()
            except OSError:
                pass
        if isinstance(exc, PublishError):
            raise
        raise PublishError("atomic publication failed") from exc
    for _target, backup in backups:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def _run_git(arguments: list[str], *, cwd: Path) -> str:
    process = subprocess.run(
        ["git", *arguments], cwd=cwd, text=True, capture_output=True, check=False
    )
    if process.returncode:
        raise PublishError("git worktree preparation failed")
    return process.stdout


def _refuse_ciphertext(root: Path, subtrees: tuple[str, ...]) -> None:
    for subtree in subtrees:
        _check_plain_ancestors(root, subtree)
        directory = root / subtree
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise PublishError("git worktree output subtree is a symbolic link")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise PublishError("git worktree output contains a symbolic link")
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                if handle.read(len(GIT_CRYPT_MAGIC)) == GIT_CRYPT_MAGIC:
                    raise PublishError("git worktree contains ciphertext output")


def _plain_parent(root: Path, relative: str) -> Path:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("git-crypt key link parent is a symbolic link")
        if current.exists() and not current.is_dir():
            raise PublishError("git-crypt key link parent is not a directory")
        current.mkdir(exist_ok=True)
    return current


def prepare_git_worktree(
    manifest: Manifest, plan: PublicationPlan, destination: Path
) -> tuple[str, ...]:
    """Prepare and stage an explicit throwaway worktree, without commit or push.

    The caller owns the resulting worktree and removes it with
    ``git worktree remove <destination>`` after inspection or publication.
    """
    repository = manifest.output.repository_root
    if destination.exists():
        raise PublishError("throwaway worktree destination already exists")
    repository_real = repository.resolve(strict=True)
    destination_lexical = Path(os.path.abspath(destination))
    destination_real = destination_lexical.resolve(strict=False)
    for candidate in (destination_lexical, destination_real):
        try:
            candidate.relative_to(repository_real)
        except ValueError:
            continue
        raise PublishError("throwaway worktree must be outside its source repository")
    _run_git(
        ["worktree", "add", "--detach", os.fspath(destination), "HEAD"], cwd=repository
    )
    try:
        if manifest.publisher.encryption == "git-crypt":
            if (
                manifest.publisher.key_link_source is None
                or manifest.publisher.key_link_target is None
            ):
                raise PublishError("git-crypt publication requires a key link")
            if not manifest.publisher.key_link_source.is_file():
                raise PublishError("git-crypt key source is missing")
            link = destination / manifest.publisher.key_link_target
            _plain_parent(destination, manifest.publisher.key_link_target)
            os.symlink(manifest.publisher.key_link_source, link)
        _refuse_ciphertext(destination, manifest.publisher.owned_subtrees)
        worktree_manifest = replace(
            manifest,
            output=replace(manifest.output, repository_root=destination),
            publisher=replace(manifest.publisher, strategy="filesystem-atomic"),
        )
        publish_filesystem(worktree_manifest, plan)
        _run_git(["add", "--", *manifest.publisher.owned_subtrees], cwd=destination)
        staged = tuple(
            line
            for line in _run_git(
                ["diff", "--cached", "--name-only", "-z"], cwd=destination
            ).split("\0")
            if line
        )
        for name in staged:
            if not any(
                name == root or name.startswith(root + "/")
                for root in manifest.publisher.owned_subtrees
            ):
                raise PublishError("git staged a path outside the owned subtrees")
        return staged
    except Exception:
        _run_git(
            ["worktree", "remove", "--force", os.fspath(destination)], cwd=repository
        )
        raise
