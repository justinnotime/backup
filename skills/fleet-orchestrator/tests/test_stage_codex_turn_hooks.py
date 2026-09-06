"""Mechanical hook staging only; no harness, trust, or service invocation."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/stage-codex-turn-hooks.sh"


class StageCodexTurnHooks(unittest.TestCase):
    def test_stages_and_preserves_existing_entries_without_accepting_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "directory with spaces/config.toml"
            config.parent.mkdir()
            config.write_text('model = "example"\n')
            selected = root / "private profile.json"
            environment = {"PATH": os.environ["PATH"], "HOME": temporary,
                           "TURN_HOOKS_CODEX_CONFIG": str(config)}
            run = subprocess.run(["bash", str(SCRIPT), "--config", str(selected)],
                                 env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            text = config.read_text()
            self.assertIn("hooks.UserPromptSubmit", text)
            self.assertIn("hooks.Stop", text)
            self.assertIn("FLEET_ORCHESTRATOR_CONFIG=", text)
            self.assertNotIn("trusted_hash", text)
            self.assertEqual(config.with_name("config.toml.bak.turn-hooks").read_text(), 'model = "example"\n')
            repeated = subprocess.run(["bash", str(SCRIPT)], env=environment,
                                      capture_output=True, text=True, check=False)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(config.read_text(), text)

    def test_preserves_uninspected_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text('model = "example"\n')
            backup = config.with_name("config.toml.bak.turn-hooks")
            backup.write_text("original")
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                                    env={"PATH": os.environ["PATH"], "HOME": temporary,
                                         "TURN_HOOKS_CODEX_CONFIG": str(config)}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(backup.read_text(), "original")
            self.assertEqual(config.read_text(), 'model = "example"\n')
