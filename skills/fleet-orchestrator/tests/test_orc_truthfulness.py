import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))


def load_orc(env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "orc_truth_under_test", ROOT / "scripts" / "fleet-orchestrator.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TruthfulnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.orc = load_orc({
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(base / "runtime"),
            "MATRIX_BUS_CFG": str(base / "bus-cfg"),
            "DISPATCH_LEDGER_ACTOR": "test",
        })
        cls.wp = cls.orc.wp
        cls.wp.DB_PATH = base / "ledger.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        conn = self.wp.connect_writable()
        with conn:
            conn.execute("DELETE FROM dispatch")
            conn.execute("DELETE FROM event")
        conn.close()

    def run_doctor_section(self, panes=None, members=None):
        conn = self.wp.connect_writable()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fails = self.orc.doctor_truthfulness(conn, panes, members)
        conn.close()
        return fails, buf.getvalue()

    def test_children_breakdown_splits_resolutions(self):
        conn = self.wp.connect_writable()
        with conn:
            goal = self.wp.insert_task(conn, recipient="role:lead",
                                       subject="goal", workflow="parent")
            kids = [self.wp.insert_task(conn, recipient="tmux9",
                                        subject=f"k{i}", check_cmd="true",
                                        parent_id=goal) for i in range(4)]
        for kid, res in zip(kids[:3], ("done", "superseded", "superseded")):
            with conn:
                self.wp.record(conn, kid, f"close:{res}", "t")
                conn.execute("UPDATE dispatch SET state='closed', resolution=?"
                             " WHERE id=?", (res, kid))
        text = self.orc.children_breakdown(conn, goal)
        conn.close()
        self.assertIn("1 done", text)
        self.assertIn("2 superseded", text)
        self.assertIn("1 active", text)
        self.assertIn("/4", text)
        self.assertNotIn("children closed", text,
                         "the combined closed-count phrasing must be gone")

    def test_weak_checks_are_named(self):
        conn = self.wp.connect_writable()
        with conn:
            self.wp.insert_task(conn, recipient="tmux9", subject="milestone A",
                                check_cmd="gh pr list --search 'sqlite in:title'")
            self.wp.insert_task(conn, recipient="tmux9", subject="milestone B",
                                check_cmd="git branch --list 'feature/x'")
            self.wp.insert_task(conn, recipient="tmux9", subject="real check",
                                check_cmd="python3 tests/run_acceptance.py")
        conn.close()
        fails, out = self.run_doctor_section()
        self.assertEqual(fails, 0, "weak checks are NOTEs, not FAILs")
        self.assertIn("weak check (pr-title-exists)", out)
        self.assertIn("weak check (branch-exists)", out)
        self.assertNotIn("real check", out,
                         "a substantive check draws no note")

    def test_duplicate_location_is_a_fail_and_orphan_pane_a_note(self):
        members = [
            {"agent_id": "a1", "handle": "example-host/one-tmux3", "status": "active",
             "tmux": "tmux=0:3.0 win=opencode"},
            {"agent_id": "a2", "handle": "example-host/two-tmux3", "status": "active",
             "tmux": "tmux=0:3.0 win=opencode"},
            {"agent_id": "a3", "handle": "example-host/ok-tmux4", "status": "active",
             "tmux": "tmux=0:4.0 win=claude"},
        ]
        panes = [("%1", "0:3.0"), ("%2", "0:4.0"), ("%3", "0:17.0")]
        fails, out = self.run_doctor_section(panes=panes, members=members)
        self.assertEqual(fails, 1)
        self.assertIn("2 active identities at the same location", out)
        self.assertIn("example-host/one-tmux3", out)
        self.assertIn("window 17 has no bus registration", out)
        self.assertNotIn("window 4 has no", out)

    def test_literal_window_recipient_on_deferred_task_warns(self):
        conn = self.wp.connect_writable()
        with conn:
            pred = self.wp.insert_task(conn, recipient="tmux9", subject="pred",
                                       check_cmd="true")
            self.wp.insert_task(conn, recipient="tmux7", subject="future work",
                                check_cmd="true", needs=(pred,))
        conn.close()
        fails, out = self.run_doctor_section()
        self.assertIn("deferred but addressed to the literal window 'tmux7'",
                      out)
        self.assertNotIn("'tmux9'", out,
                         "only DEFERRED tasks draw the literal-window warning")


if __name__ == "__main__":
    unittest.main(verbosity=2)
