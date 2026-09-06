import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chat_mentions import cli, config, drafts


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema": "chat-mentions/v1", "state_directory": "state"})
    )
    return path


def command(configured, *args):
    return cli.main(["--config", str(configured), *args])


def test_opaque_identifiers_cannot_escape_draft_directory(configured, tmp_path, capsys):
    assert (
        command(
            configured,
            "new",
            "--chat-id",
            "chat/one",
            "--msg-id",
            "../../outside",
            "--body",
            "draft",
        )
        == 0
    )
    path = Path(capsys.readouterr().out.strip())
    assert path.resolve().is_relative_to(tmp_path / "state/drafts")
    assert path.stat().st_mode & 0o777 == 0o600
    assert drafts.parse(path.read_text())[0]["msg_id"] == "../../outside"


def test_two_chats_can_share_message_id_without_closing_each_others_event(
    configured, tmp_path, capsys
):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue.jsonl").write_text(
        "".join(
            json.dumps({"msg_id": "same", "chat_id": chat}) + "\n"
            for chat in ["one", "two"]
        )
    )
    command(
        configured, "new", "--chat-id", "one", "--msg-id", "same", "--body", "first"
    )
    capsys.readouterr()
    command(configured, "open")
    output = capsys.readouterr().out
    assert "chat two" in output and "chat one" not in output
    command(
        configured, "new", "--chat-id", "two", "--msg-id", "same", "--body", "second"
    )
    assert len(list((state / "drafts").glob("*/*.md"))) == 2
    assert command(configured, "show", "same") == 1


@pytest.mark.parametrize("kind", ["plain", "file-symlink", "directory-symlink"])
def test_dismiss_cannot_modify_files_outside_draft_box(configured, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "note.md"
    path.write_text("keep me")
    if kind != "plain":
        root = tmp_path / "state/drafts"
        root.mkdir(parents=True)
        if kind == "file-symlink":
            directory = root / "2025-01-02"
            directory.mkdir()
            link = directory / "linked.md"
            link.symlink_to(path)
        else:
            (root / "2025-01-02").symlink_to(outside)
            link = root / "2025-01-02/note.md"
        ref = link
    else:
        ref = path
    assert command(configured, "dismiss", str(ref)) == 1
    assert path.read_text() == "keep me"


def test_metadata_cannot_inject_status(configured, tmp_path):
    assert (
        command(
            configured,
            "new",
            "--chat-id",
            "synthetic-chat",
            "--msg-id",
            "synthetic-message",
            "--topic",
            "label\nstatus: sent",
            "--body",
            "draft",
        )
        == 1
    )
    assert not list((tmp_path / "state").rglob("*.md"))


def test_corrupt_queue_does_not_report_fully_handled(configured, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue.jsonl").write_text("{incomplete")
    assert command(configured, "open") == 1
    assert "fully handled" not in capsys.readouterr().out


def test_mark_sent_requires_nonempty_delivery_evidence(configured, tmp_path):
    command(
        configured,
        "new",
        "--chat-id",
        "synthetic-chat",
        "--msg-id",
        "synthetic-message",
        "--body",
        "draft",
    )
    assert command(configured, "mark-sent", "synthetic-message", "--note", " ") == 1
    stored = next((tmp_path / "state/drafts").glob("*/*.md"))
    assert drafts.parse(stored.read_text())[0]["status"] == "pending"


def test_home_environment_and_relative_config_paths(configured, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "moved home"))
    monkeypatch.setenv("EXAMPLE_CACHE", str(tmp_path / "cache"))
    configured.write_text(
        json.dumps(
            {
                "schema": "chat-mentions/v1",
                "state_directory": "~/state",
                "read_token_file": "$EXAMPLE_CACHE/read.json",
            }
        )
    )
    result = config.load(configured)
    assert result["state_directory"] == tmp_path / "moved home/state"
    assert result["read_token_file"] == tmp_path / "cache/read.json"
    assert not result["state_directory"].exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"schema": "unknown"},
        {"collection_enabled": "yes"},
        {"collection_enabled": True},
        {"state_directory": "$UNSET_EXAMPLE_DIRECTORY"},
        {"draft_expiry_hours": 0},
        {"lock_file": "config.json"},
        {"unrecognized": True},
    ],
)
def test_invalid_configuration_fails_without_writes(configured, tmp_path, updates):
    settings = json.loads(configured.read_text())
    settings.update(updates)
    configured.write_text(json.dumps(settings))
    assert command(configured, "doctor") == 1
    assert not (tmp_path / "state").exists()


def test_wrapper_works_from_arbitrary_directory(configured, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/mentions"
    result = subprocess.run(
        [str(script), "doctor"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            CHAT_MENTIONS_PYTHON=sys.executable,
            CHAT_MENTIONS_CONFIG=str(configured),
        ),
    )
    assert result.returncode == 0 and "collection: disabled" in result.stdout
    assert not (tmp_path / "state").exists()
