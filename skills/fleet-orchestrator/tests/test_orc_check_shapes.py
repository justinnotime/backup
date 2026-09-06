import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))


def load_orc(env):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "orc_check_shapes_under_test", ROOT / "scripts" / "fleet-orchestrator.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class CheckShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.orc = load_orc({
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(base / "runtime"),
            "MATRIX_BUS_CFG": str(base / "bus-cfg"),
            "NW_TMUX_SERVER": "nonexistent-server-for-tests",
            "DISPATCH_LEDGER_ACTOR": "test",
        })
        cls.wp = cls.orc.wp
        cls.wp.DB_PATH = base / "ledger.sqlite3"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_counting_proxies_are_named(self):
        label = self.orc.weak_check_label
        for cmd in (
            "gh pr list --repo o/r --json number | jq length",
            "gh pr list --repo o/r --json number --jq 'length'",
            "curl -s http://127.0.0.1/items | jq '.items | length'",
            "gh pr list --repo o/r --json number --jq '. | length'",
            "jq '[.[]|select(.ok)] | length' out.json",
            "git log --oneline | wc -l",
            "git log --oneline | wc --lines",
            "ls /srv/out | wc -lc",
            "grep -c monotonic /tmp/out.txt",
            "grep -F -c monotonic /tmp/out.txt",
            "grep -rci needle /srv/logs",
            "rg -c needle /srv/logs",
            "gh pr list --repo o/r --state merged | grep --count stamp",
            "sort names | uniq -c",
        ):
            self.assertEqual(label(cmd), "counts-matches", cmd)

    def test_artifact_probes_pass(self):
        label = self.orc.weak_check_label
        for cmd in (
            "test -f /srv/out/report.md",
            "curl -fsS http://127.0.0.1:8080/health | jq -e '.tree_version'",
            "gh pr view 52293 --repo o/r --json state --jq .state",
            "test \"$(gh pr view 7 --repo o/r --json isDraft --jq .isDraft)\" = false",
            "grep -q 'tree_version' /srv/app/openapi.json",
            "grep -E 'ok' status.txt",
            "jq -e '.fields.length' out.json",
            "jq -e '.checks' out.json",
        ):
            self.assertIsNone(label(cmd), cmd)

    def test_existing_shapes_still_named(self):
        label = self.orc.weak_check_label
        self.assertEqual(label("gh pr list --search 'sqlite in:title'"), "pr-title-exists")
        self.assertEqual(label("git branch --list feature/x"), "branch-exists")
        self.assertEqual(label("true"), "asserts-nothing")
        self.assertIsNone(label(""))

    def _open(self, check, done_cmd=""):
        args = SimpleNamespace(
            to="tmux9", subject="s", body="b", check=check, no_check=False,
            link=[], after="45m", workflow="dispatch", parent="", repo="",
            owner="", reviewer="", ready_cmd="", done_cmd=done_cmd, needs=(),
            deadline=None, breaker="", await_notify=False, body_file=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            did = self.orc._open(args)
        return did, out.getvalue()

    def test_open_warns_for_a_counting_check_and_still_opens(self):
        did, out = self._open("git log --oneline origin/main | wc -l")
        self.assertIn("WARN  --check is a weak check (counts-matches)", out)

        _did2, out2 = self._open("gh pr list --repo o/r --search monotonic | wc -l")
        self.assertIn("WARN  --check is a weak check (pr-title-exists)", out2)
        self.assertIn("Probe the artifact itself", out)
        row = self.wp.fetch(self.wp.connect_readonly(), did)
        self.assertEqual(row["state"], "open", "warn, never block")

    def test_open_stays_quiet_for_an_artifact_probe(self):
        _did, out = self._open("test -f /srv/out/report.md")
        self.assertNotIn("weak check", out)

    def test_doctor_names_counting_checks_on_open_tasks(self):
        conn = self.wp.connect_writable()
        with conn:
            conn.execute("DELETE FROM dispatch")
            conn.execute("DELETE FROM event")
            did = self.wp.insert_task(conn, recipient="tmux9", subject="count me",
                                      check_cmd="grep -c stamp /tmp/x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.orc.doctor_truthfulness(conn, panes=[], members=[])
        self.assertIn(f"{did} has a weak check (counts-matches)", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
