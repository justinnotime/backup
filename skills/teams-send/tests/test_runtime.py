import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import teams_send as app

ORIGINAL_AUTH = app._msal_token


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected live network request")

    monkeypatch.setattr(requests, "request", forbidden)
    monkeypatch.setattr(app, "_msal_token", forbidden)


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / "private config.json"
    settings = {
        "schema": "teams-send/v1",
        "state_directory": "state",
        "read_token_file": "read.json",
        "send_token_file": "send.json",
        "client_id": "synthetic-client",
        "marker": "[assistant]",
    }
    path.write_text(json.dumps(settings))
    app.configure(path)
    return path, settings


def cli(configured, *args):
    return app.main(["--config", str(configured[0]), *args])


def fake_auth(monkeypatch):
    calls = []

    def token(cache, scopes, interactive):
        calls.append((cache, scopes, interactive))
        return "synthetic-token"

    monkeypatch.setattr(app, "_msal_token", token)
    return calls


def test_preview_never_authenticates_or_writes(configured, capsys):
    assert cli(configured, "send", "--chat-id", "synthetic-chat", "-m", "Hello") == 0
    assert "PREVIEW" in capsys.readouterr().out
    assert not app.STATE.exists()


def test_doctor_is_local_only(configured, capsys):
    assert cli(configured, "doctor") == 0
    assert "no authentication or send attempted" in capsys.readouterr().out
    assert not app.STATE.exists()


@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_empty_selector_never_sends(configured, extra, capsys):
    assert cli(configured, "send", "--chat", " ", "-m", "Hello", *extra) == 1
    assert "chat-selector-empty" in capsys.readouterr().err


def test_graph_send_uses_send_cache_marker_and_escaped_body(
    configured, monkeypatch, capsys
):
    calls = fake_auth(monkeypatch)
    requests_seen = []

    def request(method, url, **kwargs):
        requests_seen.append((method, url, kwargs))
        return SimpleNamespace(
            status_code=201, json=lambda: {"id": "synthetic-message"}
        )

    monkeypatch.setattr(requests, "request", request)
    assert (
        cli(
            configured,
            "send",
            "--chat-id",
            "chat/@value",
            "-m",
            "[assistant] <hi>",
            "--yes",
        )
        == 0
    )
    assert calls == [(app.SEND_CACHE, ["ChatMessage.Send"], False)]
    assert len(requests_seen) == 1
    method, url, kwargs = requests_seen[0]
    assert method == "POST" and url.endswith("/chats/chat%2F%40value/messages")
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["body"] == {
        "contentType": "html",
        "content": "<p>[assistant] &lt;hi&gt;</p>",
    }
    audit = json.loads(app.AUDIT.read_text())
    assert audit["msg_id"] == "synthetic-message"
    assert "head" not in audit and "<hi>" not in app.AUDIT.read_text()
    assert app.AUDIT.stat().st_mode & 0o777 == 0o600
    assert "message id synthetic-message" in capsys.readouterr().out


@pytest.mark.parametrize("response", [{}, {"id": ""}, {"id": 12}])
def test_missing_message_id_is_not_success(configured, monkeypatch, response, capsys):
    fake_auth(monkeypatch)
    monkeypatch.setattr(app, "_graph", lambda *a: response)
    assert (
        cli(configured, "send", "--chat-id", "synthetic-chat", "-m", "Hello", "--yes")
        == 1
    )
    assert "unconfirmed" in capsys.readouterr().err
    assert not app.AUDIT.exists()


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
def test_graph_failures_are_single_attempt_and_redacted(
    configured, monkeypatch, failure, capsys
):
    fake_auth(monkeypatch)
    attempts = []

    def request(*args, **kwargs):
        attempts.append(args)
        if failure == "timeout":
            raise requests.Timeout("PRIVATE-SENTINEL")

        def payload():
            raise ValueError("PRIVATE-SENTINEL")

        return SimpleNamespace(
            status_code=403 if failure == "http" else 201, json=payload
        )

    monkeypatch.setattr(requests, "request", request)
    assert (
        cli(configured, "send", "--chat-id", "synthetic-chat", "-m", "Hello", "--yes")
        == 1
    )
    output = capsys.readouterr()
    assert "PRIVATE-SENTINEL" not in output.err + output.out
    assert len(attempts) == 1 and not app.AUDIT.exists()


def connector(configured, tmp_path, output, code=0):
    executable = tmp_path / "synthetic connector.py"
    captured = tmp_path / "argv.json"
    executable.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(captured)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({output!r})\nraise SystemExit({code})\n"
    )
    path, settings = configured
    settings["gsk_command"] = [sys.executable, str(executable), "send"]
    path.write_text(json.dumps(settings))
    return captured


def test_external_connector_argv_images_reply_and_mentions(
    configured, tmp_path, monkeypatch
):
    captured = connector(
        configured,
        tmp_path,
        json.dumps({"status": "ok", "data": {"message_id": "synthetic-id"}}),
    )
    picture = tmp_path / "picture.png"
    picture.write_bytes(b"synthetic-image")
    calls = fake_auth(monkeypatch)
    monkeypatch.setattr(
        app,
        "_collection",
        lambda *a: [{"userId": "synthetic-member", "displayName": "Example Person"}],
    )
    assert (
        cli(
            configured,
            "send",
            "--chat-id",
            "synthetic-chat",
            "--via",
            "gsk",
            "--mention",
            "Example",
            "--reply-to",
            "synthetic-parent",
            "--image",
            str(picture),
            "-m",
            "{mention_0}\n- check **this**",
            "--yes",
        )
        == 0
    )
    argv = json.loads(captured.read_text())
    assert argv[:3] == ["send", "--chat_id", "synthetic-chat"]
    assert "<strong>this</strong>" in argv[4] and "data:image/png;base64," in argv[4]
    assert argv[5:] == [
        "--reply_to_message_id",
        "synthetic-parent",
        "--mention_user_ids",
        "synthetic-member",
        "--mention_user_names",
        "Example Person",
    ]
    assert calls == [(app.READ_CACHE, ["Chat.Read"], False)]
    assert json.loads(app.AUDIT.read_text())["msg_id"] == "synthetic-id"


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("PRIVATE-SENTINEL", 0),
        ('{"status":"ok","data":{}}', 0),
        ('{"status":"ok","data":{"message_id":"synthetic-id"}}', 1),
        ('{"status":"error","data":{"message_id":"synthetic-id"}}', 0),
        ('{"status":"ok","data":{"message_id":3}}', 0),
    ],
)
def test_connector_failure_has_no_false_success(
    configured, tmp_path, output, code, capsys
):
    connector(configured, tmp_path, output, code)
    assert (
        cli(
            configured,
            "send",
            "--chat-id",
            "synthetic-chat",
            "--via",
            "gsk",
            "-m",
            "Hello",
            "--yes",
        )
        == 1
    )
    assert "PRIVATE-SENTINEL" not in capsys.readouterr().err
    assert not app.AUDIT.exists()


def test_connector_timeout_is_not_retried(configured, monkeypatch, capsys):
    calls = []

    def timeout(*a, **k):
        calls.append(a)
        raise subprocess.TimeoutExpired(a[0], 120, "PRIVATE-SENTINEL")

    monkeypatch.setattr(subprocess, "run", timeout)
    assert (
        cli(
            configured,
            "send",
            "--chat-id",
            "synthetic-chat",
            "--via",
            "gsk",
            "-m",
            "Hello",
            "--yes",
        )
        == 1
    )
    assert len(calls) == 1
    assert "PRIVATE-SENTINEL" not in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--reply-to", "parent"], ["--mention", "Example"]])
def test_graph_does_not_silently_drop_connector_features(configured, extra):
    with pytest.raises(SystemExit):
        cli(configured, "send", "--chat-id", "synthetic-chat", "-m", "Hello", *extra)


def test_missing_mention_placeholder_fails_before_lookup(configured):
    with pytest.raises(SystemExit, match="placeholder"):
        cli(
            configured,
            "send",
            "--chat-id",
            "synthetic-chat",
            "--via",
            "gsk",
            "--mention",
            "Example",
            "-m",
            "Hello",
        )


def test_foreign_pagination_does_not_receive_token(configured):
    with pytest.raises(ValueError, match="outside-service"):
        app._graph("GET", "https://example.invalid/v1.0/chats", "synthetic-token")


def test_target_resolution_checks_all_pages(configured, monkeypatch):
    fake_auth(monkeypatch)
    pages = iter(
        [
            {
                "value": [{"id": "first", "topic": "Example group"}],
                "@odata.nextLink": app.GRAPH + "/next",
            },
            {"value": [{"id": "second", "topic": "Example group"}]},
        ]
    )
    monkeypatch.setattr(app, "_graph", lambda *a: next(pages))
    with pytest.raises(SystemExit, match="exactly 1, got 2"):
        app._resolve_chat("Example")


def test_failed_registry_refresh_keeps_previous_file(configured, monkeypatch):
    fake_auth(monkeypatch)
    app.REGISTRY.parent.mkdir()
    app.REGISTRY.write_text("previous-registry")
    pages = iter(
        [
            {"value": [{"id": "first"}], "@odata.nextLink": app.GRAPH + "/next"},
            {"error": "synthetic-incomplete"},
        ]
    )
    monkeypatch.setattr(app, "_graph", lambda *a: next(pages))
    assert cli(configured, "chats", "--refresh") == 1
    assert app.REGISTRY.read_text() == "previous-registry"


def test_mentions_need_one_member_with_id(configured, monkeypatch):
    fake_auth(monkeypatch)
    monkeypatch.setattr(app, "_collection", lambda *a: [{"displayName": "Example"}])
    with pytest.raises(ValueError, match="user-id-missing"):
        app._resolve_mentions("synthetic-chat", ["Example"])
    monkeypatch.setattr(
        app, "_collection", lambda *a: [{"displayName": "Example", "userId": "one"}] * 2
    )
    with pytest.raises(SystemExit, match="exactly 1"):
        app._resolve_mentions("synthetic-chat", ["Example"])


def test_proposal_lifecycle_without_network(configured, capsys):
    assert cli(configured, "propose", "--chat-id", "synthetic-chat", "-m", "Hello") == 0
    pending = app._load_pending()
    identifier = next(iter(pending))
    proposal = app._proposal_path(identifier)
    assert proposal.stat().st_mode & 0o777 == 0o600
    assert cli(configured, "list") == 0
    assert identifier in capsys.readouterr().out
    item = pending[identifier]
    item["created_ts"] = time.time() - app.TTL_SECONDS - 1
    proposal.write_text(json.dumps(item))
    assert app._load_pending() == {} and proposal.exists()
    assert cli(configured, "reject", identifier) == 0 and not proposal.exists()


def test_proposal_cannot_escape_queue(configured, tmp_path):
    outside = tmp_path / "keep.json"
    outside.write_text("keep")
    assert cli(configured, "reject", "../../keep") == 1
    assert outside.read_text() == "keep"


def test_invalid_proposals_are_ignored(configured):
    app.QUEUE.mkdir(parents=True)
    (app.QUEUE / "12345678.json").write_text('{"id":"12345678","created_ts":"bad"}')
    assert app._load_pending() == {}


@pytest.mark.parametrize("accept", [True, False])
def test_approve_sends_only_after_confirmation(configured, monkeypatch, accept):
    cli(configured, "propose", "--chat-id", "synthetic-chat", "-m", "Hello")
    identifier = next(iter(app._load_pending()))
    monkeypatch.setattr(app, "_confirm_proposal", lambda item: accept)
    calls = fake_auth(monkeypatch)
    sent = []

    def send(*args):
        sent.append(args)
        return {"id": "synthetic-message"}

    monkeypatch.setattr(app, "_graph", send)
    assert cli(configured, "approve", identifier) == 0
    assert len(sent) == int(accept) and len(calls) == int(accept)
    assert app._proposal_path(identifier).exists() is not accept


def test_approval_requires_real_terminal(configured, monkeypatch):
    def denied(*a, **k):
        raise OSError("synthetic-no-tty")

    monkeypatch.setattr("builtins.open", denied)
    with pytest.raises(ValueError, match="interactive-terminal"):
        app._confirm_proposal({})


def test_record_failure_reports_already_delivered(configured, monkeypatch, capsys):
    fake_auth(monkeypatch)
    monkeypatch.setattr(app, "_graph", lambda *a: {"id": "synthetic-delivered"})

    def failure(*a):
        raise OSError("synthetic-write-failure")

    monkeypatch.setattr(app, "_audit", failure)
    with pytest.raises(SystemExit) as result:
        cli(configured, "send", "--chat-id", "synthetic-chat", "-m", "Hello", "--yes")
    assert result.value.code == 2
    assert "DELIVERED message id synthetic-delivered" in capsys.readouterr().err


def test_paths_relocate_and_private_audit_is_opt_in(configured, monkeypatch, tmp_path):
    path, settings = configured
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("EXAMPLE_STATE", str(tmp_path / "other state"))
    settings.update(
        state_directory="$EXAMPLE_STATE",
        read_token_file="~/read.json",
        audit_preview_chars=4,
    )
    path.write_text(json.dumps(settings))
    app.configure(path)
    assert (
        app.STATE == tmp_path / "other state"
        and app.READ_CACHE == tmp_path / "read.json"
    )
    app._audit({"msg_id": "synthetic"}, "hello")
    assert json.loads(app.AUDIT.read_text())["head"] == "hell"


@pytest.mark.parametrize(
    "change",
    [
        {"send_token_file": "read.json"},
        {"schema": "unknown"},
        {"extra": True},
        {"state_directory": "$UNSET_SYNTHETIC_PATH"},
        {"gsk_command": "shell command"},
        {"proposal_ttl_seconds": True},
        {"audit_preview_chars": -1},
        {"marker": "two\nlines"},
    ],
)
def test_bad_configuration_fails_before_io(configured, change, capsys):
    path, settings = configured
    settings.update(change)
    path.write_text(json.dumps(settings))
    assert cli(configured, "doctor") == 1
    assert not app.STATE.exists()
    assert "FAIL" in capsys.readouterr().err


def test_cli_help_and_doctor_from_outside_package(configured, tmp_path):
    script = Path(app.__file__).resolve().parents[1] / "scripts/send"
    env = dict(
        os.environ,
        TEAMS_SEND_PYTHON=sys.executable,
        TEAMS_SEND_CONFIG=str(configured[0]),
    )
    result = subprocess.run(
        [str(script), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0 and "propose" in result.stdout
    result = subprocess.run(
        [str(script), "doctor"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0 and "no authentication" in result.stdout
    assert not app.STATE.exists()


def test_connector_limit_applies_to_rendered_bytes(configured):
    # HTML expansion can exceed argv limits even when the source text is short.
    assert (
        cli(
            configured,
            "send",
            "--chat-id",
            "synthetic-chat",
            "--via",
            "gsk",
            "-m",
            "&" * 25000,
            "--yes",
        )
        == 1
    )
    assert not app.AUDIT.exists()


def test_read_registry_does_not_authenticate(configured, capsys):
    app.REGISTRY.parent.mkdir()
    app.REGISTRY.write_text(
        json.dumps(
            {
                "chats": [
                    {
                        "id": "synthetic-chat",
                        "type": "group",
                        "label": "Example group",
                        "members": [],
                        "mirrored": False,
                        "last_message_at": "",
                    }
                ]
            }
        )
    )
    assert cli(configured, "send", "--chat", "Example", "-m", "hello") == 0
    assert "synthetic-chat" in capsys.readouterr().out
    assert not app.AUDIT.exists()


@pytest.mark.parametrize(
    "accounts", [[], [{"name": "one"}], [{"name": "one"}, {"name": "two"}]]
)
def test_msal_account_selection_is_explicit(configured, monkeypatch, accounts):
    import msal

    events = []

    class Cache:
        has_state_changed = True

        def serialize(self):
            return "synthetic-cache"

    class Client:
        def __init__(self, client_id, **kwargs):
            assert client_id == "synthetic-client"
            assert (
                kwargs["authority"] == "https://login.microsoftonline.com/organizations"
            )

        def get_accounts(self, username):
            events.append(("accounts", username))
            return accounts

        def acquire_token_silent(self, scopes, account):
            events.append(("silent", scopes, account))
            return {"access_token": "synthetic-token"}

    monkeypatch.setattr(msal, "SerializableTokenCache", Cache)
    monkeypatch.setattr(msal, "PublicClientApplication", Client)
    app.SETTINGS["login_hint"] = "synthetic-account"
    if len(accounts) > 1:
        with pytest.raises(ValueError, match="multiple-cached-accounts"):
            ORIGINAL_AUTH(app.SEND_CACHE, app.SEND_SCOPES, False)
        assert not app.SEND_CACHE.exists()
    else:
        token = ORIGINAL_AUTH(app.SEND_CACHE, app.SEND_SCOPES, False)
        assert token == ("synthetic-token" if accounts else None)
        assert app.SEND_CACHE.stat().st_mode & 0o777 == 0o600
        assert len(events) == 1 + len(accounts)
    assert events[0] == ("accounts", "synthetic-account")


def test_login_is_the_only_explicit_interactive_token_path(configured, monkeypatch):
    calls = fake_auth(monkeypatch)
    assert cli(configured, "login") == 0
    assert calls == [(app.SEND_CACHE, ["ChatMessage.Send"], True)]
