#!/usr/bin/env python3

import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_stop", ROOT / "scripts" / "agent-bus-codex-stop-hook.py")
hook = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(hook)


class CodexStopHookTest(unittest.TestCase):
    def test_returns_fixed_continuation_after_claim(self):
        def run(args, **kwargs):
            if "notify-claim" in args:
                return mock.Mock(returncode=0, stdout=json.dumps({"notify": True, "agent_id": "a", "generation": 3}))
            return mock.Mock(returncode=0, stdout="")

        output = io.StringIO()
        payload = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "$(touch /tmp/evil)"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), mock.patch.object(sys, "stdout", output), mock.patch.object(hook, "tmux_pane", return_value="0:8.0"), mock.patch.object(hook.subprocess, "run", side_effect=run):
            self.assertEqual(hook.main(), 0)
        decision = json.loads(output.getvalue())
        self.assertEqual(decision, {"decision": "block", "reason": hook.REMINDER})
        self.assertNotIn("evil", output.getvalue())

    def test_active_stop_hook_does_not_recurse(self):
        payload = {"hook_event_name": "Stop", "stop_hook_active": True}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), mock.patch.object(hook.subprocess, "run") as run:
            self.assertEqual(hook.main(), 0)
            run.assert_not_called()

    def test_ignores_wrong_event(self):
        with mock.patch.object(sys, "stdin", io.StringIO('{"hook_event_name":"PostToolUse"}')), mock.patch.object(hook.subprocess, "run") as run:
            self.assertEqual(hook.main(), 0)
            run.assert_not_called()

    def test_pane_lookup_uses_the_selected_tmux_server(self):
        result = mock.Mock(
            returncode=0,
            stdout=f"{os.getpid()} 0:14.0\n",
        )
        with mock.patch.object(
            hook.tmux_runtime, "base_cmd", return_value=["tmux", "-L", "fleet-alpha"]
        ), mock.patch.object(hook.subprocess, "run", return_value=result) as run:
            self.assertEqual(hook.tmux_pane(), "0:14.0")
        self.assertEqual(run.call_args.args[0][:3],
                         ["tmux", "-L", "fleet-alpha"])


if __name__ == "__main__":
    unittest.main()
