"""Rehearsal setup must never clear an unrelated caller directory."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_up_refuses_an_unowned_nonempty_directory(tmp_path):
    stage = tmp_path / "important files"
    stage.mkdir()
    retained = stage / "keep.txt"
    retained.write_text("unrelated content")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux = fake_bin / "tmux"
    tmux.write_text("#!/bin/sh\nexit 1\n")
    tmux.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/fleet-staging.sh"), "up", str(stage)],
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "not owned by this harness" in result.stderr
    assert retained.read_text() == "unrelated content"
    assert sorted(p.name for p in stage.iterdir()) == ["keep.txt"]
