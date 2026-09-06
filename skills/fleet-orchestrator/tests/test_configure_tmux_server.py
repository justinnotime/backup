"""Integration tests for configure-tmux-server.py with a fake tmux CLI."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure-tmux-server.py"


class ConfigureTmuxServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / "runtime"
        self.db = base / "ledger.sqlite3"
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "tmux"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "server=default\n"
            "if [ \"${1:-}\" = -L ]; then server=$2; shift 2; fi\n"
            "if [ \"$server\" = bad ]; then echo unreachable >&2; exit 1; fi\n"
            "printf '%%1\\tclaude\\n%%1\\tclaude\\n%%2\\tzsh\\n'\n"
        )
        fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(base / "home"),
            "NOTES_RUNTIME_DIR": str(self.runtime),
            "DISPATCH_LEDGER_DB": str(self.db),
            "MATRIX_BUS_CFG": str(base / "bus"),
            "PATH": f"{self.bin}:/usr/bin:/bin",
        })
        # Initialize the real workplane schema and one stale absence counter.
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import workplane as w; "
            "c=w.connect_writable(); "
            "c.execute(\"INSERT INTO drive(task_id,seat,st,cycles,grace_used,idle_waits,absent_ticks,updated_ms) VALUES('t','s','pulled',0,0,0,4,1)\"); c.commit()"
        )
        subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts" / "lib")],
                       check=True, env=self.env)
        self.addCleanup(self.tmp.cleanup)

    def run_cli(self, *args, expect=0):
        out = subprocess.run([sys.executable, str(SCRIPT), *args], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, expect, out.stdout + out.stderr)
        return out

    def test_success_publishes_selector_and_resets_absence(self):
        out = self.run_cli("tmux37")
        self.assertIn("agent_panes=1", out.stdout)
        path = self.runtime / "state" / "fleet-orchestrator" / "tmux-server"
        self.assertEqual(path.read_text(), "tmux37\n")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT absent_ticks FROM drive").fetchone()[0], 0)

    def test_failed_validation_preserves_old_selector_and_counter(self):
        path = self.runtime / "state" / "fleet-orchestrator" / "tmux-server"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n")
        out = self.run_cli("bad", expect=1)
        self.assertIn("unreachable", out.stderr)
        self.assertEqual(path.read_text(), "old\n")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT absent_ticks FROM drive").fetchone()[0], 4)

    def test_default_removal_is_durable_and_resets_counter(self):
        path = self.runtime / "state" / "fleet-orchestrator" / "tmux-server"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n")
        self.run_cli("--default")
        self.assertFalse(path.exists())
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT absent_ticks FROM drive").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
