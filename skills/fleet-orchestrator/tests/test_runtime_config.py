from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/lib"))
import runtime_config as cfg


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {"HOME": str(self.root / "another user")}

    def test_absent_default_is_local_but_explicit_missing_is_error(self):
        self.assertEqual(cfg.read(self.env), {})
        with self.assertRaisesRegex(ValueError, "explicit"):
            cfg.read({**self.env, "FLEET_ORCHESTRATOR_CONFIG": str(self.root / "missing")})

    def test_home_and_xdg_paths_are_caller_owned(self):
        self.assertEqual(cfg.path("runtime_dir", "~/state", env=self.env),
                         Path(self.env["HOME"]) / "state")
        self.assertEqual(cfg.config_path({**self.env, "XDG_CONFIG_HOME": str(self.root / "config")}),
                         self.root / "config/fleet-orchestrator/config.json")

    def test_duplicate_keys_and_wrong_schema_are_rejected_without_contents(self):
        path = self.root / "config.json"
        env = {**self.env, "FLEET_ORCHESTRATOR_CONFIG": str(path)}
        for raw in ('{"schema":"wrong"}', '{"private":"sensitive-placeholder","private":2}', '[]'):
            path.write_text(raw)
            with self.assertRaises(ValueError) as result:
                cfg.read(env)
            self.assertNotIn("sensitive-placeholder", str(result.exception))

    def test_commands_remain_argument_arrays(self):
        path = self.root / "config.json"
        path.write_text('{"hook":["/bin/echo","literal $(do-not-execute)","${HOME}/a b"]}')
        env = {**self.env, "FLEET_ORCHESTRATOR_CONFIG": str(path)}
        self.assertEqual(cfg.command("hook", env=env),
                         ["/bin/echo", "literal $(do-not-execute)", self.env["HOME"] + "/a b"])
        path.write_text('{"hook":"echo unsafe"}')
        with self.assertRaises(ValueError):
            cfg.command("hook", env=env)

    def test_invalid_encoding_does_not_echo_configuration(self):
        path = self.root / "config.json"
        path.write_bytes(b"private-placeholder\xff")
        with self.assertRaisesRegex(ValueError, "cannot be read") as result:
            cfg.read({**self.env, "FLEET_ORCHESTRATOR_CONFIG": str(path)})
        self.assertNotIn("placeholder", str(result.exception))


if __name__ == "__main__":
    unittest.main()
