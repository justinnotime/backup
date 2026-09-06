import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/link-opencode-plugins.sh"
SOURCE = ROOT / "plugins" / "opencode" / "agent-bus.ts"


def test_installs_plain_file_from_opencode_plugin_directory(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    installed = config_home / "opencode" / "plugins" / "agent-bus.ts"
    assert result.returncode == 0, result.stderr
    assert installed.is_file()
    assert not installed.is_symlink()
    assert installed.read_bytes() == SOURCE.read_bytes()


def test_refreshes_an_existing_plugin_copy(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    installed = config_home / "opencode" / "plugins" / "agent-bus.ts"
    installed.parent.mkdir(parents=True)
    installed.write_text("stale\n", encoding="utf-8")

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert installed.read_bytes() == SOURCE.read_bytes()
