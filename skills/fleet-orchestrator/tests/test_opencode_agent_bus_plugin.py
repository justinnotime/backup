from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_registers_real_host_and_tmux_metadata() -> None:
    plugin = (ROOT / "plugins" / "opencode" / "agent-bus.ts").read_text(encoding="utf-8")

    assert 'import { hostname, homedir } from "node:os"' in plugin
    assert 'run(adapter, ["tmux-id"])' in plugin
    assert '"opencode", "watch", host, tmux' in plugin
    assert 'process.env.HOSTNAME || "unknown"' not in plugin
    assert '"opencode", "watch", "unknown", "opencode"' not in plugin


def test_plugin_fails_closed_without_a_concrete_tmux_pane() -> None:
    plugin = (ROOT / "plugins" / "opencode" / "agent-bus.ts").read_text(encoding="utf-8")

    assert '!tmux.startsWith("tmux=")' in plugin
    assert '!tmux.includes(" win=")' in plugin
    assert 'tmux.endsWith(" win=")' in plugin


if __name__ == "__main__":
    test_plugin_registers_real_host_and_tmux_metadata()
    test_plugin_fails_closed_without_a_concrete_tmux_pane()
