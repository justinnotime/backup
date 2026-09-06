"""The rehearsal's fake terminal must not discard partially received input."""

import os
import select
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_fake_terminal_preserves_input_across_read_timeout(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text('#!/bin/sh\n[ "$3" != has-session ]\n')
    tmux.chmod(0o755)
    stage = tmp_path / "stage"
    env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}
    subprocess.run(
        ["bash", str(ROOT / "scripts/fleet-staging.sh"), "up", str(stage)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    control = stage / "ctl/w1"
    control.write_text("busy\n")
    log = stage / "logs/w1.log"
    proc = subprocess.Popen(
        ["bash", str(stage / "bin/agent-loop.sh")],
        env={**env, "FAKE_CTL": str(control), "FAKE_LOG": str(log)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def next_line():
        assert select.select([proc.stdout], [], [], 8)[0], "fake terminal stopped responding"
        return proc.stdout.readline()

    def wait_for_prompt():
        # A successful read logs the complete line before printing this prompt.
        for _ in range(4):
            if next_line() == b">\n":
                return
        raise AssertionError("fake terminal did not finish the input line")

    try:
        assert next_line().startswith(b"Working")
        proc.stdin.write(b"[synthetic peer header] ")
        # The next status render occurs after the timed read returns. This
        # observes the timeout itself instead of guessing a scheduling delay.
        assert next_line().startswith(b"Working")
        assert not log.exists() or log.read_bytes() == b""
        proc.stdin.write(b"ORC reminder: synthetic task\n")
        wait_for_prompt()
        expected = b"[synthetic peer header] ORC reminder: synthetic task\n"
        assert log.read_bytes() == expected
        proc.stdin.write(b"next message\n")
        wait_for_prompt()
        assert log.read_bytes() == expected + b"next message\n"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
