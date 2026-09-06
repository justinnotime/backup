#!/usr/bin/env python3
"""Session lifecycle at the registry itself: one tmux pane =
one ACTIVE seat, enforced inside cmd_join so EVERY join path (boot script,
harness plugins, hand-run CLI) hits it; a checkout is FINAL for its slot;
the Agent Bus database row is the sole pane-to-seat identity source."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_bus_v3_session", ROOT / "scripts" / "agent-bus-v3.py")
bus = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(bus)


class SessionLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        bus.CFG = base / "cfg"
        bus.CFG.mkdir()
        bus.DB_PATH = bus.CFG / "bus.sqlite3"
        (bus.CFG / "auth.hdr").write_text("Authorization: Bearer test\n")
        self.rt = base / "rt"
        self.env = mock.patch.dict(os.environ, {
            "TMUX_PANE": "", "AGENT_BUS_TRANSPORT": "matrix", "NOTES_RUNTIME_DIR": str(self.rt)})
        self.env.start()
        self.states = {}
        mock.patch.object(bus, "sync", side_effect=lambda _t, _s: {
            "next_batch": "t1"}).start()
        mock.patch.object(
            bus, "put_state",
            side_effect=lambda aid, content: self.states.__setitem__(
                aid, content) or f"$state-{aid}").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def join(self, slot, *, pane="", handle=None, harness="test",
             mode="watch", host="host", tmux="tmux=0:1.0 win=x"):
        handle = handle or f"{slot}-tmux1"
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"TMUX_PANE": pane}):
            with contextlib.redirect_stdout(output):
                bus.cmd_join(argparse.Namespace(
                    handle=handle, slot=slot, harness=harness, mode=mode,
                    host=host, tmux=tmux))
        return json.loads(output.getvalue())

    def retire(self, agent_id, kind=None):
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retire(argparse.Namespace(identity=agent_id, kind=kind))

    def active_rows(self):
        conn = bus.db()
        try:
            return conn.execute(
                "SELECT slot, pane_id FROM identities"
                " WHERE status='active' ORDER BY slot").fetchall()
        finally:
            conn.close()

    def test_join_records_pane_only_in_database(self):
        self.join("h/task-a", pane="%7")
        conn = bus.db()
        row = conn.execute("SELECT pane_id, retired_kind FROM identities"
                           " WHERE slot='h/task-a'").fetchone()
        conn.close()
        self.assertEqual(row["pane_id"], "%7")
        self.assertIsNone(row["retired_kind"])
        self.assertFalse(self.rt.exists(),
                         "join must not create a second runtime identity store")

    def test_join_outside_tmux_records_no_pane_and_no_runtime_store(self):
        self.join("h/task-a", pane="")
        conn = bus.db()
        row = conn.execute("SELECT pane_id FROM identities"
                           " WHERE slot='h/task-a'").fetchone()
        conn.close()
        self.assertIsNone(row["pane_id"])
        self.assertFalse(self.rt.exists())

    def test_headless_services_do_not_share_a_fake_tmux_location(self):
        self.join("h/old-cron", pane="", mode="pull",
                  tmux="tmux=none win=cron")
        joined = self.join(
            "h/fleet-orchestrator-cron", pane="", harness="cron",
            mode="pull", tmux="headless=cron service=fleet-orchestrator")
        self.assertEqual(joined["slot"], "h/fleet-orchestrator-cron")
        self.assertEqual(len(self.active_rows()), 2)

    def test_second_slot_on_same_pane_is_refused_for_any_mode(self):
        self.join("h/old-dsh", pane="%9", harness="test", mode="pull",
                  tmux="tmux=0:9.0 win=dsh")
        for mode, tmux in (("watch", "tview-x:2.0 hidden"),
                           ("pull", "tmux=9:1.0 win=other")):
            with self.subTest(mode=mode):
                with self.assertRaises(RuntimeError) as ctx:
                    self.join("h/new-seat", pane="%9", mode=mode,
                              tmux="tmux=1:1.0 win=oc")
                text = str(ctx.exception)
                self.assertIn("pane-succession", text,
                              "refusal must point at the sanctioned path")
                self.assertIn("AGENT_BUS_SLOT=h/old-dsh", text,
                              "refusal must show the resume path")
        rows = self.active_rows()
        self.assertEqual([r["slot"] for r in rows], ["h/old-dsh"],
                         "no second identity may appear on refusal")

    def test_same_slot_resume_on_same_pane_is_allowed(self):
        first = self.join("h/task-a", pane="%3")
        again = self.join("h/task-a", pane="%3",
                          handle="h/task-a-tmux9")
        self.assertEqual(first["agent_id"], again["agent_id"],
                         "resume keeps the identity")
        self.assertEqual(len(self.active_rows()), 1)

    def test_legacy_row_without_pane_id_still_guards_by_location(self):
        conn = bus.db()
        with conn:
            conn.execute(
                "INSERT INTO identities(agent_id,slot,handle,generation,"
                "status,harness,mode,host,tmux,aliases_json,created_ms,"
                "updated_ms) VALUES ('legacy-1','h/legacy','h/legacy-tmux2',"
                "1,'active','dsh','pull','host','tmux=0:2.0 win=dsh','[]',"
                "1,1)")
        conn.close()
        with self.assertRaises(RuntimeError):
            self.join("h/new-seat", pane="%2", tmux="tmux=0:2.0 win=dsh")

    def test_different_panes_coexist(self):
        self.join("h/task-a", pane="%1", tmux="tmux=0:1.0 win=a")
        self.join("h/task-b", pane="%2", tmux="tmux=0:2.0 win=b")
        self.assertEqual(len(self.active_rows()), 2)

    def test_checkout_retired_slot_refuses_revive(self):
        first = self.join("h/bench", pane="%5")
        self.retire(first["agent_id"], kind="checkout")
        with self.assertRaises(RuntimeError) as ctx:
            self.join("h/bench", pane="%5")
        self.assertIn("BY CHECKOUT", str(ctx.exception))
        self.assertEqual(self.active_rows(), [])
        with mock.patch.dict(os.environ, {"AGENT_BUS_REVIVE_CHECKEDOUT": "1"}):
            revived = self.join("h/bench", pane="%5")
        self.assertEqual(revived["agent_id"], first["agent_id"],
                         "the operator override stays available")

    def test_reaper_retired_slot_revives_by_plain_rejoin(self):
        first = self.join("h/task-a", pane="%5")
        self.retire(first["agent_id"], kind="reaper")
        revived = self.join("h/task-a", pane="%5")
        self.assertEqual(revived["agent_id"], first["agent_id"],
                         "reaper retirement stays revivable (README rule)")
        conn = bus.db()
        row = conn.execute("SELECT retired_kind FROM identities"
                           " WHERE slot='h/task-a'").fetchone()
        conn.close()
        self.assertIsNone(row["retired_kind"],
                          "revive clears the retirement kind")

    def test_racing_join_loses_at_the_database_not_the_guard(self):
        # round 2 (tmux3 barrier repro): per-agent registry locks let two
        # concurrent joins BOTH pass the pane guard's SELECT, then both
        # wrote - two ACTIVE seats on one pane. The race is reproduced
        # deterministically by DISABLING the guard's read entirely (the
        # exact state both racers were in) and letting the writes land in
        # turn: the DB's partial unique index is the final arbiter and the
        # loser gets the guard's refusal with zero rows landed. (A live
        # thread-barrier repro deadlocks on the shared test DB's init
        # plumbing; the enforcement point pinned here - write-time,
        # independent of any read - is the ruling's property.)
        with mock.patch.object(bus, "_join_pane_guard",
                               lambda *a, **k: None):
            self.join("h/race-1", pane="%77", tmux="tmux=0:1.0 win=x")
            with self.assertRaises(RuntimeError) as ctx:
                self.join("h/race-2", pane="%77", tmux="tmux=0:2.0 win=x")
        self.assertIn("concurrent join won the race", str(ctx.exception))
        rows = self.active_rows()
        self.assertEqual([r["slot"] for r in rows], ["h/race-1"],
                         "the DATABASE holds one ACTIVE seat per pane;"
                         " the loser landed zero rows")

    def test_index_blocks_a_revive_onto_an_occupied_pane(self):
        # the UPDATE path can violate uniqueness too: a retired seat
        # reviving by slot onto a pane someone else now holds
        first = self.join("h/task-a", pane="%77", tmux="tmux=0:1.0 win=x")
        self.retire(first["agent_id"], kind="reaper")
        self.join("h/task-b", pane="%77", tmux="tmux=0:2.0 win=y")
        with mock.patch.object(bus, "_join_pane_guard",
                               lambda *a, **k: None):
            with self.assertRaises(RuntimeError) as ctx:
                self.join("h/task-a", pane="%77", tmux="tmux=0:1.0 win=x")
        self.assertIn("concurrent join won the race", str(ctx.exception))
        self.assertEqual(len(self.active_rows()), 1)

    def test_retire_kind_recorded_and_defaults_to_manual(self):
        a = self.join("h/task-a", pane="%1", tmux="tmux=0:1.0 win=a")
        b = self.join("h/task-b", pane="%2", tmux="tmux=0:2.0 win=b")
        self.retire(a["agent_id"])
        self.retire(b["agent_id"], kind="succession")
        conn = bus.db()
        kinds = {r["slot"]: r["retired_kind"] for r in conn.execute(
            "SELECT slot, retired_kind FROM identities")}
        conn.close()
        self.assertEqual(kinds, {"h/task-a": "manual",
                                 "h/task-b": "succession"})


if __name__ == "__main__":
    unittest.main()
