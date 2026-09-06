#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rollout_control", ROOT / "scripts" / "rollout-control.py")
rc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
import sys
sys.modules[SPEC.name] = rc
SPEC.loader.exec_module(rc)


class RolloutControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.canonical = self.base / "canonical"
        self.home = self.base / "home"
        self.runtime = self.base / "runtime"
        self.cfg = self.base / "cfg"
        self.settings = self.base / "runtime.json"
        self.settings.write_text("{}")
        configuration = mock.patch.dict(os.environ, {"FLEET_ORCHESTRATOR_CONFIG": str(self.settings)})
        configuration.start()
        self.addCleanup(configuration.stop)
        for path in (self.repo, self.canonical, self.home, self.runtime, self.cfg):
            path.mkdir(parents=True, exist_ok=True)
        self.env = {
            "HOME": str(self.home), "ROLLOUT_HOME": str(self.home),
            "NOTES_RUNTIME_DIR": str(self.runtime), "MATRIX_BUS_CFG": str(self.cfg),
            "DSH_HOME": str(self.home / ".dsh-work"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "ROLLOUT_STAGE_DIR": str(self.runtime / "staged"),
            "ROLLOUT_TMUX_PANES_JSON": "[]",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, **overrides):
        row = {
            "id": "demo", "class": "opencode-plugin",
            "source": ["plugins/demo.ts"], "harness": "opencode",
            "seat": "registered-seats", "activation": "process-restart",
            "target_state": "VERIFIED", "install": "copy-file",
            "target": "${XDG_CONFIG_HOME}/opencode/plugins/demo.ts",
            "observer": "seat-presence",
        }
        row.update(overrides)
        return row

    def manifest(self, artifact=None):
        return {
            "$schema": "./rollout-artifacts.schema.json", "schema": "orc-rollout/v1",
            "states": list(rc.STATES), "artifacts": [artifact or self.artifact()],
        }

    def write_sources(self, artifact, text="export default 1\n"):
        for source in artifact.get("source", []):
            path = self.repo / source
            if Path(source).suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                other = self.canonical / source
                other.parent.mkdir(parents=True, exist_ok=True)
                other.write_text(text)
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "index.js").write_text(text)
                other = self.canonical / source
                other.mkdir(parents=True, exist_ok=True)
                (other / "index.js").write_text(text)

    def control(self, artifact):
        rc.validate_manifest(self.manifest(artifact))
        return rc.ControlPlane(self.manifest(artifact), repo=self.repo,
                               canonical_repo=self.canonical, env=self.env)

    def test_agent_bus_config_and_database_use_transport_neutral_overrides(self):
        bus_cfg = self.base / "local-bus"
        bus_db = self.base / "local-bus.sqlite3"
        ledger_db = self.base / "local-ledger.sqlite3"
        env = {**self.env, "AGENT_BUS_CFG": str(bus_cfg),
               "AGENT_BUS_DB": str(bus_db),
               "DISPATCH_LEDGER_DB": str(ledger_db)}
        control = rc.ControlPlane(self.manifest(), repo=self.repo,
                                  canonical_repo=self.canonical, env=env)
        self.assertEqual(control.cfg, bus_cfg)
        self.assertEqual(control.bus_db, bus_db)
        self.assertEqual(control.ledger_db, ledger_db)

    def test_local_fleet_presence_uses_explicit_work_ledger(self):
        bus_cfg = self.base / "local-bus"
        ledger_db = self.base / "local-ledger.sqlite3"
        conn = sqlite3.connect(ledger_db)
        conn.execute(
            "CREATE TABLE seat_presence (seat TEXT, harness TEXT, at_ms INTEGER,"
            " kind TEXT, starts INTEGER, ends INTEGER)"
        )
        conn.execute(
            "INSERT INTO seat_presence VALUES(?,?,?,?,?,?)",
            ("seat-1", "codex", 1234, "turn", 1, 1),
        )
        conn.commit()
        conn.close()
        env = {
            **self.env,
            "AGENT_BUS_CFG": str(bus_cfg),
            "DISPATCH_LEDGER_DB": str(ledger_db),
        }
        control = rc.ControlPlane(
            self.manifest(), repo=self.repo,
            canonical_repo=self.canonical, env=env,
        )
        seat = rc.Seat(
            "seat-1", "host/seat", [], "codex", "pull", "host",
            "tmux=0:1.0 win=codex", "1",
        )

        observed_at, detail = control._presence(seat, "codex")

        self.assertEqual(observed_at, 1234)
        self.assertIn("starts=1 ends=1", detail)

    def test_manifest_strict_validation(self):
        data = self.manifest()
        rc.validate_manifest(data)
        bad = json.loads(json.dumps(data))
        bad["extra"] = True
        with self.assertRaises(rc.ManifestError):
            rc.validate_manifest(bad)
        bad = json.loads(json.dumps(data))
        bad["artifacts"][0]["id"] = "Bad_ID"
        with self.assertRaises(rc.ManifestError):
            rc.validate_manifest(bad)
        bad = json.loads(json.dumps(data))
        bad["artifacts"][0]["source"] = ["../escape"]
        with self.assertRaises(rc.ManifestError):
            rc.validate_manifest(bad)
        with self.assertRaises(rc.ManifestError):
            rc.expand_path("relative/destination", self.env)

    def test_states_absent_staged_activation_required_and_drifted(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "dev/seat", [], "opencode", "watch", "host",
                       "tmux=0:3.0 win=opencode", "3")
        control = self.control(artifact)
        with mock.patch.object(control, "_select_seats", return_value=[seat]):
            rec = control.probe(artifact, "opencode", seat)
            self.assertEqual(rec["state"], "ABSENT")
            rec = control.stage(artifact, "opencode", seat)
            self.assertEqual(rec["state"], "STAGED")
            target = rc.expand_path(artifact["target"], self.env)
            target.parent.mkdir(parents=True)
            target.write_text("different\n")
            rec = control.probe(artifact, "opencode", seat)
            self.assertEqual(rec["state"], "DRIFTED")
            target.write_text((self.canonical / artifact["source"][0]).read_text())
            rec = control.probe(artifact, "opencode", seat)
            self.assertEqual(rec["state"], "ACTIVATION_REQUIRED")

    def test_stage_is_exact_and_idempotent(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host", "", None)
        control = self.control(artifact)
        a = control.stage(artifact, "opencode", seat)
        b = control.stage(artifact, "opencode", seat)
        self.assertEqual(a["desired_version"], b["desired_version"])
        staged = control._stage_dir(artifact, "opencode", seat, a["desired_version"])
        self.assertTrue((staged / "metadata.json").exists())
        self.assertEqual((staged / "files" / artifact["source"][0]).read_text(),
                         (self.repo / artifact["source"][0]).read_text())
        (staged / "files" / artifact["source"][0]).write_text("tampered\n")
        self.assertFalse(control._staged(artifact, "opencode", seat, a["desired_version"]))
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "opencode", seat)

    def test_spec_identity_covers_behavior_fields(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        base = rc.desired_version(artifact, self.repo)
        changed = dict(artifact, observer="different-proof")
        self.assertNotEqual(base, rc.desired_version(changed, self.repo))

    def test_external_private_sources_stage_and_reject_unmerged_changes(self):
        working = self.base / "private-working"
        canonical = self.base / "private-canonical"
        for root in (working, canonical):
            (root / "plugins").mkdir(parents=True)
            (root / "plugins/demo.ts").write_text("private configuration\n")
        artifact = self.artifact(source_roots={"working": str(working),
                                                "canonical": str(canonical)})
        control = self.control(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host", "", None)
        control.stage(artifact, "opencode", seat)
        (canonical / "plugins/demo.ts").write_text("different published version\n")
        self.assertEqual(control._installed_source_version(artifact),
                         rc.desired_version(artifact, self.canonical, canonical=True))
        self.assertNotEqual(control._installed_source_version(artifact),
                            rc.desired_version(artifact, self.repo))
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "opencode", seat)
        self.assertFalse(rc.expand_path(artifact["target"], self.env).exists())
        (canonical / "plugins/demo.ts").write_text("private configuration\n")
        control.install(artifact, "opencode", seat)
        self.assertEqual(rc.expand_path(artifact["target"], self.env).read_text(),
                         "private configuration\n")

    def test_runtime_override_applies_to_default_ledger(self):
        control = self.control(self.artifact())
        self.assertEqual(control.ledger_db,
                         self.runtime / "state/fleet-orchestrator/dispatch-ledger.sqlite3")

    def test_install_refuses_unmerged_and_drift(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host", "", None)
        control = self.control(artifact)
        control.stage(artifact, "opencode", seat)
        (self.repo / artifact["source"][0]).write_text("unmerged\n")
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "opencode", seat)
        (self.repo / artifact["source"][0]).write_text(
            (self.canonical / artifact["source"][0]).read_text())
        target = rc.expand_path(artifact["target"], self.env)
        target.parent.mkdir(parents=True)
        target.write_text("foreign\n")
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "opencode", seat)
        self.assertEqual(target.read_text(), "foreign\n")

    def test_tmux_process_lookup_uses_full_session_window_pane(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host",
                       "tmux=other:3.1 win=opencode", "3",
                       session_name="other", pane_index="1")
        env = dict(self.env)
        env["ROLLOUT_TMUX_PANES_JSON"] = json.dumps([
            {"pane": "%a", "session": "0", "window": "3", "pane_index": "0",
             "name": "opencode", "command": "opencode", "pid": 100},
            {"pane": "%b", "session": "other", "window": "3", "pane_index": "1",
             "name": "opencode", "command": "opencode", "pid": 200},
        ])
        control = rc.ControlPlane(self.manifest(artifact), repo=self.repo,
                                  canonical_repo=self.canonical, env=env)
        control._proc_cache = {
            100: (1, ["opencode", "wrong"], 1.0),
            200: (1, ["opencode", "right"], 2.0),
        }
        proc = control._process_for(seat)
        self.assertIsNotNone(proc)
        self.assertEqual(proc.pid, 200)
        self.assertEqual(proc.pane, "%b")

    def test_claude_install_preserves_unrelated_settings(self):
        artifact = self.artifact(
            id="claude-hook", **{"class": "hooks", "harness": "claude",
            "source": ["scripts/reporter.py"], "config": "${HOME}/.claude/settings.json",
            "format": "claude-json", "events": ["UserPromptSubmit", "Stop"],
            "command": "python3 /canonical/reporter.py --harness claude",
            "install": "claude-json-hooks", "activation": "process-restart"})
        self.write_sources(artifact, "print('ok')\n")
        settings = rc.expand_path(artifact["config"], self.env)
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark", "hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": "keep"}]}
        ]}}))
        settings.chmod(0o600)
        seat = rc.Seat("seat-c", "h", [], "claude", "watch", "host", "", None)
        control = self.control(artifact)
        control.stage(artifact, "claude", seat)
        control.install(artifact, "claude", seat)
        first = settings.read_text()
        control.install(artifact, "claude", seat)
        data = json.loads(settings.read_text())
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(settings.read_text(), first)
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(data["hooks"]["Stop"]), 1)

    def test_codex_exact_hook_not_substring_and_trust_block(self):
        artifact = self.artifact(
            id="codex-hook", **{"class": "hooks", "harness": "codex",
            "source": ["scripts/reporter.py"], "config": "${HOME}/.codex/config.toml",
            "format": "codex-toml", "events": ["Stop"],
            "command": "python3 /canonical/reporter.py --harness codex",
            "install": "codex-stage-hooks", "trust_required": True})
        self.write_sources(artifact, "print('ok')\n")
        cfg = rc.expand_path(artifact["config"], self.env)
        cfg.parent.mkdir(parents=True)
        cfg.write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype="command"\ncommand="echo reporter.py"\ntimeout=5\n')
        control = self.control(artifact)
        version, installed, trusted, _mtime, detail = control._codex_hook_status(artifact)
        self.assertFalse(installed)
        self.assertFalse(trusted)
        self.assertIn("missing", detail)
        cfg.write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype="command"\ncommand="python3 /canonical/reporter.py --harness codex"\ntimeout=5\n')
        version, installed, trusted, _mtime, _detail = control._codex_hook_status(artifact)
        self.assertTrue(installed)
        self.assertFalse(trusted)

    def test_skill_links_do_not_claim_context_loaded(self):
        artifact = {
            "id": "skills", "class": "skill-links", "source_globs": ["skills/*/SKILL.md"],
            "harness": "multi", "harnesses": ["claude"], "seat": "machine",
            "activation": "invocation", "target_state": "INSTALLED", "install": "symlink-tree",
            "target_roots": {"claude": "${HOME}/.claude/skills"},
        }
        bundle = self.base / "public packages/custom-location"
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text("# demo\n")
        artifact.pop("source_globs")
        artifact["source"] = ["scripts/skill-sources.json"]
        self.settings.write_text(json.dumps({"rollout": {"skill_sources": {"demo": str(bundle)}}}))
        for root in (self.repo, self.canonical):
            selection = root / "scripts/skill-sources.json"
            selection.parent.mkdir(parents=True)
            selection.write_text(json.dumps({"demo": str(bundle)}))
        target = self.home / ".claude/skills/demo"
        target.parent.mkdir(parents=True)
        target.symlink_to(bundle)
        control = self.control(artifact)
        rec = control.probe(artifact, "claude", rc.Seat("machine", "machine", [], "claude", "none", "host", "", None))
        self.assertEqual(rec["state"], "INSTALLED")
        self.assertIsNone(rec["context_loaded"])
        version = rec["desired_version"]
        (bundle / "SKILL.md").write_text("# demo changed\n")
        self.assertNotEqual(rc.desired_version(artifact, self.repo), version)
        target.unlink()
        seat = rc.Seat("machine", "machine", [], "claude", "none", "host", "", None)
        control.stage(artifact, "claude", seat)
        installed = control.install(artifact, "claude", seat)
        self.assertEqual(installed["state"], "INSTALLED")
        self.assertEqual(target.resolve(), bundle.resolve())
        target.unlink()
        target.mkdir()
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "claude", seat)

    def test_old_installer_is_not_executed_by_read_only_inventory(self):
        installer = self.repo / "scripts/link-global-skills.sh"
        installer.parent.mkdir(parents=True)
        marker = self.base / "unexpected-install"
        import shlex
        installer.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\n")
        with self.assertRaises(rc.OperationError):
            rc.skill_sources(self.repo)
        self.assertFalse(marker.exists())

    def test_missing_configured_source_is_unknown_not_empty(self):
        self.settings.write_text(json.dumps({"rollout": {"skill_sources": {"demo": str(self.base / "absent")}}}))
        with self.assertRaises(rc.OperationError):
            rc.skill_sources(self.repo)

    def test_published_service_unit_keeps_exact_file_check_without_process_probe(self):
        artifact = self.artifact(**{
            "id": "dispatcher-unit", "class": "published-file",
            "source": ["scripts/agent-bus-dispatcher.service"],
            "harness": "agent-bus", "seat": "machine",
            "activation": "immediate", "target_state": "INSTALLED",
            "install": "status-only",
            "target": "${XDG_CONFIG_HOME}/systemd/user/agent-bus-dispatcher.service",
            "copy_source": "scripts/agent-bus-dispatcher.service",
        })
        self.write_sources(artifact, "[Service]\nExecStart=/canonical/dispatch\n")
        target = rc.expand_path(artifact["target"], self.env)
        target.parent.mkdir(parents=True)
        target.write_bytes((self.canonical / artifact["source"][0]).read_bytes())
        control = self.control(artifact)
        with mock.patch.object(
            control, "_find_process", side_effect=AssertionError("process probe forbidden")
        ):
            rec = control.probe(
                artifact, "agent-bus",
                rc.Seat("machine", "machine", [], "agent-bus", "none", "host", "", None),
            )
        self.assertEqual(rec["state"], "INSTALLED", rec["detail"])
        target.write_text("drifted\n")
        rec = control.probe(
            artifact, "agent-bus",
            rc.Seat("machine", "machine", [], "agent-bus", "none", "host", "", None),
        )
        self.assertEqual(rec["state"], "DRIFTED", rec["detail"])

    def test_dsh_status_does_not_copy_or_edit_composition(self):
        artifact = self.artifact(
            id="dsh-plugin", **{"class": "dsh-plugin", "harness": "dsh",
            "source": ["plugins/dsh/demo"], "package": "demo",
            "install": "status-only", "activation": "profile-restart",
            "target_state": "INSTALLED"})
        self.write_sources(artifact)
        profile = self.home / ".dsh-work/profiles/p1"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text("- id: credentials\n")
        seat = rc.Seat("seat-d", "h", [], "dsh", "pull", "host",
                       "tmux=0:2.0 win=node", "2", profile="p1")
        control = self.control(artifact)
        control.stage(artifact, "dsh", seat)
        with self.assertRaises(rc.OperationError):
            control.install(artifact, "dsh", seat)
        self.assertFalse((profile / "node_modules/demo").exists())
        self.assertEqual((profile / "cordis.patch.yml").read_text(), "- id: credentials\n")

    def test_status_failure_exit_contract_is_explicit(self):
        self.assertIn("DRIFTED", rc.FAILURE_STATES)
        self.assertIn("BLOCKED_TRUST", rc.FAILURE_STATES)
        self.assertNotIn("ACTIVATION_REQUIRED", rc.FAILURE_STATES)
        # UNKNOWN is honest cannot-read, distinct from FAILED: it must never
        # count as a refuting probe, and the status verb handles its own
        # non-zero exit for it separately.
        self.assertNotIn("UNKNOWN", rc.FAILURE_STATES)
        self.assertIn("UNKNOWN", rc.STATES)

    def reexec_artifact(self, cron_line):
        return self.artifact(**{
            "id": "orc-engine", "class": "reexec", "harness": "orc",
            "seat": "fleet-orchestrator-cron", "activation": "reexec",
            "install": "merge-only", "source": ["scripts/engine.py"],
            "cron_exact_line": cron_line,
            "observation_log": "${NOTES_RUNTIME_DIR}/logs/engine.log",
            "max_observation_age_s": 900,
        })

    def _fresh_engine_log(self, started_ns=None, completed_ns=None):
        log = self.runtime / "logs" / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        now_ns = time.time_ns()
        started_ns = started_ns or now_ns - 2_000_000_000
        completed_ns = completed_ns or now_ns - 1_000_000_000
        log.write_text(
            "OK tick done: 0 owed pairs, 0 nudges,"
            f" process_started_ns={started_ns}, completed_ns={completed_ns}\n")
        return log

    def test_reexec_exact_cron_line_verifies_and_prints_the_line(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        now_ns = time.time_ns()
        self._fresh_engine_log(now_ns - 2_000_000_000,
                               now_ns - 1_000_000_000)
        source_ns = now_ns - 10_000_000_000
        os.utime(self.canonical / "scripts/engine.py",
                 ns=(source_ns, source_ns))
        self.env["ROLLOUT_CRONTAB_TEXT"] = (
            "# comment line\n" + rc.expand_vars(expected, self.env) + "\n")
        control = self.control(artifact)
        (record,) = control.records()
        self.assertEqual(record["state"], "VERIFIED", record["detail"])
        self.assertIn("cron=exact; line=", record["detail"])
        self.assertIn("observation=fresh-after-source", record["detail"])

    def test_reexec_fresh_log_before_current_source_requires_activation(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        now_ns = time.time_ns()
        self._fresh_engine_log(now_ns - 10_000_000_000,
                               now_ns - 9_000_000_000)
        source_ns = now_ns - 1_000_000_000
        os.utime(self.canonical / "scripts/engine.py",
                 ns=(source_ns, source_ns))
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"

        (record,) = self.control(artifact).records()

        self.assertEqual(record["state"], "ACTIVATION_REQUIRED", record["detail"])
        self.assertIsNone(record["activated_version"])
        self.assertIsNone(record["observed_version"])
        self.assertIn("observation=not-after-source", record["detail"])

    def test_reexec_run_started_before_source_is_not_verified_if_it_finishes_after(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        now_ns = time.time_ns()
        self._fresh_engine_log(now_ns - 10_000_000_000,
                               now_ns - 1_000_000_000)
        source_ns = now_ns - 5_000_000_000
        os.utime(self.canonical / "scripts/engine.py",
                 ns=(source_ns, source_ns))
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"

        (record,) = self.control(artifact).records()

        self.assertEqual(record["state"], "ACTIVATION_REQUIRED", record["detail"])
        self.assertIsNone(record["activated_version"])
        self.assertIsNone(record["observed_version"])
        self.assertIn("observation=not-after-source", record["detail"])

    def test_reexec_old_log_after_source_is_still_not_verified(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        now_ns = time.time_ns()
        self._fresh_engine_log(now_ns - 1_100_000_000_000,
                               now_ns - 1_000_000_000_000)
        source_ns = now_ns - 2_000_000_000_000
        os.utime(self.canonical / "scripts/engine.py",
                 ns=(source_ns, source_ns))
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"

        (record,) = self.control(artifact).records()

        self.assertEqual(record["state"], "ACTIVATION_REQUIRED", record["detail"])
        self.assertIsNone(record["activated_version"])
        self.assertIsNone(record["observed_version"])
        self.assertIn("observation=stale", record["detail"])

    def test_reexec_future_log_is_not_current_version_evidence(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        now_ns = time.time_ns()
        self._fresh_engine_log(now_ns - 2_000_000_000,
                               now_ns + 10_000_000_000)
        source_ns = now_ns - 10_000_000_000
        os.utime(self.canonical / "scripts/engine.py",
                 ns=(source_ns, source_ns))
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"

        (record,) = self.control(artifact).records()

        self.assertEqual(record["state"], "ACTIVATION_REQUIRED", record["detail"])
        self.assertIsNone(record["activated_version"])
        self.assertIsNone(record["observed_version"])
        self.assertIn("observation=future", record["detail"])

    def test_reexec_legacy_log_without_receipt_requires_activation(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        log = self.runtime / "logs" / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("OK tick done: 0 owed pairs, 0 nudges\n")
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"

        (record,) = self.control(artifact).records()

        self.assertEqual(record["state"], "ACTIVATION_REQUIRED", record["detail"])
        self.assertIsNone(record["activated_version"])
        self.assertIsNone(record["observed_version"])
        self.assertIn("observation=receipt-absent", record["detail"])

    def test_reexec_unreadable_receipt_is_unknown_not_absent(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"
        control = self.control(artifact)

        with mock.patch.object(
                rc, "reexec_receipt",
                side_effect=rc.OperationError("observation log denied")):
            (record,) = control.records()

        self.assertEqual(record["state"], "UNKNOWN", record["detail"])
        self.assertIn("observation log denied", record["detail"])

    def test_reexec_receipt_distinguishes_missing_from_unreadable(self):
        missing = self.runtime / "logs" / "missing.log"
        self.assertEqual(rc.reexec_receipt(missing), (None, None))
        with mock.patch.object(Path, "open",
                               side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(rc.OperationError, "denied"):
                rc.reexec_receipt(self.runtime / "logs" / "denied.log")

    def test_reexec_missing_canonical_source_preserves_absent_and_staged(self):
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        self.env["ROLLOUT_CRONTAB_TEXT"] = rc.expand_vars(expected, self.env) + "\n"
        control = self.control(artifact)
        source = self.canonical / "scripts/engine.py"
        source.unlink()

        (record,) = control.records()
        self.assertEqual(record["state"], "ABSENT", record["detail"])

        seat = control.targets(artifact)[0][1]
        control.stage(artifact, "orc", seat)
        (record,) = control.records()
        self.assertEqual(record["state"], "STAGED", record["detail"])

    def test_reexec_prefixed_cron_line_is_drifted_not_verified(self):
        # The demonstrated hazard: the live invocation with an injected env
        # prefix (NW_TMUX_SERVER=...) contains the old substring, so a
        # substring assertion called it VERIFIED. Exact-line must call it
        # DRIFTED and print the live line it matched.
        expected = ("4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick "
                    ">> ${NOTES_RUNTIME_DIR}/logs/engine.log 2>&1")
        artifact = self.reexec_artifact(expected)
        self.write_sources(artifact)
        self._fresh_engine_log()
        live = rc.expand_vars(expected, self.env).replace(
            "* * * * ", "* * * * NW_TMUX_SERVER=tmux37 ")
        self.env["ROLLOUT_CRONTAB_TEXT"] = live + "\n"
        control = self.control(artifact)
        (record,) = control.records()
        self.assertEqual(record["state"], "DRIFTED", record["detail"])
        self.assertIn("cron=drifted", record["detail"])
        self.assertIn("NW_TMUX_SERVER=tmux37", record["detail"],
                      "detail must print the live drifted line")

    def test_reexec_missing_cron_line_is_not_verified(self):
        artifact = self.reexec_artifact(
            "4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick")
        self.write_sources(artifact)
        self._fresh_engine_log()
        self.env["ROLLOUT_CRONTAB_TEXT"] = "0 0 * * * /bin/true\n"
        control = self.control(artifact)
        (record,) = control.records()
        self.assertNotEqual(record["state"], "VERIFIED")
        self.assertIn("cron=missing", record["detail"])

    def test_unreadable_bus_registry_marks_only_member_rows_unknown(self):
        # The demonstrated blocker: one garbage registry aborted the ENTIRE
        # status run (FAIL exit 3, zero rows). A broken sensor must not
        # silence the whole truth layer: sentinel-seat artifacts still
        # report, and only the member-needing row goes UNKNOWN.
        (self.cfg / "agent-bus-v3.sqlite3").write_bytes(b"NOT A SQLITE FILE")
        cron_line = "4-59/5 * * * * ${NOTES_RUNTIME_DIR}/engine.py tick"
        engine = self.reexec_artifact(cron_line)
        member = self.artifact()  # registered-seats plugin: needs _members
        self.write_sources(engine)
        self.write_sources(member)
        self._fresh_engine_log()
        self.env["ROLLOUT_CRONTAB_TEXT"] = ""
        manifest = self.manifest(engine)
        manifest["artifacts"].append(member)
        rc.validate_manifest(manifest)
        control = rc.ControlPlane(manifest, repo=self.repo,
                                  canonical_repo=self.canonical, env=self.env)
        records = control.records()
        by_id = {record["artifact"]: record for record in records}
        self.assertEqual(len(records), 2, records)
        self.assertNotEqual(by_id["orc-engine"]["state"], "UNKNOWN")
        self.assertEqual(by_id["demo"]["state"], "UNKNOWN")
        self.assertEqual(by_id["demo"]["seat"], "unresolved")
        self.assertIn("cannot enumerate target seats", by_id["demo"]["detail"])
        summary = control.summary(records)
        self.assertEqual(summary["unknown"], 1)

    def test_install_has_no_restart_service_tmux_or_bus_calls(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host", "", None)
        control = self.control(artifact)
        control.stage(artifact, "opencode", seat)
        with mock.patch.object(rc.subprocess, "run", side_effect=AssertionError("subprocess forbidden")):
            control.install(artifact, "opencode", seat)

    def test_exact_copy_install_is_true_noop(self):
        artifact = self.artifact()
        self.write_sources(artifact)
        seat = rc.Seat("seat-1", "h", [], "opencode", "watch", "host", "", None)
        control = self.control(artifact)
        control.stage(artifact, "opencode", seat)
        target = rc.expand_path(artifact["target"], self.env)
        target.parent.mkdir(parents=True)
        target.write_bytes((self.canonical / artifact["source"][0]).read_bytes())
        before = target.stat().st_mtime_ns
        control.install(artifact, "opencode", seat)
        self.assertEqual(target.stat().st_mtime_ns, before,
                         "an exact repeat install must not manufacture a restart requirement")


if __name__ == "__main__":
    unittest.main()
