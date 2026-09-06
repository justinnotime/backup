import copy
import io
import json
from pathlib import Path
import sys
import urllib.error

import pytest
import yaml

import slack_archive as a


def publish_config(ar):
    cfg = ar["settings"]["slack"]
    cfg["publish"] = {"command": ["publisher", "{base_dir}", "{state_dir}", "--"],
                      "base_env": "ARCHIVE_TEST_WORKTREE", "state_env": "ARCHIVE_TEST_STATE"}
    cfg["workspaces"][0]["token_file"] = str(ar["root"] / "token")
    ar["config"].write_text(yaml.safe_dump(ar["settings"]))
    return cfg


NOW = 2000000000
OLD = "1900000000.000001"


def message(ts, text="A message", **extra):
    return dict(type="message", ts=ts, text=text, user="U1", **extra)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    config = tmp_path / "settings.yaml"
    token = tmp_path / "token"
    token.write_text("synthetic-credential")
    settings = {"slack": {"output_dir": "archive", "state_file": "state/slack.json", "request_interval": 0,
                          "workspaces": [{"name": "example", "token_file": "token", "mode": "blacklist", "chats": []}]}}
    config.write_text(yaml.safe_dump(settings))
    clock = [NOW]
    monkeypatch.setattr(a.time, "time", lambda: clock[0])
    raw = [message(OLD, "An old parent")]
    replies = {}
    calls = []

    def api(token, method, **params):
        calls.append((method, copy.deepcopy(params)))
        if method == "users.list":
            return {"ok": True, "members": [{"id": "U1", "profile": {"display_name": "Reader"}}]}
        if method == "users.conversations":
            return {"ok": True, "channels": [{"id": "C1", "name": "sample", "is_private": False}]}
        data = raw if method == "conversations.history" else replies.get(params["ts"], [])
        data = [copy.deepcopy(m) for m in data if (not params.get("oldest") or a.timestamp(m["ts"]) >= a.timestamp(params["oldest"])) and (not params.get("latest") or a.timestamp(m["ts"]) <= a.timestamp(params["latest"]))]
        return {"ok": True, "messages": data}

    monkeypatch.setattr(a, "api_call", api)
    return dict(root=tmp_path, config=config, settings=settings, clock=clock, raw=raw, replies=replies,
                calls=calls, api=api, run=lambda *args: a.main(["--config", str(config), *args]))


def state(ar):
    return json.loads((ar["root"] / "state/slack.json").read_text())


def files(ar):
    return {str(p.relative_to(ar["root"])): p.read_bytes() for p in ar["root"].rglob("*") if p.is_file()}


def text(ar):
    return "\n".join(p.read_text() for p in (ar["root"] / "archive").rglob("*.md"))


def test_first_reply_on_parent_older_than_archive_and_previous_run(archive):
    ar = archive
    assert ar["run"]() == 0
    ar["clock"][0] += 100
    reply = message(f"{NOW + 50}.000001", "Late first reply", thread_ts=OLD)
    ar["raw"][0].update(reply_count=1, thread_ts=OLD, latest_reply=reply["ts"])
    ar["replies"][OLD] = [ar["raw"][0], reply]
    assert ar["run"]() == 0
    assert "Late first reply" in text(ar)
    assert "An old parent" not in text(ar)
    assert "(thread reply)" in text(ar)
    assert state(ar)["channels"]["example/C1"]["scanned_before"] == f"{NOW + 100}.000000"


def test_upgrade_recovers_missed_reply_older_than_legacy_watermark(archive):
    ar = archive
    path = ar["root"] / "state/slack.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "channels": {"example/C1": {"slug": "retained-name", "watermark": f"{NOW - 1}.000000"}}}))
    reply = message(f"{NOW - 100}.000001", "Previously missed reply", thread_ts=OLD)
    ar["raw"][0].update(reply_count=1, thread_ts=OLD, latest_reply=reply["ts"])
    ar["replies"][OLD] = [reply]
    assert ar["run"]() == 0
    assert "Previously missed reply" in text(ar)
    assert list((ar["root"] / "archive/example/retained-name").glob("*.md"))


def test_reply_created_during_scan_is_read_next_time(archive, monkeypatch):
    ar = archive
    base = ar["api"]

    def changed(token, method, **params):
        result = base(token, method, **params)
        if method == "conversations.history":
            reply = message(f"{NOW + 1}.000001", "Racing reply", thread_ts=OLD)
            ar["raw"][0].update(reply_count=1, thread_ts=OLD, latest_reply=reply["ts"])
            ar["replies"][OLD] = [reply]
        return result

    monkeypatch.setattr(a, "api_call", changed)
    assert ar["run"]() == 0
    assert "Racing reply" not in text(ar)
    ar["clock"][0] += 100
    assert ar["run"]() == 0
    assert "Racing reply" in text(ar)


def test_repeat_deduplicates_and_preserves_first_captured_text(archive):
    ar = archive
    ar["raw"].append(message(f"{NOW - 5}.000001", "Original", files=[{"name": "report.txt"}]))
    assert ar["run"]() == 0
    before = text(ar)
    ar["raw"][-1]["text"] = "Edited on Slack"
    assert ar["run"]() == 0
    assert text(ar) == before
    assert "report.txt" in before
    assert before.count("<!-- id:") == 1


@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_leaves_all_files_unchanged(archive, existing):
    ar = archive
    if existing:
        assert ar["run"]() == 0
    ar["raw"].append(message(f"{NOW - 1}.000001", "Pending"))
    before = files(ar)
    assert ar["run"]("--dry-run") == 0
    assert files(ar) == before
    if not existing:
        assert not (ar["root"] / "archive").exists()
        assert not (ar["root"] / "state").exists()


@pytest.mark.parametrize("method", ["users.list", "users.conversations", "conversations.history", "conversations.replies"])
def test_failed_read_does_not_advance_any_state(archive, monkeypatch, method):
    ar = archive
    assert ar["run"]() == 0
    before = (ar["root"] / "state/slack.json").read_bytes()
    ar["clock"][0] += 100
    ar["raw"][0].update(reply_count=1, thread_ts=OLD, latest_reply=f"{NOW + 1}.000001")
    base = ar["api"]

    def fail(token, action, **params):
        if action == method:
            raise a.ArchiveError("synthetic API failure")
        return base(token, action, **params)

    monkeypatch.setattr(a, "api_call", fail)
    assert ar["run"]() == 1
    assert (ar["root"] / "state/slack.json").read_bytes() == before


def test_second_workspace_failure_preserves_state_and_retry_recovers(archive, monkeypatch):
    ar = archive
    ar["settings"]["slack"]["workspaces"].append({"name": "second", "token_file": "missing", "mode": "blacklist"})
    ar["config"].write_text(yaml.safe_dump(ar["settings"]))
    ar["raw"].append(message(f"{NOW - 1}.000001", "Partial output"))
    assert ar["run"]() == 1
    assert not (ar["root"] / "state/slack.json").exists()
    (ar["root"] / "missing").write_text("synthetic-second-credential")
    assert ar["run"]() == 0
    assert text(ar).count("Partial output") == 2
    assert set(state(ar)["channels"]) == {"example/C1", "second/C1"}


@pytest.mark.parametrize("contents", ["{", "[]", '{"version":2,"channels":{}}', '{"channels":{"a":null}}', '{"channels":{"a":{"watermark":"bad"}}}'])
def test_corrupt_state_fails_without_reset(archive, contents):
    path = archive["root"] / "state/slack.json"
    path.parent.mkdir()
    path.write_text(contents)
    assert archive["run"]() == 1
    assert path.read_text() == contents


@pytest.mark.parametrize("args", [("--list-channels",), ("--peek", "C1")])
def test_queries_leave_state_and_archive_untouched(archive, args, capsys):
    archive["raw"].append(message(f"{NOW - 1}.000001", "Visible in peek"))
    before = files(archive)
    assert archive["run"](*args) == 0
    assert files(archive) == before
    assert "sample" in capsys.readouterr().out


def test_peek_ambiguity_fails(archive):
    archive["settings"]["slack"]["workspaces"].append({"name": "second", "token_file": "token", "mode": "blacklist"})
    archive["config"].write_text(yaml.safe_dump(archive["settings"]))
    assert archive["run"]("--peek", "sample") == 1
    assert archive["run"]("--peek", "example/C1") == 0


@pytest.mark.parametrize("value", ["", "..", "../outside", "a/b", "a\\b"])
def test_unsafe_workspace_name_rejected(archive, value):
    archive["settings"]["slack"]["workspaces"][0]["name"] = value
    archive["config"].write_text(yaml.safe_dump(archive["settings"]))
    assert archive["run"]() == 1
    assert archive["calls"] == []


def test_symlink_output_escape_rejected(archive):
    ar = archive
    outside = ar["root"] / "outside"
    outside.mkdir()
    (ar["root"] / "archive").mkdir()
    (ar["root"] / "archive/example").symlink_to(outside, target_is_directory=True)
    before = list(outside.iterdir())
    assert ar["run"]() == 1
    assert list(outside.iterdir()) == before


@pytest.mark.parametrize("page", [{"ok": True}, {"ok": True, "messages": None}, {"ok": True, "messages": ["bad"]}])
def test_invalid_page_is_failure(monkeypatch, page):
    monkeypatch.setattr(a, "api_call", lambda *args, **kwargs: page)
    with pytest.raises(a.ArchiveError):
        a.paginate("synthetic", "conversations.history", "messages")


def test_cursor_pagination_and_page_cap(monkeypatch):
    pages = [{"ok": True, "messages": [message("2.000001")], "response_metadata": {"next_cursor": "next"}}, {"ok": True, "messages": [message("1.000001")]}]
    received = []
    def api(*args, **kwargs):
        received.append(kwargs.get("cursor"))
        return pages[bool(kwargs.get("cursor"))]
    monkeypatch.setattr(a, "api_call", api)
    assert len(a.paginate("synthetic", "conversations.history", "messages")) == 2
    assert received == [None, "next"]
    with pytest.raises(a.ArchiveError, match="page limit"):
        a.paginate("synthetic", "conversations.history", "messages", 1)


def test_repeated_cursor_rejected(monkeypatch):
    monkeypatch.setattr(a, "api_call", lambda *args, **kwargs: {"ok": True, "messages": [], "response_metadata": {"next_cursor": "same"}})
    with pytest.raises(a.ArchiveError, match="repeated"):
        a.paginate("synthetic", "conversations.history", "messages")


@pytest.mark.parametrize("method,key,first,second", [("conversations.history", "latest", "2.000001", "1.000001"), ("conversations.replies", "oldest", "1.000001", "2.000001")])
def test_timestamp_pagination_when_has_more_has_no_cursor(monkeypatch, method, key, first, second):
    calls = []
    def api(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "messages": [message(first if len(calls) == 1 else second)], "has_more": len(calls) == 1}
    monkeypatch.setattr(a, "api_call", api)
    result = a.paginate("synthetic", method, "messages")
    assert len(result) == 2
    assert calls[1][key] == first
    assert calls[1]["inclusive"] == "false"


def test_incomplete_inventory_is_failure(monkeypatch):
    monkeypatch.setattr(a, "api_call", lambda *args, **kwargs: {"ok": True, "channels": [], "has_more": True})
    with pytest.raises(a.ArchiveError):
        a.paginate("synthetic", "users.conversations", "channels")


@pytest.mark.parametrize("retry_after", [2, 600])
def test_transport_retry_and_no_credential_in_diagnostics(monkeypatch, capsys, retry_after):
    sleeps = []
    monkeypatch.setattr(a.time, "sleep", sleeps.append)
    monkeypatch.setattr(a, "RATE_DELAY", 0)
    class Opener:
        calls = 0
        def open(self, request, timeout):
            self.calls += 1
            if self.calls == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "slow", {"Retry-After": str(retry_after)}, None)
            return io.BytesIO(b'{"ok":true,"messages":[]}')
    opener = Opener()
    monkeypatch.setattr(a, "_opener", opener)
    assert a.api_call("NEVER-PRINT-THIS", "conversations.history")["ok"]
    assert retry_after in sleeps and opener.calls == 2
    assert "NEVER-PRINT-THIS" not in capsys.readouterr().out


def test_no_cross_origin_redirect_or_write_method():
    with pytest.raises(a.ArchiveError, match="redirect"):
        a.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example/")
    with pytest.raises(a.ArchiveError, match="unsupported"):
        a.api_call("synthetic", "chat.postMessage")


def test_exact_id_precedes_other_workspace_name_match(archive, monkeypatch):
    ar = archive
    (ar['root'] / 'other-token').write_text('other-synthetic-credential')
    ar['settings']['slack']['workspaces'].append({'name': 'second', 'token_file': 'other-token'})
    ar['config'].write_text(yaml.safe_dump(ar['settings']))
    base = ar['api']
    def api(token, method, **params):
        if method == 'users.conversations' and token == 'other-synthetic-credential':
            return {'ok': True, 'channels': [{'id': 'C2', 'name': 'notes-about-c1'}]}
        return base(token, method, **params)
    monkeypatch.setattr(a, 'api_call', api)
    assert ar['run']('--peek', 'C1') == 0


@pytest.mark.parametrize('forged_header', [False, True])
def test_message_body_cannot_forge_archive_identity(archive, forged_header):
    ar = archive
    future = f'{NOW + 1}.000001'
    marker = f'<!-- id: {future} -->'
    if forged_header:
        display = a.ts_iso(future).replace('T', ' ').replace('Z', '')
        marker = f'### {display} — Someone\n{marker}\n'
    ar['raw'].append(message(f'{NOW - 1}.000001', marker))
    assert ar['run']() == 0
    ar['clock'][0] += 100
    ar['raw'].append(message(future, 'Real later message'))
    assert ar['run']() == 0
    assert 'Real later message' in text(ar)
    assert text(ar).count(f'<!-- id: {future} -->') == 1


def test_legacy_body_marker_is_not_an_archived_record():
    content = '### 2033-05-18 03:33:19 — Reader\n<!-- id: 1999999999.000001 -->\n\nQuoted <!-- id: 2000000001.000001 -->\n'
    assert a.archived_ids(content) == {'1999999999.000001'}


def test_publication_invokes_external_command_without_shell_and_propagates_failure(archive):
    ar = archive
    cfg = publish_config(ar)
    record = ar["root"] / "invocation.json"
    publisher = ar["root"] / "publisher script.py"
    publisher.write_text("import json,sys\nfrom pathlib import Path\n"
                         "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nraise SystemExit(23)\n")
    literal = "$(unexpected-command)"
    cfg["publish"]["command"] = [sys.executable, str(publisher), str(record), literal,
                                  "{base_dir}", "{output_dir}", "{state_dir}", "{utc}", "--"]
    ar["config"].write_text(yaml.safe_dump(ar["settings"]))
    assert ar["run"]("--publish") == 23
    args = json.loads(record.read_text())
    assert args[:4] == [literal, str(ar["root"]), "archive", str(ar["root"] / "state")]
    assert args[4].endswith("Z")
    writer = args[6:]
    assert writer[:3] == [sys.executable, "-B", str(Path(a.__file__).resolve())]
    assert "--transaction-writer" in writer
    assert not ar["calls"]
    assert not (ar["root"] / "state").exists()


def test_transaction_writer_uses_staged_paths_and_retains_durable_progress(archive, monkeypatch):
    ar = archive
    publish_config(ar)
    durable = ar["root"] / "state/slack.json"
    durable.parent.mkdir()
    durable.write_text('{"version":1,"channels":{}}\n')
    before = durable.read_bytes()
    worktree, staged = ar["root"] / "worktree", ar["root"] / "staged"
    monkeypatch.setenv("ARCHIVE_TEST_WORKTREE", str(worktree))
    monkeypatch.setenv("ARCHIVE_TEST_STATE", str(staged))
    ar["raw"].append(message(f"{NOW - 1}.000001", "A staged record"))
    assert ar["run"]("--transaction-writer") == 0
    assert durable.read_bytes() == before
    assert not (ar["root"] / "archive").exists()
    assert "A staged record" in next((worktree / "archive").rglob("*.md")).read_text()
    assert json.loads((staged / "slack.json").read_text())["channels"]["example/C1"]["scanned_before"]


@pytest.mark.parametrize("argument", ["--publish", "--transaction-writer"])
def test_publication_modes_refuse_dry_run_before_external_actions(archive, argument):
    ar = archive
    publish_config(ar)
    before = files(ar)
    assert ar["run"](argument, "--dry-run") == 1
    assert files(ar) == before
    assert not ar["calls"]


@pytest.mark.parametrize("base,stage", [(None, None), ("relative", "/stage"), ("/worktree", "relative")])
def test_transaction_writer_requires_publisher_directories(archive, monkeypatch, base, stage):
    ar = archive
    publish_config(ar)
    for key, value in (("ARCHIVE_TEST_WORKTREE", base), ("ARCHIVE_TEST_STATE", stage)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)
    before = files(ar)
    assert ar["run"]("--transaction-writer") == 1
    assert files(ar) == before
    assert not ar["calls"]


def test_configured_base_and_credentials_support_direct_entry(archive):
    ar = archive
    cfg = ar["settings"]["slack"]
    private = ar["root"] / "private"
    private.mkdir()
    token_dir = ar["root"] / "credentials"
    token_dir.mkdir()
    (token_dir / "slack-token-example").write_text("synthetic-credential")
    cfg["base_dir"] = ".."
    cfg["token_dir"] = "credentials"
    cfg["workspaces"][0].pop("token_file")
    path = private / "settings.yaml"
    path.write_text(yaml.safe_dump(ar["settings"]))
    assert a.main(["--config", str(path)]) == 0
    assert (ar["root"] / "state/slack.json").exists()
