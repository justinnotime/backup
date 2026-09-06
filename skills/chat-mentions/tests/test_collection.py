import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from chat_mentions import cli, collector, config, graph

AUTHENTICATE = graph.Graph.authenticate

NOW = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prevent_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected live Graph request")

    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(graph.Graph, "authenticate", forbidden)


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "config.json"
    value = {
        "schema": "chat-mentions/v1",
        "state_directory": "state",
        "collection_enabled": True,
        "client_id": "synthetic-client",
        "read_token_file": "read.json",
    }
    path.write_text(json.dumps(value))
    return path, config.load(path)


def message(identifier="synthetic-message"):
    return {
        "id": identifier,
        "messageType": "message",
        "createdDateTime": "2025-01-02T11:59:00Z",
        "from": {"user": {"id": "synthetic-sender", "displayName": "Example Sender"}},
        "body": {"content": "<p>Hello</p>"},
    }


def fake_graph(monkeypatch, *, fail=False):
    class Fake:
        def __init__(self, settings):
            pass

        def authenticate(self):
            pass

        def own_id(self):
            return "synthetic-owner"

        def active_chats(self, since):
            return [
                {
                    "id": "synthetic-chat",
                    "chatType": "oneOnOne",
                    "topic": "Example chat",
                }
            ]

        def messages(self, chat_id, since):
            if fail:
                raise ValueError("synthetic-read-failure")
            return [message()]

    monkeypatch.setattr(collector, "Graph", Fake)
    monkeypatch.setattr(collector, "utcnow", lambda: NOW)


def test_disabled_collection_does_not_access_tokens_or_create_state(settings, capsys):
    path, value = settings
    raw = json.loads(path.read_text())
    raw["collection_enabled"] = False
    path.write_text(json.dumps(raw))
    assert cli.main(["--config", str(path), "collect"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert not value["state_directory"].exists()


def test_collection_persists_compatible_queue_and_is_idempotent(settings, monkeypatch):
    _, value = settings
    fake_graph(monkeypatch)
    collector.run(value)
    state = value["state_directory"]
    first = (state / "queue.jsonl").read_bytes()
    row = json.loads(first)
    assert row["msg_id"] == "synthetic-message" and row["chat_id"] == "synthetic-chat"
    assert row["kind"] == "dm"
    assert json.loads((state / "state.json").read_text())["me_id"] == "synthetic-owner"
    collector.run(value)
    assert (state / "queue.jsonl").read_bytes() == first
    assert (state / "queue.jsonl").stat().st_mode & 0o777 == 0o600


def test_failed_read_keeps_state_and_queue(settings, monkeypatch):
    _, value = settings
    fake_graph(monkeypatch)
    collector.run(value)
    files = [value["state_directory"] / name for name in ["state.json", "queue.jsonl"]]
    before = [p.read_bytes() for p in files]
    fake_graph(monkeypatch, fail=True)
    with pytest.raises(ValueError, match="synthetic-read-failure"):
        collector.run(value)
    assert [p.read_bytes() for p in files] == before


def test_failure_after_queue_write_recovers_without_duplicate_events(
    settings, monkeypatch
):
    _, value = settings
    fake_graph(monkeypatch)
    original = collector.atomic_write

    def fail_state(path, text):
        if path.name == "state.json":
            raise OSError("synthetic disk failure")
        original(path, text)

    monkeypatch.setattr(collector, "atomic_write", fail_state)
    with pytest.raises(OSError):
        collector.run(value)
    queue = value["state_directory"] / "queue.jsonl"
    durable = queue.read_bytes()
    assert not (queue.parent / "state.json").exists()
    monkeypatch.setattr(collector, "atomic_write", original)
    collector.run(value)
    assert queue.read_bytes() == durable and (queue.parent / "state.json").exists()


def test_corrupt_state_does_not_reset_progress(settings):
    _, value = settings
    state = value["state_directory"]
    state.mkdir()
    (state / "state.json").write_text("corrupt")
    with pytest.raises(ValueError):
        collector.run(value)
    assert (state / "state.json").read_text() == "corrupt"


def test_account_change_does_not_reuse_another_users_state(settings, monkeypatch):
    _, value = settings
    fake_graph(monkeypatch)
    state = value["state_directory"]
    state.mkdir()
    (state / "state.json").write_text('{"me_id":"different-owner"}')
    with pytest.raises(ValueError, match="another-account"):
        collector.run(value)
    assert not (state / "queue.jsonl").exists()


def test_busy_collector_lock_skips_without_authentication(settings, capsys):
    import fcntl

    _, value = settings
    value["lock_file"].parent.mkdir(parents=True)
    with value["lock_file"].open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collector.run(value)
    assert "another collector" in capsys.readouterr().out
    assert not (value["state_directory"] / "state.json").exists()


def test_pagination_limit_and_failed_pages_are_errors(settings, monkeypatch):
    _, value = settings
    value["message_page_limit"] = 1
    client = graph.Graph(value)
    monkeypatch.setattr(
        client,
        "get",
        lambda *a: {"value": [message()], "@odata.nextLink": graph.BASE + "/next"},
    )
    with pytest.raises(ValueError, match="page-limit"):
        client.messages("synthetic-chat", NOW.replace(hour=11))
    monkeypatch.setattr(
        client,
        "get",
        lambda *a: {"value": [message()], "@odata.nextLink": graph.BASE + "/next"},
    )
    value["message_page_limit"] = 4
    with pytest.raises(ValueError, match="pagination-cycle"):
        client.messages("synthetic-chat", NOW.replace(hour=11))


def test_messages_stop_at_timestamp_boundary_and_encode_chat_id(settings, monkeypatch):
    _, value = settings
    client = graph.Graph(value)
    calls = []
    older = message("older")
    older["createdDateTime"] = "2025-01-02T10:00:00Z"

    def get(url, params):
        calls.append((url, params))
        return {"value": [message(), older], "@odata.nextLink": graph.BASE + "/unused"}

    monkeypatch.setattr(client, "get", get)
    assert len(client.messages("chat/opaque", NOW.replace(hour=11))) == 1
    assert calls[0][0] == "/chats/chat%2Fopaque/messages"
    assert calls[0][1]["$orderby"] == "createdDateTime desc"
    assert len(calls) == 1


def test_foreign_next_link_cannot_receive_authentication(settings):
    client = graph.Graph(settings[1])
    client.token = "synthetic-token"
    with pytest.raises(ValueError, match="outside-service"):
        client.get("https://example.invalid/v1.0/chats")


@pytest.mark.parametrize("kind", ["timeout", "http", "json"])
def test_graph_errors_do_not_print_response_contents(settings, monkeypatch, kind):
    client = graph.Graph(settings[1])
    client.token = "synthetic-token"
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        if kind == "timeout":
            raise requests.Timeout("PRIVATE-SENTINEL")

        def json_data():
            raise ValueError("PRIVATE-SENTINEL")

        return SimpleNamespace(
            status_code=403 if kind == "http" else 200, json=json_data
        )

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(ValueError) as error:
        client.get("/me/chats")
    assert "PRIVATE-SENTINEL" not in str(error.value)
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


@pytest.mark.parametrize("account_count", [0, 1, 2])
def test_authentication_selects_one_cached_account_without_login(
    settings, monkeypatch, account_count
):
    import msal

    _, value = settings
    calls = []

    class Cache:
        has_state_changed = True

        def serialize(self):
            return "synthetic-cache"

    class Client:
        def __init__(self, client_id, **kwargs):
            assert client_id == "synthetic-client"

        def get_accounts(self, username):
            return [{"name": str(i)} for i in range(account_count)]

        def acquire_token_silent(self, scopes, account):
            calls.append((scopes, account))
            return {"access_token": "synthetic-token"}

    monkeypatch.setattr(msal, "SerializableTokenCache", Cache)
    monkeypatch.setattr(msal, "PublicClientApplication", Client)
    client = graph.Graph(value)
    if account_count == 1:
        AUTHENTICATE(client)
        assert client.token == "synthetic-token"
        assert calls == [(["Chat.Read"], {"name": "0"})]
        assert value["read_token_file"].stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(ValueError, match="one-account"):
            AUTHENTICATE(client)
        assert not value["read_token_file"].exists()


def test_active_chat_pagination_uses_activity_boundary(settings, monkeypatch):
    client = graph.Graph(settings[1])
    calls = []

    def get(url, params):
        calls.append(url)
        if len(calls) == 1:
            return {
                "value": [
                    {
                        "id": "first",
                        "lastMessagePreview": {
                            "createdDateTime": "2025-01-02T11:59:00Z"
                        },
                    }
                ],
                "@odata.nextLink": graph.BASE + "/next",
            }
        return {
            "value": [
                {
                    "id": "older",
                    "lastMessagePreview": {"createdDateTime": "2025-01-01T00:00:00Z"},
                }
            ],
            "@odata.nextLink": graph.BASE + "/unused",
        }

    monkeypatch.setattr(client, "get", get)
    assert [row["id"] for row in client.active_chats(NOW.replace(hour=11))] == ["first"]
    assert len(calls) == 2
