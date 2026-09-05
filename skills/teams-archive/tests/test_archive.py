"""End-to-end archive behavior with synthetic Graph and attachment transports."""
import copy
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1] / 'src/teams_archive.py'
CHAT = {'id': '19:example@thread.v2', 'topic': 'Example project', 'chatType': 'group', 'members': []}
MESSAGE = {
    'id': 'message-1', 'messageType': 'message',
    'createdDateTime': '2026-01-02T03:04:05Z',
    'from': {'user': {'displayName': 'Example reader'}},
    'body': {'contentType': 'text', 'content': 'First captured text'},
}


@pytest.fixture
def archive(monkeypatch):
    spec = importlib.util.spec_from_file_location('archive_under_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(module, 'graph_token', lambda **_: 'SYNTHETIC_TOKEN')
    return module


def config(tmp_path, **settings):
    path = tmp_path / 'config.yaml'
    body = {'backend': 'graph', 'output_dir': 'archive', 'state_file': 'state/progress.json',
            'graph': {'client_id': 'EXAMPLE_CLIENT', 'token_cache': 'credentials/cache.json'},
            'mode': 'whitelist', 'chats': ['Example project']}
    body.update(settings)
    path.write_text(yaml.safe_dump({'teams': body}))
    return path


def graph(monkeypatch, archive, messages=None):
    messages = copy.deepcopy(messages if messages is not None else [MESSAGE])
    calls = []
    def get(url, token, params=None):
        calls.append(url)
        return {'value': [copy.deepcopy(CHAT)] if url.endswith('/me/chats') else copy.deepcopy(messages)}
    monkeypatch.setattr(archive, 'graph_get', get)
    return calls


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_first_sync_repeat_and_two_configurations(archive, monkeypatch, tmp_path):
    calls = graph(monkeypatch, archive)
    cfg = config(tmp_path)
    assert archive.main(['--config', str(cfg)]) == 0
    output = tmp_path / 'archive/Example-project/2026-01.md'
    assert 'First captured text' in output.read_text()
    before = snapshot(tmp_path)
    assert archive.main(['--config', str(cfg)]) == 0
    assert snapshot(tmp_path) == before
    assert output.read_text().count('<!-- id: message-1 -->') == 1
    second = tmp_path / 'other-consumer'
    second.mkdir()
    cfg2 = config(second)
    assert archive.main(['--config', str(cfg2)]) == 0
    assert (second / 'archive/Example-project/2026-01.md').read_bytes() == output.read_bytes()
    assert all(snapshot(tmp_path)[name] == content for name, content in before.items())
    assert len(calls) == 6


def test_explicit_chat_id_survives_renamed_topic(archive, monkeypatch, tmp_path):
    graph(monkeypatch, archive)
    cfg = config(tmp_path, chats=[{'match': CHAT['id'], 'alias': 'stable-name'}])
    assert archive.main(['--config', str(cfg)]) == 0
    assert (tmp_path / 'archive/stable-name/2026-01.md').is_file()


def test_paging_failure_preserves_state_and_retries(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path)
    graph(monkeypatch, archive)
    assert archive.main(['--config', str(cfg)]) == 0
    before = snapshot(tmp_path)
    def failure(url, token, params=None):
        if url.endswith('/me/chats'):
            return {'value': [CHAT]}
        if url.endswith('/next'):
            return None
        return {'value': [dict(MESSAGE, id='message-2')], '@odata.nextLink': archive.GRAPH_BASE + '/next'}
    monkeypatch.setattr(archive, 'graph_get', failure)
    assert archive.main(['--config', str(cfg)]) == 1
    assert snapshot(tmp_path) == before
    graph(monkeypatch, archive, [MESSAGE, dict(MESSAGE, id='message-2')])
    assert archive.main(['--config', str(cfg)]) == 0
    assert (tmp_path / 'archive/Example-project/2026-01.md').read_text().count('<!-- id:') == 2


def test_incomplete_chat_list_fails_without_output(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path)
    monkeypatch.setattr(archive, 'MAX_LIST_PAGES', 1)
    monkeypatch.setattr(archive, 'graph_get', lambda *a: {'value': [CHAT], '@odata.nextLink': archive.GRAPH_BASE + '/next'})
    assert archive.main(['--config', str(cfg)]) == 1
    assert not (tmp_path / 'archive').exists()
    assert not (tmp_path / 'state').exists()


def test_dry_run_never_writes_or_downloads(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path, attachments=True, registry_file='state/registry.json')
    message = copy.deepcopy(MESSAGE)
    message['body'] = {'contentType': 'html', 'content': '<img src="https://graph.microsoft.com/v1.0/chats/example/messages/message-1/hostedContents/EXAMPLE/$value">'}
    graph(monkeypatch, archive, [message])
    monkeypatch.setattr(archive.subprocess, 'run', lambda *a, **kw: pytest.fail('dry run started a connector'))
    before = snapshot(tmp_path)
    assert archive.main(['--config', str(cfg), '--dry-run', '--dump-raw']) == 0
    assert snapshot(tmp_path) == before
    assert archive.main(['--config', str(cfg), '--peek', 'Example project', '--dry-run']) == 0
    assert snapshot(tmp_path) == before


def test_optional_registry_peek_without_registry(archive, monkeypatch, tmp_path, capsys):
    cfg = config(tmp_path)
    graph(monkeypatch, archive)
    assert archive.main(['--config', str(cfg), '--peek', 'Example project']) == 0
    assert 'First captured text' in capsys.readouterr().out
    assert not (tmp_path / 'archive').exists()
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('settings', [
    {'mode': 'invalid'}, {'chats': [None]}, {'chats': [{'match': ''}]},
    {'chats': [{'match': 'Example project', 'alias': '../escape'}]},
    {'graph': {}}, {'output_dir': None}, {'max_pages_per_chat': 0},
])
def test_invalid_config_fails_before_network(archive, monkeypatch, tmp_path, settings):
    cfg = config(tmp_path, **settings)
    monkeypatch.setattr(archive, 'graph_get', lambda *a, **kw: pytest.fail('invalid config read remote data'))
    assert archive.main(['--config', str(cfg)]) == 1
    assert set(snapshot(tmp_path)) == {'config.yaml'}


def test_missing_configuration_has_nonzero_exit(archive, tmp_path):
    assert archive.main(['--config', str(tmp_path / 'missing.yaml')]) == 1


def test_missing_auth_fails_sync(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path)
    monkeypatch.setattr(archive, 'graph_token', lambda **_: None)
    assert archive.main(['--config', str(cfg)]) == 1
    assert not (tmp_path / 'state').exists()


def test_newest_disk_clamps_progress_from_failed_publication(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path)
    graph(monkeypatch, archive)
    assert archive.main(['--config', str(cfg)]) == 0
    state = tmp_path / 'state/progress.json'
    value = json.loads(state.read_text())
    value['chats'][CHAT['id']]['watermark'] = '2026-01-10T00:00:00Z'
    state.write_text(json.dumps(value))
    seen = []
    def read(cid, since, pages):
        seen.append(since)
        return [MESSAGE], True
    monkeypatch.setattr(archive, 'read_chat', read)
    assert archive.main(['--config', str(cfg)]) == 0
    assert seen == ['2026-01-02T02:49:05Z']
    assert json.loads(state.read_text())['chats'][CHAT['id']]['watermark'] == MESSAGE['createdDateTime']


def attachment_message():
    value = copy.deepcopy(MESSAGE)
    value['body'] = {'contentType': 'html', 'content': '<img src="https://graph.microsoft.com/v1.0/chats/example/messages/message-1/hostedContents/EXAMPLE/$value">'}
    return value


def test_attachment_download_manifest_repeat_and_backfill(archive, monkeypatch, tmp_path):
    import urllib.request
    cfg = config(tmp_path, attachments=True)
    graph(monkeypatch, archive, [attachment_message()])
    monkeypatch.setattr(archive, 'gsk_available', lambda: True)
    commands = []
    def run(cmd, **kw):
        commands.append(cmd)
        if 'download_attachment' in cmd:
            out = json.dumps({'data': {'success': True, 'file_wrapper_url': 'https://example.invalid/wrapper', 'content_type': 'image/png'}})
        elif 'download_file' in cmd:
            out = 'Download Complete'
        elif 'get_readable_url' in cmd:
            out = json.dumps({'url': 'https://example.invalid/image.png'})
        else:
            out = '{}'
        return SimpleNamespace(stdout=out, stderr='', returncode=0)
    monkeypatch.setattr(archive.subprocess, 'run', run)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'SYNTHETIC_IMAGE'))
    assert archive.main(['--config', str(cfg)]) == 0
    folder = tmp_path / 'archive/Example-project'
    assert len(list((folder / 'attachments').glob('*.png'))) == 1
    assert '![inline image](attachments/' in (folder / '2026-01.md').read_text()
    assert any(cmd[2] == 'rm' for cmd in commands)
    before = snapshot(tmp_path)
    commands.clear()
    assert archive.main(['--config', str(cfg)]) == 0
    assert snapshot(tmp_path) == before
    assert not commands
    assert archive.main(['--config', str(cfg), '--backfill-attachments', '30']) == 0
    assert snapshot(tmp_path) == before
    assert not commands


def test_attachment_failure_does_not_advance_progress(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path, attachments=True)
    graph(monkeypatch, archive, [attachment_message()])
    monkeypatch.setattr(archive, 'gsk_available', lambda: True)
    def fail(*a, **kw):
        raise archive.ArchiveError('synthetic attachment failure')
    monkeypatch.setattr(archive.AttachmentStore, '_download', fail)
    assert archive.main(['--config', str(cfg)]) == 1
    assert not (tmp_path / 'state/progress.json').exists()
    assert not (tmp_path / 'archive/Example-project/2026-01.md').exists()


def test_graph_pagination_cannot_send_token_to_other_origin(archive, monkeypatch):
    import requests
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: pytest.fail('credential sent to foreign origin'))
    with pytest.raises(archive.ArchiveError):
        archive.graph_get('https://example.invalid/messages', 'SYNTHETIC_TOKEN')


def test_dry_run_auth_does_not_save_refreshed_cache(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('archive_auth_test', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = config(tmp_path)
    module.configure(cfg, dry_run=True)
    class Cache:
        has_state_changed = True
        def serialize(self):
            pytest.fail('dry run tried to persist credentials')
    class App:
        def __init__(self, *a, **kw): pass
        def get_accounts(self): return [{}]
        def acquire_token_silent(self, *a, **kw): return {'access_token': 'SYNTHETIC_TOKEN'}
    monkeypatch.setitem(sys.modules, 'msal', SimpleNamespace(SerializableTokenCache=Cache, PublicClientApplication=App))
    assert module.graph_token() == 'SYNTHETIC_TOKEN'
    assert not (tmp_path / 'credentials').exists()


def test_help_works_without_configuration(tmp_path):
    result = subprocess.run([sys.executable, str(SOURCE), '--help'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert '--dry-run' in result.stdout
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('args', [['--list-chats'], ['--peek', CHAT['id']]])
def test_failed_read_commands_return_nonzero(archive, monkeypatch, tmp_path, args):
    cfg = config(tmp_path)
    monkeypatch.setattr(archive, 'graph_get', lambda *a, **kw: None)
    assert archive.main(['--config', str(cfg), *args]) == 1
    assert not (tmp_path / 'archive').exists()


def test_peek_partial_network_failure_is_not_a_page_budget(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path)
    def get(url, token, params=None):
        if url.endswith('/next'):
            return None
        return {'value': [MESSAGE], '@odata.nextLink': archive.GRAPH_BASE + '/next'}
    monkeypatch.setattr(archive, 'graph_get', get)
    assert archive.main(['--config', str(cfg), '--peek', CHAT['id'], '--peek-limit', '100']) == 1


def test_gsk_failed_envelope_is_not_empty_success(archive, monkeypatch):
    monkeypatch.setattr(archive.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='{"data":{"success":false}}', stderr=''))
    assert archive.run_gsk(['microsoft_teams', 'list_chats']) is None


def test_peek_does_not_require_attachment_connector(archive, monkeypatch, tmp_path):
    cfg = config(tmp_path, attachments=True)
    graph(monkeypatch, archive)
    monkeypatch.setattr(archive, 'gsk_available', lambda: False)
    assert archive.main(['--config', str(cfg), '--peek', CHAT['id']]) == 0
    assert archive.main(['--config', str(cfg)]) == 1
    assert not (tmp_path / 'archive').exists()
