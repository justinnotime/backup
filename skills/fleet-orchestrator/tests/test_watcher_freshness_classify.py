"""Resident source proof and explicit watcher exception tests."""
import importlib.util
import contextlib
import fcntl
import hashlib
import io
import os
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "watcher_freshness", ROOT / "scripts" / "agent-bus-watcher-freshness.py")
wf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wf)

AID = "aaaa-1111"


def resident_argv(*command: str, source_fd: int = 9) -> list[str]:
    return [
        sys.executable, "-I", "-S", "-c", wf.SNAPSHOT_LOADER, str(source_fd),
        str(wf.BUS), *command,
    ]


class ClassifyReaderTest(unittest.TestCase):
    def test_loaded_source_identity_is_read_from_sealed_memfd(self):
        data = b"sealed source bytes\n"
        fd = os.memfd_create("agent-bus-v3-source", os.MFD_ALLOW_SEALING)
        try:
            os.write(fd, data)
            fcntl.fcntl(fd, wf.F_ADD_SEALS, wf.SOURCE_SEALS)
            identity, error = wf.loaded_source_identity(
                os.getpid(), resident_argv("watch", AID, source_fd=fd)
            )
        finally:
            os.close(fd)
        self.assertEqual(error, "")
        self.assertEqual(identity, "sha256:" + hashlib.sha256(data).hexdigest())

    def test_dispatcher_options_match_but_once_process_does_not(self):
        base = resident_argv("dispatch")
        self.assertTrue(wf.matches_resident_command(base, ["dispatch"]))
        self.assertTrue(wf.matches_resident_command(
            base + ["--interval", "0.2", "--host", "host-a"],
            ["dispatch"],
        ))
        self.assertFalse(wf.matches_resident_command(
            base + ["--once"], ["dispatch"]
        ))

    def test_dispatcher_processes_are_filtered_by_named_fleet(self):
        with mock.patch.dict(os.environ, {
            "NW_FLEET": "alpha", "MATRIX_BUS_CFG": "/cfg/alpha"
        }, clear=False), mock.patch.object(
            wf, "process_environment", side_effect=lambda pid: {
                1: {"NW_FLEET": "alpha", "MATRIX_BUS_CFG": "/cfg/alpha"},
                2: {"NW_FLEET": "beta", "MATRIX_BUS_CFG": "/cfg/beta"},
                3: {},
            }[pid]
        ):
            self.assertTrue(wf.same_fleet_process(1))
            self.assertFalse(wf.same_fleet_process(2))
            self.assertFalse(wf.same_fleet_process(3))

    def test_default_fleet_ignores_named_dispatchers(self):
        env = dict(os.environ)
        env.pop("NW_FLEET", None)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            wf, "process_environment", side_effect=lambda pid: (
                {} if pid == 1 else {"NW_FLEET": "alpha"}
            )
        ):
            self.assertTrue(wf.same_fleet_process(1))
            self.assertFalse(wf.same_fleet_process(2))

    def test_named_local_fleet_matches_by_agent_bus_config(self):
        with mock.patch.dict(os.environ, {
            "NW_FLEET": "alpha", "AGENT_BUS_CFG": "/runtime/alpha/bus"
        }, clear=False), mock.patch.object(
            wf, "process_environment", side_effect=lambda pid: {
                1: {"NW_FLEET": "alpha", "AGENT_BUS_CFG": "/runtime/alpha/bus"},
                2: {"NW_FLEET": "alpha", "AGENT_BUS_CFG": "/runtime/beta/bus"},
            }[pid]
        ):
            self.assertTrue(wf.same_fleet_process(1))
            self.assertFalse(wf.same_fleet_process(2))

    def test_script_text_and_named_memfd_without_exact_loader_do_not_match(self):
        direct = [sys.executable, str(wf.BUS), "watch", AID]
        fake_loader = [
            sys.executable, "-I", "-S", "-c", "pass", "9", str(wf.BUS),
            "watch", AID,
        ]
        nonisolated = [
            sys.executable, "-c", wf.SNAPSHOT_LOADER, "9", str(wf.BUS),
            "watch", AID,
        ]
        self.assertFalse(wf.matches_resident_command(direct, ["watch", AID]))
        self.assertFalse(
            wf.matches_resident_command(fake_loader, ["watch", AID])
        )
        self.assertFalse(
            wf.matches_resident_command(nonisolated, ["watch", AID])
        )

    def test_process_assertion_compares_content_identity(self):
        desired = "sha256:" + "b" * 64
        argv = resident_argv("watch", AID)
        output = io.StringIO()
        failures = []
        with (
            mock.patch.object(wf, "matching_pids", return_value=[4242]),
            mock.patch.object(wf, "cmdline", return_value=argv),
            mock.patch.object(
                wf, "loaded_source_identity", return_value=(desired, "")
            ),
            mock.patch.object(wf, "executable_contract_error", return_value=""),
            contextlib.redirect_stdout(output),
        ):
            wf.assert_process("seat", ["watch", AID], desired, failures)
        self.assertEqual(failures, [])
        self.assertIn("loaded current source", output.getvalue())

        output = io.StringIO()
        failures = []
        with (
            mock.patch.object(wf, "matching_pids", return_value=[4242]),
            mock.patch.object(wf, "cmdline", return_value=argv),
            mock.patch.object(
                wf, "loaded_source_identity", return_value=(desired, "")
            ),
            mock.patch.object(wf, "executable_contract_error", return_value=""),
            contextlib.redirect_stdout(output),
        ):
            wf.assert_process(
                "seat", ["watch", AID], "sha256:" + "c" * 64, failures
            )
        self.assertEqual(failures, ["seat"])
        self.assertIn("loaded sha256:", output.getvalue())

    def test_process_without_sealed_source_is_explicitly_old(self):
        argv = resident_argv("watch", AID)
        output = io.StringIO()
        failures = []
        with (
            mock.patch.object(wf, "matching_pids", return_value=[4242]),
            mock.patch.object(wf, "cmdline", return_value=argv),
            mock.patch.object(
                wf,
                "loaded_source_identity",
                return_value=(None, "expected one sealed source snapshot, found 0"),
            ),
            mock.patch.object(wf, "executable_contract_error", return_value=""),
            contextlib.redirect_stdout(output),
        ):
            wf.assert_process(
                "seat", ["watch", AID], "sha256:" + "d" * 64, failures
            )
        self.assertEqual(failures, ["seat"])
        self.assertIn("no verifiable loaded source", output.getvalue())

    def test_exception_applies_only_when_standard_watcher_is_absent(self):
        kind, pid = wf.classify_reader(
            AID, {AID: {"ruling": "operator said no"}},
            matching_fn=lambda tail: [])
        self.assertEqual(kind, "accepted")
        self.assertEqual(
            wf.classify_reader(
                AID,
                {AID: {"ruling": "operator said no"}},
                matching_fn=lambda tail: [4242],
            ),
            ("standard", 4242),
        )

    def test_standard_watch_process(self):
        def find(tail):
            return [4242] if tail == ["watch", AID] else []
        self.assertEqual(wf.classify_reader(AID, {}, matching_fn=find),
                         ("standard", 4242))

    def test_unread_command_text_does_not_count_as_a_watcher(self):
        self.assertEqual(
            wf.classify_reader(AID, {}, matching_fn=lambda tail: []),
            ("missing", None),
        )

    def test_no_reader_at_all_is_missing(self):
        self.assertEqual(wf.classify_reader(AID, {}, matching_fn=lambda tail: []),
                         ("missing", None))

    def test_exceptions_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "exceptions.json"
            path.write_text(json.dumps({"exceptions": [{
                "agent_id": "synthetic-agent", "ruling": "test exception"
            }]}))
            with mock.patch.object(wf, "EXCEPTIONS_FILE", path):
                entries = wf.load_exceptions()
            self.assertEqual(entries["synthetic-agent"]["ruling"], "test exception")

    def test_no_exceptions_are_bundled(self):
        with mock.patch.object(wf, "EXCEPTIONS_FILE", None):
            self.assertEqual(wf.load_exceptions(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
