from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.audit import GIT_CRYPT_MAGIC
from agent_skills.sessions.manifest import load_manifest
from agent_skills.sessions.model import PlannedFile, PublicationPlan
from agent_skills.sessions.publish import (
    PublishError,
    prepare_git_worktree,
    publish_filesystem,
)

INDEX = b"# Index\n\n- Managed-By: agent-session-extraction/v1\n- View: index\n"


def git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", *arguments], cwd=root, text=True, capture_output=True, check=True
    )
    return process.stdout


def git_bytes(root: Path, *arguments: str) -> bytes:
    process = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=True
    )
    return process.stdout


def resolved_git_dir(root: Path) -> Path:
    path = Path(git(root, "rev-parse", "--git-dir").strip())
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=True)


def resolved_git_common_dir(root: Path) -> Path:
    path = Path(git(root, "rev-parse", "--git-common-dir").strip())
    if not path.is_absolute():
        path = root / path
    return path.resolve(strict=True)


class FilesystemPublisherTest(unittest.TestCase):
    def test_publishes_only_owned_subtrees_and_preserves_external_dirt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            (output / "unowned.txt").write_text("keep", encoding="utf-8")
            data = manifest_data(source, output, publisher="filesystem-atomic")
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            plan = PublicationPlan(
                (
                    PlannedFile("History/README.md", INDEX, None, "index"),
                    PlannedFile("Prompts/README.md", INDEX, None, "index"),
                ),
                (),
            )
            publish_filesystem(manifest, plan)
            self.assertEqual((output / "unowned.txt").read_text(), "keep")
            self.assertEqual((output / "History/README.md").read_bytes(), INDEX)

    def test_refuses_symlinks_inside_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            history = output / "History"
            history.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (history / "2026-01").symlink_to(outside, target_is_directory=True)
            data = manifest_data(source, output, publisher="filesystem-atomic")
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            plan = PublicationPlan(
                (PlannedFile("History/2026-01/session.md", INDEX, None, "index"),),
                (),
            )
            with self.assertRaises(PublishError):
                publish_filesystem(manifest, plan)
            self.assertFalse((outside / "session.md").exists())

    def test_refuses_symbolic_link_ancestor_of_nested_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (output / "Raw").symlink_to(outside, target_is_directory=True)
            data = manifest_data(source, output, publisher="filesystem-atomic")
            data["output"]["history_directory"] = "Raw/History"
            data["output"]["prompt_directory"] = "Raw/Prompts"
            data["publisher"]["owned_subtrees"] = [
                "Raw/History",
                "Raw/Prompts",
            ]
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            plan = PublicationPlan(
                (PlannedFile("Raw/History/session.md", INDEX, None, "index"),),
                (),
            )
            with self.assertRaises(PublishError):
                publish_filesystem(manifest, plan)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_refuses_traversal_even_when_called_without_pipeline_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            output = root / "output"
            output.mkdir()
            data = manifest_data(source, output, publisher="filesystem-atomic")
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            plan = PublicationPlan(
                (PlannedFile("History/../escaped.md", INDEX, None, "index"),),
                (),
            )
            with self.assertRaises(PublishError):
                publish_filesystem(manifest, plan)
            self.assertFalse((output / "escaped.md").exists())


class GitWorktreePublisherTest(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()
        git(repository, "init", "-q")
        git(repository, "config", "user.name", "Synthetic Test")
        git(repository, "config", "user.email", "synthetic@example.invalid")
        (repository / "unowned.txt").write_text("base", encoding="utf-8")
        (repository / "History").mkdir()
        (repository / "Prompts").mkdir()
        (repository / "History/.keep").write_text("", encoding="utf-8")
        (repository / "History/existing.md").write_text(
            "synthetic existing plaintext", encoding="utf-8"
        )
        (repository / "Prompts/.keep").write_text("", encoding="utf-8")
        git(repository, "add", "unowned.txt", "History", "Prompts")
        git(repository, "commit", "-qm", "synthetic base")
        return repository

    @staticmethod
    def _synthetic_key(root: Path, key_name: str) -> Path:
        key = root / f"synthetic-{key_name}.key"
        if not key.exists():
            key.write_bytes(b"synthetic-key-material")
        return key

    def _configure_filter(
        self,
        repository: Path,
        root: Path,
        driver: str,
        *,
        encrypted: bool,
        key_name: str = "default",
        patterns: tuple[str, ...] = ("History/**", "Prompts/**"),
    ) -> Path:
        key = self._synthetic_key(root, key_name)
        key_target = resolved_git_dir(repository) / f"git-crypt/keys/{key_name}"
        key_target.parent.mkdir(parents=True)
        key_target.symlink_to(key)
        filter_script = root / f"{driver}-filter.py"
        filter_script.write_text(
            f"""#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

raw_git_dir = subprocess.check_output(
    ["git", "rev-parse", "--git-dir"], text=True
).strip()
git_dir = Path(raw_git_dir)
if not git_dir.is_absolute():
    git_dir = (Path.cwd() / git_dir).resolve(strict=True)
key = git_dir / {f"git-crypt/keys/{key_name}"!r}
if not key.is_symlink():
    raise SystemExit(2)
secret = key.read_bytes()
if not secret:
    raise SystemExit(3)
mask = secret[0]
magic = b"\\x00GITCRYPT\\x00"
data = sys.stdin.buffer.read()
if sys.argv[1] == "clean":
    output = magic + bytes(value ^ mask for value in data) if {encrypted!r} else data
elif sys.argv[1] == "smudge":
    if {encrypted!r} and not data.startswith(magic):
        raise SystemExit(4)
    body = data[len(magic):] if {encrypted!r} else data
    output = bytes(value ^ mask for value in body) if {encrypted!r} else body
else:
    raise SystemExit(5)
sys.stdout.buffer.write(output)
""",
            encoding="utf-8",
        )
        filter_script.chmod(0o755)
        command = shlex.quote(str(filter_script))
        clean_command = f"{command} clean"
        smudge_command = f"{command} smudge"
        git(repository, "config", f"filter.{driver}.clean", clean_command)
        git(repository, "config", f"filter.{driver}.smudge", smudge_command)
        git(repository, "config", f"filter.{driver}.required", "true")
        (repository / ".gitattributes").write_text(
            "".join(f"{pattern} filter={driver}\n" for pattern in patterns),
            encoding="utf-8",
        )
        git(repository, "add", ".gitattributes")
        git(repository, "add", "--renormalize", "History", "Prompts")
        git(repository, "commit", "-qm", "synthetic encryption attributes")
        return key

    def _git_crypt_manifest(
        self, root: Path, repository: Path, *, key_name: str = "default"
    ):
        source = root / "source"
        source.mkdir(exist_ok=True)
        key = self._synthetic_key(root, key_name)
        data = manifest_data(source, repository, publisher="git-worktree")
        data["publisher"]["encryption"] = "git-crypt"
        data["publisher"]["key_link"] = {
            "source": str(key),
            "target": f"git-crypt/keys/{key_name}",
        }
        manifest = load_manifest(
            write_manifest(root / "manifest.json", data),
            environ={"HOME": str(root)},
        )
        return manifest, key

    @staticmethod
    def _plan() -> PublicationPlan:
        return PublicationPlan(
            (
                PlannedFile("History/README.md", INDEX, None, "index"),
                PlannedFile("Prompts/README.md", INDEX, None, "index"),
            ),
            (),
        )

    def test_git_crypt_filters_encrypt_index_and_key_remains_a_link(self) -> None:
        for driver, key_name in (
            ("git-crypt", "default"),
            ("git-crypt-fixture", "fixture"),
        ):
            with (
                self.subTest(driver=driver, key_name=key_name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                repository = self._repository(root)
                self._configure_filter(
                    repository,
                    root,
                    driver,
                    encrypted=True,
                    key_name=key_name,
                )
                manifest, key = self._git_crypt_manifest(
                    root, repository, key_name=key_name
                )
                self.assertTrue(
                    git_bytes(repository, "show", ":History/existing.md").startswith(
                        GIT_CRYPT_MAGIC
                    )
                )
                (repository / "unowned.txt").write_text(
                    "dirty outside owned subtree", encoding="utf-8"
                )
                destination = root / "prepared"

                staged = prepare_git_worktree(manifest, self._plan(), destination)
                try:
                    self.assertEqual(
                        set(staged), {"History/README.md", "Prompts/README.md"}
                    )
                    for relative_path in staged:
                        blob = git_bytes(destination, "show", f":{relative_path}")
                        self.assertTrue(blob.startswith(GIT_CRYPT_MAGIC))
                        self.assertNotIn(INDEX, blob)
                    private_git_dir = resolved_git_dir(destination)
                    self.assertNotEqual(
                        private_git_dir, resolved_git_common_dir(destination)
                    )
                    link = private_git_dir / f"git-crypt/keys/{key_name}"
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), key)
                    self.assertEqual(key.read_bytes(), b"synthetic-key-material")
                    self.assertEqual((destination / "unowned.txt").read_text(), "base")
                    self.assertEqual(
                        (destination / "History/existing.md").read_text(),
                        "synthetic existing plaintext",
                    )
                finally:
                    git(
                        repository,
                        "worktree",
                        "remove",
                        "--force",
                        str(destination),
                    )

    @unittest.skipUnless(shutil.which("git-crypt"), "real git-crypt is unavailable")
    def test_real_git_crypt_unlocks_only_after_private_key_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            git_crypt = shutil.which("git-crypt")
            self.assertIsNotNone(git_crypt)
            subprocess.run([git_crypt or "", "init"], cwd=repository, check=True)
            (repository / ".gitattributes").write_text(
                "History/** filter=git-crypt diff=git-crypt\n"
                "Prompts/** filter=git-crypt diff=git-crypt\n",
                encoding="utf-8",
            )
            git(repository, "add", ".gitattributes")
            git(repository, "add", "--renormalize", "History", "Prompts")
            git(repository, "commit", "-qm", "real git-crypt attributes")
            key = root / "exported.key"
            subprocess.run(
                [git_crypt or "", "export-key", str(key)],
                cwd=repository,
                check=True,
            )
            source = root / "source"
            source.mkdir()
            data = manifest_data(source, repository, publisher="git-worktree")
            data["publisher"]["encryption"] = "git-crypt"
            data["publisher"]["key_link"] = {
                "source": str(key),
                "target": "git-crypt/keys/default",
            }
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            destination = root / "prepared"

            staged = prepare_git_worktree(manifest, self._plan(), destination)
            try:
                self.assertEqual(
                    set(staged), {"History/README.md", "Prompts/README.md"}
                )
                self.assertEqual(
                    (destination / "History/existing.md").read_text(),
                    "synthetic existing plaintext",
                )
                for relative_path in staged:
                    blob = git_bytes(destination, "show", f":{relative_path}")
                    self.assertTrue(blob.startswith(GIT_CRYPT_MAGIC))
                    self.assertNotIn(INDEX, blob)
                link = resolved_git_dir(destination) / "git-crypt/keys/default"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), key)
            finally:
                git(
                    repository,
                    "worktree",
                    "remove",
                    "--force",
                    str(destination),
                )

    def test_git_crypt_refuses_missing_filter_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            manifest, _key = self._git_crypt_manifest(root, repository)
            before = git(repository, "status", "--porcelain=v1")
            worktrees_before = git(repository, "worktree", "list", "--porcelain")
            destination = root / "prepared"

            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, self._plan(), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(git(repository, "status", "--porcelain=v1"), before)
            self.assertEqual(
                git(repository, "worktree", "list", "--porcelain"),
                worktrees_before,
            )
            self.assertFalse((repository / "History/README.md").exists())

    def test_git_crypt_checks_unchanged_tracked_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            (repository / "History/existing.md").write_text(
                "synthetic existing output", encoding="utf-8"
            )
            git(repository, "add", "History/existing.md")
            git(repository, "commit", "-qm", "synthetic existing output")
            self._configure_filter(
                repository,
                root,
                "git-crypt",
                encrypted=True,
                patterns=(
                    "History/.keep",
                    "Prompts/.keep",
                    "History/README.md",
                    "Prompts/README.md",
                ),
            )
            manifest, _key = self._git_crypt_manifest(root, repository)
            before = git(repository, "status", "--porcelain=v1")
            worktrees_before = git(repository, "worktree", "list", "--porcelain")
            destination = root / "prepared"

            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, self._plan(), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(git(repository, "status", "--porcelain=v1"), before)
            self.assertEqual(
                git(repository, "worktree", "list", "--porcelain"),
                worktrees_before,
            )

    def test_git_crypt_filter_name_must_match_key_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            self._configure_filter(
                repository,
                root,
                "git-crypt-wrong-name",
                encrypted=True,
                key_name="default",
            )
            manifest, _key = self._git_crypt_manifest(root, repository)
            worktrees_before = git(repository, "worktree", "list", "--porcelain")
            destination = root / "prepared"

            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, self._plan(), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(
                git(repository, "worktree", "list", "--porcelain"),
                worktrees_before,
            )

    def test_git_crypt_refuses_filter_that_leaves_plaintext_in_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            self._configure_filter(repository, root, "git-crypt", encrypted=False)
            manifest, _key = self._git_crypt_manifest(root, repository)
            before = git(repository, "status", "--porcelain=v1")
            worktrees_before = git(repository, "worktree", "list", "--porcelain")
            destination = root / "prepared"

            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, self._plan(), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(git(repository, "status", "--porcelain=v1"), before)
            self.assertEqual(
                git(repository, "worktree", "list", "--porcelain"),
                worktrees_before,
            )
            self.assertFalse((repository / "History/README.md").exists())

    def test_refuses_ciphertext_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            (repository / "History/encrypted.md").write_bytes(
                b"\x00GITCRYPT\x00synthetic"
            )
            git(repository, "add", "History/encrypted.md")
            git(repository, "commit", "-qm", "synthetic ciphertext")
            source = root / "source"
            source.mkdir()
            data = manifest_data(source, repository, publisher="git-worktree")
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            destination = root / "prepared"
            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, PublicationPlan((), ()), destination)
            self.assertFalse(destination.exists())

    def test_refuses_worktree_inside_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            source = root / "source"
            source.mkdir()
            manifest = load_manifest(
                write_manifest(
                    root / "manifest.json",
                    manifest_data(source, repository, publisher="git-worktree"),
                ),
                environ={"HOME": str(root)},
            )
            destination = repository / ".internal-worktree"
            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, PublicationPlan((), ()), destination)
            self.assertFalse(destination.exists())

    def test_prepare_rechecks_owned_output_against_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            source = root / "source"
            source.mkdir()
            manifest = load_manifest(
                write_manifest(
                    root / "manifest.json",
                    manifest_data(source, repository, publisher="git-worktree"),
                ),
                environ={"HOME": str(root)},
            )
            (repository / "History/.keep").write_text(
                "changed after inventory scan", encoding="utf-8"
            )
            worktrees_before = git(repository, "worktree", "list", "--porcelain")
            destination = root / "prepared"

            with self.assertRaises(PublishError):
                prepare_git_worktree(manifest, PublicationPlan((), ()), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(
                git(repository, "worktree", "list", "--porcelain"),
                worktrees_before,
            )


if __name__ == "__main__":
    unittest.main()
