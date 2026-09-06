"""Exercise the portable plugin without contacting a real harness or ledger."""
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("override", [False, True])
def test_dsh_plugin_uses_public_command_or_explicit_executable(tmp_path, override):
    home = tmp_path / "another user"
    command = str(home / "custom programs/reporter") if override else "orc-turn-report"
    code = '''import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
const calls=[];
childProcess.spawn=(...args)=>{calls.push(args);return {unref(){},on(){}}};
syncBuiltinESMExports();
const plugin=await import(process.argv[1]);
const handlers={};plugin.apply({on:(name,callback)=>{handlers[name]=callback}});
await handlers['agent/pre-step']({},async()=>{});
console.log(JSON.stringify(calls));
'''
    env = {**os.environ, "HOME": str(home)}
    env.pop("ORC_TURN_REPORT_COMMAND", None)
    if override:
        env["ORC_TURN_REPORT_COMMAND"] = command
    source = ROOT / "plugins/dsh/dsh-turn-report/index.js"
    output = subprocess.check_output(
        ["node", "--input-type=module", "-e", code, str(source)], env=env, text=True)
    calls = json.loads(output)
    assert len(calls) == 1
    assert calls[0][0] == command
    assert calls[0][1] == ["--kind", "start", "--harness", "dsh"]


@pytest.mark.parametrize("plugin", ["dsh", "opencode"])
def test_missing_reporter_does_not_terminate_plugin_host(tmp_path, plugin):
    if plugin == "dsh":
        source = ROOT / "plugins/dsh/dsh-turn-report/index.js"
        code = """const plugin=await import(process.argv[1]);
const handlers={};plugin.apply({on:(name,callback)=>{handlers[name]=callback}});
await handlers['agent/pre-step']({},async()=>{});
await new Promise(resolve=>setTimeout(resolve,100));
console.log('host survived');
"""
    else:
        # This plugin has only erased type annotations; execute its emitted JS
        # to verify the same asynchronous process-error path on supported Node.
        source = tmp_path / "turn-report.mjs"
        text = (ROOT / "plugins/opencode/turn-report.ts").read_text()
        text = text.replace('import type { Plugin } from "@opencode-ai/plugin"\n', '')
        text = text.replace('kind: "start" | "end"', 'kind').replace(': Plugin =', ' =')
        source.write_text(text)
        code = """const plugin=await import(process.argv[1]);
const hooks=await plugin.default();await hooks['chat.message']();
await new Promise(resolve=>setTimeout(resolve,100));
console.log('host survived');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, str(source)],
        env={**os.environ, "ORC_TURN_REPORT_COMMAND": str(tmp_path / "absent-reporter")},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "host survived"
