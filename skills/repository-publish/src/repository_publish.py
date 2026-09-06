"""Run a writer in an isolated Git worktree; advance progress only after publication."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath


class Failure(RuntimeError):
    """An operation failed without advancing durable writer progress."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}", flush=True)


def git(
    root: Path, *args: str, check: bool = True, env: dict | None = None
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode:
        raise Failure(f"git {args[0]} failed (exit {result.returncode})")
    return result


def text(root: Path, *args: str) -> str:
    return git(root, *args).stdout.decode().strip()


def absolute(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def paths(value: str) -> list[str]:
    result = value.split()
    if not result:
        raise Failure("at least one owned path is required")
    for item in result:
        p = PurePosixPath(item)
        if p.is_absolute() or ".." in p.parts or ".git" in p.parts or item.startswith("-"):
            raise Failure("owned paths must be relative literal paths outside Git metadata")
        if item in {".", ""} or str(p) != item.rstrip("/"):
            raise Failure("owned paths must be normalized and cannot select the whole repository")
    return [item.rstrip("/") for item in result]


def owned(path: str, selected: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in selected)


def changed(root: Path) -> list[tuple[str, str]]:
    records = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split(
        b"\0"
    )
    output = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        status, name = record[:2].decode(), os.fsdecode(record[3:])
        output.append((status, name))
        if "R" in status or "C" in status:
            if i >= len(records) or not records[i]:
                raise Failure("incomplete rename status")
            output.append((status, os.fsdecode(records[i])))
            i += 1
    return output


def check_ownership(root: Path, selected: list[str]) -> list[str]:
    changes = changed(root)
    for status, name in changes:
        if status != "??" and not owned(name, selected):
            raise Failure("writer changed a tracked path outside its ownership")
    return [name for _, name in changes if owned(name, selected)]


@contextlib.contextmanager
def lock(path: Path, wait: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def state_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise Failure("state directory must not be a symlink")
    output = []
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Failure("state contains a symlink or special file")
        if path.is_file():
            output.append(path)
    return output


def promote(staged: Path, durable: Path) -> None:
    files = state_files(staged)
    state_files(durable)
    for source in files:
        atomic_write(durable / source.relative_to(staged), source.read_bytes())


def command(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Failure("policy command must be a JSON argument array") from exc
    if (
        not isinstance(result, list)
        or not result
        or not all(isinstance(x, str) and x for x in result)
    ):
        raise Failure("policy command must be a nonempty JSON string array")
    return result


def policy(argv: list[str] | None, root: Path, env: dict, *, capture: bool = False) -> str:
    if not argv:
        return ""
    replacement = {
        "{worktree}": str(root),
        "{repository}": env["REPOSITORY_PUBLISH_REPOSITORY"],
        "{state}": env.get("REPOSITORY_PUBLISH_STATE", ""),
    }
    args = []
    for arg in argv:
        for key, value in replacement.items():
            arg = arg.replace(key, value)
        args.append(arg)
    proc = subprocess.run(
        args, cwd=root, env=env, check=False, stdout=subprocess.PIPE if capture else None
    )
    if proc.returncode:
        raise Failure(f"private policy command failed (exit {proc.returncode})")
    return proc.stdout.decode() if capture else ""


def checked_policy(argv: list[str] | None, root: Path, env: dict, *, capture=False) -> str:
    if not argv:
        return ""
    before = (text(root, "rev-parse", "HEAD"), git(root, "diff", "--cached", "--raw", "-z").stdout)
    result = policy(argv, root, env, capture=capture)
    after = (text(root, "rev-parse", "HEAD"), git(root, "diff", "--cached", "--raw", "-z").stdout)
    if after != before or git(root, "diff", "--quiet", check=False).returncode != 0:
        raise Failure("policy command modified tracked repository content")
    return result


def lfs_objects(root: Path, revision: str, selected: list[str]) -> tuple[list[str], list[str]]:
    parent = git(root, "rev-parse", "--verify", revision + "^", check=False)
    base = (
        parent.stdout.decode().strip()
        if parent.returncode == 0
        else text(root, "hash-object", "-t", "tree", "/dev/null")
    )
    changed_names = git(
        root,
        "--literal-pathspecs",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        "--diff-filter=AM",
        base,
        revision,
        "--",
        *selected,
    ).stdout
    names, oids = [], []
    for raw in changed_names.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        size = int(text(root, "cat-file", "-s", f"{revision}:{name}"))
        if size > 1024:
            continue
        content = git(root, "cat-file", "blob", f"{revision}:{name}").stdout
        if not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            continue
        match = re.search(rb"^oid sha256:([0-9a-f]{64})$", content, re.MULTILINE)
        if not match or not re.search(rb"^size [0-9]+$", content, re.MULTILINE):
            raise Failure("invalid LFS pointer")
        names.append(name)
        oids.append(match[1].decode())
    return names, sorted(set(oids))


def verify_lfs(root: Path, remote: str, revision: str, selected: list[str]) -> None:
    names, oids = lfs_objects(root, revision, selected)
    if not oids:
        return
    log(f"verifying {len(oids)} pushed LFS object(s)")
    # --refetch forces a remote transfer even when a local object already exists.
    # Do not override lfs.storage through `git -c`: the file transport passes
    # that setting to the remote helper and changes its object location too.
    for attempt in range(2):
        # An include list cannot represent commas or glob metacharacters as
        # literal filenames. In that uncommon case fetch the revision's objects.
        include = "" if any(re.search(r"[,\[\]*?]", name) for name in names) else ",".join(names)
        downloaded = git(
            root,
            "-c",
            "lfs.fetchexclude=",
            "-c",
            f"lfs.fetchinclude={include}",
            "-c",
            "lfs.skipdownloaderrors=false",
            "lfs",
            "fetch",
            "--refetch",
            remote,
            revision,
            check=False,
        )
        if downloaded.returncode == 0:
            log("all pushed LFS objects verified servable")
            return
        if attempt == 0:
            log("LFS download failed; re-uploading referenced objects once")
            git(root, "lfs", "push", "--object-id", remote, *oids, check=False)
    raise Failure(
        "pushed LFS objects cannot be downloaded after one repair upload: " + ", ".join(oids)
    )


def fetch(root: Path, remote: str, branch: str) -> str:
    git(root, "fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return f"refs/remotes/{remote}/{branch}"


def push_existing(
    root: Path,
    args,
    selected: list[str],
    env: dict,
    pending: Path | None = None,
    message: list[str] | None = None,
) -> str:
    if text(root, "branch", "--show-current") != args.expected_branch:
        raise Failure("refusing to publish an unexpected branch")
    with lock(args.publish_lock, args.lock_timeout) as acquired:
        if not acquired:
            raise Failure("timed out waiting for publication lock")
        for attempt in range(args.attempts):
            log(f"publish attempt {attempt + 1}/{args.attempts}")
            try:
                upstream = fetch(root, args.remote, args.branch)
            except Failure:
                if attempt + 1 < args.attempts:
                    time.sleep(args.retry_delay * (attempt + 1))
                continue
            rebase = git(root, "rebase", upstream, check=False)
            if rebase.returncode:
                git(root, "rebase", "--abort", check=False)
                raise Failure("rebase conflict; progress was not advanced", 4)
            try:
                checked_policy(args.validate, root, env)
            except Failure as exc:
                raise Failure(str(exc), 3) from exc
            if message and text(root, "rev-list", "--count", upstream + "..HEAD") != "0":
                result = checked_policy(message, root, env, capture=True)
                if not result.strip():
                    raise Failure("message command returned an empty commit message")
                proc = subprocess.run(
                    ["git", "-C", str(root), "commit", "--quiet", "--amend", "-F", "-"],
                    input=result.encode(),
                    capture_output=True,
                    env=env,
                    check=False,
                )
                if proc.returncode:
                    raise Failure("commit message refresh failed")
            revision = text(root, "rev-parse", "HEAD")
            if selected:
                committed = git(
                    root, "diff", "--no-renames", "--name-only", "-z", upstream, revision
                ).stdout.split(b"\0")
                if any(not owned(os.fsdecode(name), selected) for name in committed if name):
                    raise Failure("commit includes a path outside its ownership")
            if pending:
                atomic_write(
                    pending, json.dumps({"revision": revision, "paths": selected}).encode()
                )
            pushed = git(
                root, "push", "--quiet", args.remote, f"HEAD:refs/heads/{args.branch}", check=False
            )
            if pushed.returncode == 0:
                verify_lfs(root, args.remote, revision, selected)
                # Update the local tracking reference used by persistent callers
                # to distinguish already-published commits on their next run.
                fetch(root, args.remote, args.branch)
                return revision
            if attempt + 1 < args.attempts:
                time.sleep(args.retry_delay * (attempt + 1))
        raise Failure("publication failed after configured retry attempts")


def prepare_args(args):
    args.repo = absolute(args.repo)
    if not (args.repo / ".git").exists():
        raise Failure("repository must be a Git checkout")
    args.selected = paths(args.paths) if args.paths else []
    args.validate = command(args.validate_command)
    args.message = command(args.message_command)
    args.scratch = absolute(args.scratch)
    args.publish_lock = absolute(args.publish_lock)
    if args.attempts < 1 or args.retry_delay < 0 or args.lock_timeout < 0:
        raise Failure("invalid retry or lock settings")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.task):
        raise Failure("task must be a simple identifier")
    key = args.task + "-" + hashlib.sha256(os.fsencode(args.repo)).hexdigest()[:12]
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    args.state_dir = args.state_dir or str(state_root / "repository-publish" / key)
    args.lock = args.lock or str(cache_root / "repository-publish" / (key + ".lock"))
    for value in (args.worktree_env, args.state_env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) or value in {
            "HOME",
            "PATH",
            "CODEX_HOME",
        }:
            raise Failure("invalid writer environment variable")
    if args.remote.startswith("-") or args.branch.startswith("-"):
        raise Failure("remote and branch cannot be options")
    git(args.repo, "check-ref-format", "refs/heads/" + args.branch)
    git(args.repo, "remote", "get-url", args.remote)
    return args


def transaction(args) -> int:
    if not args.selected or not args.subject or not args.writer:
        raise Failure("--paths, --subject and a writer command after -- are required")
    durable = absolute(args.state_dir)
    if durable == args.repo or args.repo in durable.parents:
        raise Failure("durable state must be outside the checkout")
    if args.scratch == args.repo or args.repo in args.scratch.parents:
        raise Failure("scratch must be outside the checkout")
    args.scratch.mkdir(parents=True, exist_ok=True)
    durable.mkdir(parents=True, exist_ok=True)
    pending = durable.with_name(durable.name + ".publish-pending.json")
    with lock(absolute(args.lock), 0) as acquired:
        if not acquired:
            log("another writer for this task is running; exiting")
            return 0
        upstream = fetch(args.repo, args.remote, args.branch)
        if pending.exists():
            record = json.loads(pending.read_text())
            if not isinstance(record, dict):
                raise Failure("invalid pending publication record")
            revision = record["revision"]
            if (
                not isinstance(revision, str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", revision)
                or not isinstance(record["paths"], list)
                or not all(isinstance(item, str) for item in record["paths"])
            ):
                raise Failure("invalid pending publication record")
            paths(" ".join(record["paths"]))
            if (
                git(
                    args.repo, "merge-base", "--is-ancestor", revision, upstream, check=False
                ).returncode
                == 0
            ):
                verify_lfs(args.repo, args.remote, revision, record["paths"])
            pending.unlink()
        run = Path(tempfile.mkdtemp(prefix=f"publish-{args.task}-", dir=args.scratch))
        worktree, staged = run / "worktree", run / "state"
        branch = "publish/" + args.task + "-" + uuid.uuid4().hex
        args.expected_branch = branch
        try:
            git(
                args.repo, "worktree", "add", "--no-checkout", "-b", branch, str(worktree), upstream
            )
            if args.sparse and not args.validate:
                git(worktree, "sparse-checkout", "set", "--cone", "--", *paths(args.sparse))
            else:
                git(worktree, "sparse-checkout", "disable")
            git(worktree, "checkout", "--quiet", branch)
            initial = text(worktree, "rev-parse", "HEAD")
            state_files(durable)
            shutil.copytree(durable, staged)
            env = dict(os.environ)
            env.update(
                {
                    args.worktree_env: str(worktree),
                    args.state_env: str(staged),
                    "REPOSITORY_PUBLISH_WORKTREE": str(worktree),
                    "REPOSITORY_PUBLISH_STATE": str(staged),
                    "REPOSITORY_PUBLISH_REPOSITORY": str(args.repo),
                    "REPOSITORY_PUBLISH_SUBJECT": args.subject,
                    "REPOSITORY_PUBLISH_AGENT": args.agent,
                }
            )
            log(f"txn/{args.task}: running writer")
            writer = args.writer[1:] if args.writer[0] == "--" else args.writer
            proc = subprocess.run(writer, cwd=worktree, env=env, check=False)
            if proc.returncode:
                raise Failure(f"writer exited {proc.returncode}; progress was not advanced")
            if text(worktree, "rev-parse", "HEAD") != initial:
                raise Failure("writer must not create commits or change the checkout revision")
            selected = check_ownership(worktree, args.selected)
            # Stage literal changed paths; unmatched declared paths are valid
            # for empty initial runs, and Git pathspec metacharacters stay literal.
            if selected:
                git(worktree, "--literal-pathspecs", "add", "--", *selected)
            checked_policy(args.validate, worktree, env)
            check_ownership(worktree, args.selected)
            difference = git(worktree, "diff", "--cached", "--quiet", check=False).returncode
            if difference not in {0, 1}:
                raise Failure("cannot inspect staged changes")
            if difference:
                message = (
                    checked_policy(args.message, worktree, env, capture=True)
                    if args.message
                    else args.subject
                )
                if not message.strip():
                    raise Failure("message command returned an empty commit message")
                proc = subprocess.run(
                    ["git", "-C", str(worktree), "commit", "--quiet", "-F", "-"],
                    input=message.encode(),
                    capture_output=True,
                    env=env,
                    check=False,
                )
                if proc.returncode:
                    raise Failure("commit failed")
                push_existing(worktree, args, args.selected, env, pending, args.message)
            else:
                log(f"txn/{args.task}: no content changes")
            promote(staged, durable)
            pending.unlink(missing_ok=True)
            log(f"txn/{args.task}: complete; state advanced")
            return 0
        finally:
            git(args.repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(args.repo, "branch", "-D", branch, check=False)
            shutil.rmtree(run, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "repository-publish"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--task", default="writer")
    p.add_argument("--paths", default="")
    p.add_argument("--sparse", default="")
    p.add_argument("--subject", default="")
    p.add_argument("--agent", default="")
    p.add_argument("--state-dir")
    p.add_argument("--lock")
    p.add_argument("--scratch", default=str(cache / "transactions"))
    p.add_argument("--publish-lock", default=str(cache / "publish.lock"))
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="main")
    p.add_argument("--attempts", type=int, default=5)
    p.add_argument("--retry-delay", type=float, default=2)
    p.add_argument("--lock-timeout", type=float, default=300)
    p.add_argument("--worktree-env", default="REPOSITORY_PUBLISH_WORKTREE")
    p.add_argument("--state-env", default="SYNC_STATE_DIR")
    p.add_argument("--validate-command")
    p.add_argument("--message-command")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--existing-worktree", action="store_true")
    p.add_argument("--expected-branch", default="")
    modes.add_argument("--verify-lfs", metavar="REVISION")
    p.add_argument("writer", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        prepare_args(args)
        env = dict(
            os.environ,
            REPOSITORY_PUBLISH_REPOSITORY=str(args.repo),
            REPOSITORY_PUBLISH_WORKTREE=str(args.repo),
            REPOSITORY_PUBLISH_SUBJECT=args.subject,
            REPOSITORY_PUBLISH_AGENT=args.agent,
        )
        if args.verify_lfs:
            verify_lfs(args.repo, args.remote, args.verify_lfs, args.selected)
        elif args.existing_worktree:
            if not args.expected_branch or not (args.repo / ".git").is_file():
                raise Failure("existing publication requires a linked worktree and expected branch")
            push_existing(args.repo, args, args.selected, env, message=args.message)
        else:
            return transaction(args)
        return 0
    except (Failure, OSError, ValueError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, Failure) and args.existing_worktree else 1


if __name__ == "__main__":
    raise SystemExit(main())
