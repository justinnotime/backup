from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SESSION_SKILL = REPOSITORY_ROOT / "skills" / "agent-session-extraction"


class SkillLayoutTests(unittest.TestCase):
    def test_session_wrappers_resolve_absolute_and_relative_skill_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            absolute_link = root / "absolute" / "agent-session-extraction"
            absolute_link.parent.mkdir()
            absolute_link.symlink_to(SESSION_SKILL, target_is_directory=True)

            relative_link = root / "relative" / "agent-session-extraction"
            relative_link.parent.mkdir()
            relative_target = os.path.relpath(SESSION_SKILL, relative_link.parent)
            relative_link.symlink_to(relative_target, target_is_directory=True)

            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment["PYTHONNOUSERSITE"] = "1"
            for skill_link in (absolute_link, relative_link):
                for command in ("doctor", "extract", "reconcile"):
                    with self.subTest(skill_link=skill_link.parent.name, command=command):
                        result = subprocess.run(
                            [str(skill_link / "scripts" / command), "--help"],
                            cwd=root,
                            env=environment,
                            text=True,
                            capture_output=True,
                            timeout=20,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("usage: agent-session-extraction", result.stdout)

    def test_profiles_root_path_is_a_relative_compatibility_link(self) -> None:
        compatibility_path = REPOSITORY_ROOT / "PROFILES.md"
        target = "skills/state-backup/references/profiles.md"

        self.assertTrue(compatibility_path.is_symlink())
        self.assertEqual(os.readlink(compatibility_path), target)
        self.assertEqual(
            compatibility_path.resolve(),
            (REPOSITORY_ROOT / target).resolve(),
        )


if __name__ == "__main__":
    unittest.main()
