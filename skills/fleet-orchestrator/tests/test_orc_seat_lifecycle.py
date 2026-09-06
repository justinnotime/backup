import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORC = ROOT / "scripts" / "fleet-orchestrator.py"

SEAT = {
    "agent_id": "aaaa-1111-bbbb", "handle": "test/seat-tmux9",
    "aliases": ["test/old-tmux9"], "host": "test-host",
    "tmux": "tmux=0:9.0 win=claude", "status": "active", "mode": "watch",
}


def fake_bus_script(log_path: Path, members: list[dict]) -> str:
    lines = "\n".join(json.dumps(m) for m in members)
    return f"""#!/bin/bash
echo "$@" >> {log_path}
case "$1" in
  members) cat <<'EOF'
{lines}
EOF
  ;;
  retire) echo "retired $2" ;;
  send) echo '{{"msg_id": "fake-msg-1", "transport_state": "accepted"}}' ;;
  unread) echo '{{"count": 0}}' ;;
esac
exit 0
"""


class CheckoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.buslog = base / "bus.log"
        self.buslog.touch()
        bus = base / "fake-bus.sh"
        bus.write_text(fake_bus_script(self.buslog, [SEAT]))
        bus.chmod(0o755)
        self.txnlog = base / "txn.log"
        self.handoff_capture = base / "handoff-captured.md"
        txn = base / "stub-sync-txn.sh"
        txn.write_text(f"#!/bin/bash\necho \"$@ DST=$ORC_HANDOFF_DST\""
                       f" >> {self.txnlog}\n"
                       f"cp \"$ORC_HANDOFF_SRC\" {self.handoff_capture}\n"
                       f"exit 0\n")
        txn.chmod(0o755)
        self.publisher = txn
        config = base / "runtime.json"
        config.write_text(json.dumps({"handoff": {
            "directory": str(base / "handoffs"),
            "publish_command": [str(txn), "agent-handoff"],
        }}))
        self.env = dict(os.environ)
        self.env.update({
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(base / "runtime"),
            "MATRIX_BUS_CFG": str(base / "bus-cfg"),
            "NW_BUS_CLI": str(bus),
            "FLEET_ORCHESTRATOR_CONFIG": str(config),
            "NW_TMUX_SERVER": "unreachable-test-server",
            "DISPATCH_LEDGER_ACTOR": "test",
            "NW_GH_CLI": str(base / "gh"),
            "AGENT_BUS_DB": str(base / "bus-cfg" / "agent-bus-v3.sqlite3"),
            "ORC_SEAT_ID": "",
            "TMUX_PANE": "%72",
        })
        (base / "gh").write_text("#!/bin/sh\necho '[]'\n")
        (base / "gh").chmod(0o755)
        bus_db = Path(self.env["AGENT_BUS_DB"])
        bus_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(bus_db)
        with conn:
            conn.execute(
                "CREATE TABLE identities (agent_id TEXT PRIMARY KEY,"
                " status TEXT NOT NULL, host TEXT NOT NULL, pane_id TEXT,"
                " harness TEXT NOT NULL, lease_until_ms INTEGER)")
            conn.execute(
                "INSERT INTO identities"
                " (agent_id,status,host,pane_id,harness,lease_until_ms)"
                " VALUES (?,?,?,?,?,?)",
                (SEAT["agent_id"], "active", socket.gethostname(), "%72",
                 "codex", int(time.time() * 1000) + 3_600_000))
        conn.close()

    def orc(self, *argv):
        return subprocess.run([sys.executable, str(ORC), *argv],
                              text=True, capture_output=True, env=self.env)

    def bus_verbs(self):
        return [line.split()[0] for line in
                self.buslog.read_text().splitlines() if line.strip()]

    def test_owed_task_refuses_checkout(self):
        r = self.orc("open", "--to", "test/seat-tmux9",
                     "--subject", "unfinished thing", "--check", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.orc("checkout", "test/seat-tmux9", "--summary", "bye")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("owes", out.stdout)
        self.assertIn("unfinished thing"[:20], out.stdout)
        self.assertNotIn("retire", self.bus_verbs())
        self.assertFalse(self.txnlog.exists() and self.txnlog.read_text(),
                         "handoff must not publish for a refused checkout")

    def test_held_role_refuses_checkout(self):
        r = self.orc("role", "grant", "test-key", SEAT["agent_id"],
                     "--by", "test ruling")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.orc("checkout", SEAT["agent_id"], "--summary", "bye")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("role:test-key", out.stdout)
        self.assertNotIn("retire", self.bus_verbs())

    def test_clean_checkout_publishes_then_retires_without_broadcast(self):
        r = self.orc("open", "--to", "test/seat-tmux9",
                     "--subject", "finished thing", "--check", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        import re
        task_id = re.search(r"\b([0-9a-f]{8})\b", r.stdout).group(1)
        rc = self.orc("close", task_id, "--resolution", "done")
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        out = self.orc("checkout", "test/seat-tmux9",
                       "--summary", "state handed to nobody; queue empty")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        txn = self.txnlog.read_text()
        self.assertIn("agent-handoff", txn)
        self.assertRegex(txn, r"DST=\d{4}-\d{2}-\d{2}-seat-tmux9\.md")
        verbs = self.bus_verbs()
        self.assertIn("retire", verbs)
        self.assertNotIn("send", verbs,
                         "checkout creates no fleet-wide inbox work")
        self.assertIn("checkout complete", out.stdout)


        note = self.handoff_capture.read_text()
        self.assertIn("Handoff summary (agent-authored)", note)
        self.assertIn("state handed to nobody; queue empty", note)
        self.assertIn("Ledger trace (auto-generated", note)
        self.assertIn(task_id, note)
        self.assertIn("closed/done", note)

    def test_no_vault_note_skips_publish(self):
        out = self.orc("checkout", "test/seat-tmux9", "--summary", "bye",
                       "--no-vault-note")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.txnlog.read_text() if self.txnlog.exists() else "",
                         "")
        self.assertIn("retire", self.bus_verbs())

    def test_omitted_identity_resolves_exact_calling_pane(self):
        out = self.orc("checkout", "--summary", "self checkout",
                       "--no-vault-note")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("checkout complete: test/seat-tmux9", out.stdout)
        self.assertIn("retire", self.bus_verbs())

    def test_omitted_identity_refuses_unregistered_calling_pane(self):
        self.env["TMUX_PANE"] = "%999"
        out = self.orc("checkout", "--summary", "must not guess",
                       "--no-vault-note")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("current tmux pane %999 has no unique active Agent Bus",
                      out.stdout)
        self.assertIn("nothing was retired", out.stdout)
        self.assertNotIn("retire", self.bus_verbs())

    def test_failed_handoff_publish_leaves_seat_active(self):
        self.publisher.write_text("#!/bin/bash\nexit 1\n")
        out = self.orc("checkout", "test/seat-tmux9", "--summary", "bye")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("seat left ACTIVE", out.stdout)
        self.assertNotIn("retire", self.bus_verbs())


class OnboardTest(CheckoutTest):


    def test_owed_task_and_role_are_surfaced(self):
        r = self.orc("open", "--to", "test/seat-tmux9",
                     "--subject", "inherited work item", "--check", "true")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.orc("role", "grant", "night-shift", SEAT["agent_id"],
                 "--by", "test ruling")
        out = self.orc("onboard", "test/seat-tmux9")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("OWED — 1 open task", out.stdout)
        self.assertIn("inherited work item", out.stdout)
        self.assertIn("role:night-shift", out.stdout)

    def test_missing_ledger_is_unknown_not_a_clean_slate(self):
        out = self.orc("onboard", "test/seat-tmux9")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("OWED — unknown; ledger unavailable", out.stdout)
        self.assertIn("ROLES — unknown; ledger unavailable", out.stdout)

    def test_predecessor_handoff_notes_are_pointed_at(self):
        hdir = Path(self.tmp.name) / "handoffs"
        hdir.mkdir(parents=True)
        (hdir / "2020-01-21-seat-tmux3.md").write_text("old note")
        (hdir / "2020-01-22-seat-tmux9.md").write_text("latest note")
        (hdir / "2020-01-20-unrelated-thing.md").write_text("noise")
        out = self.orc("onboard", "test/seat-tmux9")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("HANDOFFS — predecessor notes for 'seat'", out.stdout)
        self.assertIn("2020-01-22-seat-tmux9.md", out.stdout)
        self.assertNotIn("unrelated-thing", out.stdout)

    def test_unknown_identity_still_exits_zero(self):
        out = self.orc("onboard", "nobody/never-registered")
        self.assertEqual(out.returncode, 0,
                         "boot must never be blocked by a failed self-brief:\n"
                         + out.stdout + out.stderr)


def load_orc_module(env: dict):


    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("orc_under_test", ORC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class SeatLivenessTest(unittest.TestCase):
    HOUR = 60 * 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.orc = load_orc_module({
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(base / "runtime"),
            "MATRIX_BUS_CFG": str(base / "bus-cfg"),
            "NW_BUS_CLI": str(base / "fake-bus.sh"),
            "DISPATCH_LEDGER_ACTOR": "test",
        })
        buslog = base / "bus.log"
        (base / "fake-bus.sh").write_text(fake_bus_script(buslog, [SEAT]))
        (base / "fake-bus.sh").chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.conn = self.orc.wp.connect_writable()
        self.conn.execute("DELETE FROM seat_watch")
        self.conn.execute("DELETE FROM wake_attempt")
        self.conn.commit()
        self.calls = {"nudge": [], "retire": []}

    def run_pass(self, *, alive, panes, unread=0, now, seat=SEAT):
        self.orc.tick_seat_liveness(
            self.conn, dry=False, members=[dict(seat)], panes=panes,
            watcher_alive=lambda aid: alive,
            unread_count=lambda aid: unread,
            nudge=lambda pane_id, progress=None:
                self.calls["nudge"].append(pane_id) or (self.orc.wp.SendOutcome.CONTACTED, ""),
            retire=lambda aid: (self.calls["retire"].append(aid), True)[1],
            now_ms=now, hostnames={"test-host"})

    def watch_row(self):
        return self.conn.execute("SELECT * FROM seat_watch WHERE agent_id=?",
                                 (SEAT["agent_id"],)).fetchone()

    def test_idle_limit_one_threshold_two_doors(self):

        self.assertEqual(self.orc.idle_wait_limit_for("fix the thing", False),
                         self.orc.wp.IDLE_WAIT_LIMIT)

        self.assertEqual(self.orc.idle_wait_limit_for("fix the thing", True),
                         self.orc.wp.IDLE_WAIT_LIMIT_ACTIVE)

        self.assertEqual(
            self.orc.idle_wait_limit_for("STANDING: run patrol each wake", False),
            self.orc.wp.IDLE_WAIT_LIMIT_ACTIVE)

        self.assertEqual(
            self.orc.idle_wait_limit_for("not STANDING: really", False),
            self.orc.wp.IDLE_WAIT_LIMIT)

    def test_live_watcher_clears_death_clock_but_keeps_rate_limit(self):
        self.conn.execute("INSERT INTO seat_watch (agent_id, first_dead_ms,"
                          " last_nudge_ms) VALUES (?, 1, 42)", (SEAT["agent_id"],))
        self.conn.commit()
        self.run_pass(alive=True, panes=[("%9", "0:9.0")], now=self.HOUR)
        row = self.watch_row()
        self.assertEqual(row["first_dead_ms"], 0, "death clock cleared")
        self.assertEqual(row["last_nudge_ms"], 42,
                         "nudge rate limit survives the watcher coming back")

    def test_flap_does_not_reset_the_nudge_rate_limit(self):
        t0 = 1_800_000_000
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3, now=t0)
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 10)
        self.assertEqual(self.calls["nudge"], ["%9"], "nudged once")
        self.run_pass(alive=True, panes=[("%9", "0:9.0")], now=t0 + 20)
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 30)
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 40)
        self.assertEqual(self.calls["nudge"], ["%9"],
                         "a crash-looping watcher must not re-arm the nudge")
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 10 + 31 * 60)
        self.assertEqual(self.calls["nudge"], ["%9", "%9"],
                         "the 30m gap still applies from the LAST nudge")

    def test_flap_restarts_the_continuous_absence_clock(self):
        t0 = 1_800_000_000
        m = 60
        self.run_pass(alive=False, panes=[], now=t0)
        self.run_pass(alive=False, panes=[], now=t0 + 10)
        self.run_pass(alive=True, panes=[], now=t0 + 50 * m)
        self.run_pass(alive=False, panes=[], now=t0 + 51 * m)
        self.run_pass(alive=False, panes=[], now=t0 + 52 * m)
        self.run_pass(alive=False, panes=[], now=t0 + self.HOUR + 10)
        self.assertEqual(self.calls["retire"], [],
                         "retirement needs a CONTINUOUS hour, not a total")
        self.run_pass(alive=False, panes=[],
                      now=t0 + 52 * m + self.HOUR + 10)
        self.assertEqual(self.calls["retire"], [SEAT["agent_id"]])

    def test_stale_rows_for_departed_seats_are_cleaned(self):
        self.conn.execute("INSERT INTO seat_watch (agent_id, first_dead_ms)"
                          " VALUES ('gone-seat-id', 1)")
        self.conn.commit()
        self.run_pass(alive=True, panes=[], now=self.HOUR)
        gone = self.conn.execute("SELECT * FROM seat_watch WHERE"
                                 " agent_id='gone-seat-id'").fetchone()
        self.assertIsNone(gone, "rows for seats outside the watch set are dropped")

    def test_dead_with_pane_and_mail_nudges_once_per_gap(self):


        t0 = 1_800_000_000
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3, now=t0)
        self.assertEqual(self.calls["nudge"], [], "first sighting only records")
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 10)
        self.assertEqual(self.calls["nudge"], ["%9"])
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 20)
        self.assertEqual(self.calls["nudge"], ["%9"], "rate limit: 30m gap")
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=3,
                      now=t0 + 10 + 31 * 60)
        self.assertEqual(self.calls["nudge"], ["%9", "%9"])

    def test_parked_seat_with_empty_inbox_is_left_alone(self):
        for now in (0, 1000, self.HOUR, 3 * self.HOUR):
            self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=0,
                          now=now)
        self.assertEqual(self.calls["nudge"], [])
        self.assertEqual(self.calls["retire"], [])
        self.assertIsNotNone(self.watch_row(), "dead watcher is still recorded")

    def test_pane_gone_probes_then_retires_after_an_hour(self):
        t0 = 1_800_000_000
        self.run_pass(alive=False, panes=[], now=t0)
        self.run_pass(alive=False, panes=[], now=t0 + 10)
        row = self.watch_row()
        self.assertGreater(row["probe_ms"], 0)
        self.assertEqual(self.calls["retire"], [])
        self.run_pass(alive=False, panes=[], now=t0 + self.HOUR - 10)
        self.assertEqual(self.calls["retire"], [], "not before the hour")
        self.run_pass(alive=False, panes=[], now=t0 + self.HOUR + 10)
        self.assertEqual(self.calls["retire"], [SEAT["agent_id"]])
        self.assertIsNone(self.watch_row(), "retired seat's bookkeeping cleared")
        broadcasts = self.conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE purpose='liveness-retire'"
        ).fetchone()[0]
        self.assertEqual(broadcasts, 0,
                         "automatic retirement creates no fleet-wide inbox work")

    def test_pane_returning_stops_the_retirement_clock(self):
        self.run_pass(alive=False, panes=[], now=0)
        self.run_pass(alive=False, panes=[], now=1000)
        self.run_pass(alive=False, panes=[("%9", "0:9.0")], unread=0,
                      now=2 * self.HOUR)
        self.assertEqual(self.calls["retire"], [],
                         "a present pane is never retired by the machine")

    def test_other_hosts_and_pull_seats_are_ignored(self):
        foreign = dict(SEAT, host="other-host")
        self.orc.tick_seat_liveness(
            self.conn, dry=False, members=[foreign], panes=[],
            watcher_alive=lambda aid: False,
            unread_count=lambda aid: 9,
            nudge=lambda p, progress=None:
                self.calls["nudge"].append(p) or (self.orc.wp.SendOutcome.CONTACTED, ""),
            retire=lambda aid: True, now_ms=0, hostnames={"test-host"})
        pull_seat = dict(SEAT, mode="pull")
        self.orc.tick_seat_liveness(
            self.conn, dry=False, members=[pull_seat], panes=[],
            watcher_alive=lambda aid: False,
            unread_count=lambda aid: 9,
            nudge=lambda p, progress=None:
                self.calls["nudge"].append(p) or (self.orc.wp.SendOutcome.CONTACTED, ""),
            retire=lambda aid: True, now_ms=0, hostnames={"test-host"})
        self.assertIsNone(self.watch_row())
        self.assertEqual(self.calls["nudge"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
