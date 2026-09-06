"""Exercise the public CLI with synthetic installations and private state."""

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORC = ROOT / "scripts" / "orc"


class PublicConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.env = {
            "HOME": str(self.base / "home"),
            "PATH": os.environ["PATH"],
            "XDG_CONFIG_HOME": str(self.base / "configuration"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_orc(self, *arguments, config=None, environment=None):
        command = [str(ORC)]
        if config is not None:
            config_file = self.base / "job.json"
            config_file.write_text(json.dumps(config))
            command += ["--config", str(config_file)]
        return subprocess.run(command + list(arguments), env=environment or self.env,
                              text=True, capture_output=True, timeout=15)

    def open_arguments(self):
        return ("open", "--to", "operator", "--subject", "synthetic task",
                "--body", "Review this isolated example.", "--no-check")

    def test_unconfigured_copy_owns_local_state_without_a_notes_checkout(self):
        result = self.run_orc(*self.open_arguments())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        database = (self.base / "state" / "fleet-orchestrator" / "state" /
                    "fleet-orchestrator" / "dispatch-ledger.sqlite3")
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT subject FROM dispatch").fetchone(),
                             ("synthetic task",))
        self.assertFalse((self.base / "home" / "src").exists())

    def test_config_is_loaded_before_the_public_entrypoint_imports_state(self):
        ledger = self.base / "selected" / "tasks.sqlite3"
        result = self.run_orc(*self.open_arguments(), config={
            "paths": {"ledger": str(ledger)},
            "authority": {"merge_keys": {"example-project": "project-owner"}},
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with sqlite3.connect(ledger) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 1)
        self.assertFalse((self.base / "state").exists())

    def test_development_copy_refuses_configured_protected_file_aliases(self):
        production = self.base / "production.sqlite3"
        production.write_bytes(b"do not modify")
        symbolic = self.base / "symbolic.sqlite3"
        symbolic.symlink_to(production)
        hard = self.base / "hard.sqlite3"
        os.link(production, hard)
        configuration = {
            "canonical_source_root": str(self.base / "trusted-installation"),
            "paths": {"ledger": str(self.base / "other-production.sqlite3")},
            "protected_databases": ["$XDG_STATE_HOME/../production.sqlite3"],
        }
        for selected in (production, symbolic, hard):
            with self.subTest(path=selected.name):
                result = self.run_orc(*self.open_arguments(), config=configuration,
                                     environment={**self.env, "DISPATCH_LEDGER_DB": str(selected)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-canonical checkout", result.stdout + result.stderr)
                self.assertEqual(production.read_bytes(), b"do not modify")

    def test_named_ledger_hard_links_outside_the_root_remain_protected(self):
        root = self.base / "named"
        configuration = {
            "canonical_source_root": str(self.base / "trusted-installation"),
            "protected_named_database_roots": [str(root)],
        }
        for number, suffix in enumerate(("dispatch-ledger.sqlite3",
                                        "state/fleet-orchestrator/dispatch-ledger.sqlite3")):
            with self.subTest(layout=suffix):
                production = root / str(number) / suffix
                production.parent.mkdir(parents=True)
                production.write_bytes(b"named production sentinel")
                outside = self.base / f"alias-{number}.sqlite3"
                os.link(production, outside)
                result = self.run_orc(*self.open_arguments(), config=configuration,
                                     environment={**self.env, "DISPATCH_LEDGER_DB": str(outside)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("named production file", result.stdout + result.stderr)
                self.assertEqual(production.read_bytes(), b"named production sentinel")

    def test_unrelated_isolated_database_is_allowed_beside_named_ledgers(self):
        root = self.base / "named"
        production = root / "alpha" / "dispatch-ledger.sqlite3"
        production.parent.mkdir(parents=True)
        production.write_bytes(b"named production sentinel")
        isolated = self.base / "review.sqlite3"
        result = self.run_orc(*self.open_arguments(), config={
            "canonical_source_root": str(self.base / "trusted-installation"),
            "protected_named_database_roots": [str(root)],
        }, environment={**self.env, "DISPATCH_LEDGER_DB": str(isolated)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with sqlite3.connect(isolated) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 1)
        self.assertEqual(production.read_bytes(), b"named production sentinel")


if __name__ == "__main__":
    unittest.main()
