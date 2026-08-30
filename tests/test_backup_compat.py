"""Compatibility tests for the standalone ``backup.sh`` entry point.

All inputs live under a temporary HOME.  External copy commands are replaced
with small recorders so these tests never inspect or modify machine state.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPOSITORY_ROOT / "backup.sh"


BACKUP_ENVIRONMENT_VARIABLES = {
    "BACKUP_LOG",
    "BACKUP_ROOT",
    "CLAUDE_BACKUP_DIR",
    "CLAUDE_HOME",
    "CLAUDE_PROFILES",
    "CODEX_BACKUP_DIR",
    "CODEX_HOME",
    "CODEX_PROFILES",
    "CURSOR_BACKUP_DIR",
    "CURSOR_HOME",
    "CURSOR_USER_DIR",
    "DSH_BACKUP_PREFIX",
    "DSH_PROFILES",
    "MACHINE_ID",
    "OPENCLAW_BACKUP_DIR",
    "OPENCLAW_HOME",
    "OPENCODE_BACKUP_DIR",
    "OPENCODE_CONFIG_SRC",
    "OPENCODE_DATA_DIR",
    "OPENCODE_PROFILES",
    "OPENCODE_STATE_DIR",
    "SYNCTHING_ROOT",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


class BackupCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.test_root = Path(temporary_directory.name)
        self.home = self.test_root / "home"
        self.home.mkdir()

        self.command_directory = self.test_root / "commands"
        self.command_directory.mkdir()
        self.rsync_log = self.test_root / "rsync-calls.tsv"
        self.python_log = self.test_root / "python-calls.txt"
        self._install_command_stubs()

        self.environment = os.environ.copy()
        for variable in BACKUP_ENVIRONMENT_VARIABLES:
            self.environment.pop(variable, None)
        self.environment.update(
            {
                "HOME": str(self.home),
                "LC_ALL": "C",
                "MACHINE_ID": "fixture-node",
                "PATH": f"{self.command_directory}{os.pathsep}{os.environ['PATH']}",
                "PYTHON_CALL_LOG": str(self.python_log),
                "PYTHONNOUSERSITE": "1",
                "RSYNC_LOG": str(self.rsync_log),
            }
        )
        self.environment.pop("PYTHONPATH", None)

    def _install_command_stubs(self) -> None:
        rsync = self.command_directory / "rsync"
        rsync.write_text(
            """#!/bin/sh
{
  printf 'CALL'
  for argument in "$@"; do
    printf '\\t%s' "$argument"
  done
  printf '\\n'
} >> "$RSYNC_LOG"
exit 0
""",
            encoding="utf-8",
        )
        rsync.chmod(0o755)

        failing_python = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$PYTHON_CALL_LOG"
exit 97
"""
        for command_name in ("python", "python3"):
            command = self.command_directory / command_name
            command.write_text(failing_python, encoding="utf-8")
            command.chmod(0o755)

    def _write_file(self, path: Path, contents: str = "synthetic fixture\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _write_config(self, assignments: dict[str, str]) -> None:
        config = self.home / ".config" / "backup" / "config"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "".join(
                f"{name}={shlex.quote(value)}\n" for name, value in assignments.items()
            ),
            encoding="utf-8",
        )

    def _run_backup(
        self, *, through_home_symlink: bool = False
    ) -> subprocess.CompletedProcess[str]:
        command = BACKUP_SCRIPT
        if through_home_symlink:
            command = self.home / "bin" / "backup"
            command.parent.mkdir(parents=True, exist_ok=True)
            command.symlink_to(BACKUP_SCRIPT)

        result = subprocess.run(
            [str(command)],
            cwd=self.home,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"backup command failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def _recorded_rsync_calls(self) -> str:
        return self.rsync_log.read_text(encoding="utf-8")

    def test_native_defaults_and_home_symlink_need_no_python_runtime(self) -> None:
        self._write_file(
            self.home / ".openclaw" / "agents" / "main" / "sessions" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".claude" / "projects" / "project" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".codex" / "sessions" / "2026" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".dsh" / "sessions" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".config" / "opencode" / "opencode.json",
            "{}\n",
        )
        self._write_file(
            self.home
            / ".cursor"
            / "projects"
            / "project"
            / "agent-transcripts"
            / "session"
            / "transcript.jsonl",
            "{}\n",
        )

        self._run_backup(through_home_symlink=True)

        output = self.home / "syncthing" / "backup" / "fixture-node"
        expected_directories = (
            output / "openclaw" / "sessions",
            output / "claude" / "projects",
            output / "codex" / "sessions",
            output / "dsh",
            output / "opencode" / "config",
            output / "cursor" / "projects",
        )
        for directory in expected_directories:
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())

        calls = self._recorded_rsync_calls()
        for native_source in (
            self.home / ".openclaw" / "agents" / "main" / "sessions",
            self.home / ".claude" / "projects",
            self.home / ".codex" / "sessions",
            self.home / ".dsh",
            self.home / ".config" / "opencode",
            self.home / ".cursor" / "projects",
        ):
            with self.subTest(native_source=native_source):
                self.assertIn(f"{native_source}/", calls)

        self.assertFalse(
            self.python_log.exists(),
            "backup.sh unexpectedly invoked Python",
        )

    def test_existing_profile_formats_keep_their_destination_names(self) -> None:
        profile_roots: dict[str, dict[str, Path]] = {
            "claude": {
                "alpha": self.home / ".claude-alpha",
                "beta": self.home / ".claude-beta",
            },
            "codex": {
                "alpha": self.home / ".codex-alpha",
                "beta": self.home / ".codex-beta",
            },
            "opencode": {
                "alpha": self.home / ".opencode-alpha",
                "beta": self.home / ".opencode-beta",
            },
            "dsh": {
                "alpha": self.home / ".dsh-alpha",
                "beta": self.home / "synthetic roots" / ".dsh-beta",
            },
        }

        for root in profile_roots["claude"].values():
            self._write_file(root / "projects" / "project" / "session.jsonl", "{}\n")
        for root in profile_roots["codex"].values():
            self._write_file(root / "sessions" / "2026" / "session.jsonl", "{}\n")
        for root in profile_roots["opencode"].values():
            self._write_file(root / "config" / "opencode" / "opencode.json", "{}\n")
        for root in profile_roots["dsh"].values():
            self._write_file(root / "sessions" / "session.jsonl", "{}\n")

        output = self.test_root / "backup-output"
        self._write_config(
            {
                "MACHINE_ID": "fixture-node",
                "BACKUP_ROOT": str(output),
                "CLAUDE_PROFILES": " ".join(
                    f"{label}:{root}" for label, root in profile_roots["claude"].items()
                ),
                "CODEX_PROFILES": " ".join(
                    f"{label}:{root}" for label, root in profile_roots["codex"].items()
                ),
                "OPENCODE_PROFILES": " ".join(
                    f"{label}:{root}"
                    for label, root in profile_roots["opencode"].items()
                ),
                "DSH_PROFILES": "\n".join(
                    f"{label}:{root}" for label, root in profile_roots["dsh"].items()
                ),
            }
        )

        self._run_backup()

        expected_directories = (
            output / "claude-alpha" / "projects",
            output / "claude-beta" / "projects",
            output / "codex-alpha" / "sessions",
            output / "codex-beta" / "sessions",
            output / "opencode-alpha" / "config",
            output / "opencode-beta" / "config",
            output / "dsh-alpha",
            output / "dsh-beta",
        )
        for directory in expected_directories:
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())

        calls = self._recorded_rsync_calls()
        for tool_roots in profile_roots.values():
            for source in tool_roots.values():
                with self.subTest(source=source):
                    self.assertIn(str(source), calls)


if __name__ == "__main__":
    unittest.main()
