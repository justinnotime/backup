from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from session_test_support import manifest_data, write_manifest

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
        (repository / "Prompts/.keep").write_text("", encoding="utf-8")
        git(repository, "add", "unowned.txt", "History", "Prompts")
        git(repository, "commit", "-qm", "synthetic base")
        return repository

    def test_prepares_key_link_and_stages_only_owned_subtrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            source = root / "source"
            source.mkdir()
            data = manifest_data(source, repository, publisher="git-worktree")
            key = root / "synthetic.key"
            key.write_text("not-a-real-key", encoding="utf-8")
            data["publisher"]["encryption"] = "git-crypt"
            data["publisher"]["key_link"] = {
                "source": str(key),
                "target": ".runtime/git-crypt.key",
            }
            manifest = load_manifest(
                write_manifest(root / "manifest.json", data),
                environ={"HOME": str(root)},
            )
            (repository / "unowned.txt").write_text(
                "dirty outside owned subtree", encoding="utf-8"
            )
            destination = root / "prepared"
            plan = PublicationPlan(
                (
                    PlannedFile("History/README.md", INDEX, None, "index"),
                    PlannedFile("Prompts/README.md", INDEX, None, "index"),
                ),
                (),
            )
            staged = prepare_git_worktree(manifest, plan, destination)
            try:
                self.assertEqual(
                    set(staged), {"History/README.md", "Prompts/README.md"}
                )
                link = destination / ".runtime/git-crypt.key"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), key)
                self.assertEqual((destination / "unowned.txt").read_text(), "base")
            finally:
                git(repository, "worktree", "remove", "--force", str(destination))

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


if __name__ == "__main__":
    unittest.main()
