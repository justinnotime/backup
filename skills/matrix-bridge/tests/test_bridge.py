import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import matrix_bridge as bridge

ROOT = Path(__file__).resolve().parents[1]
USER = '@sender:example.invalid'
ROOM = '!transfer:example.invalid'
PEER = '@receiver:example.invalid'
AUTH = 'Bearer EXAMPLE_TOKEN'


@pytest.fixture
def server(tmp_path):
    state = SimpleNamespace(calls=[], encrypted=False, membership='join', user=USER,
                            syncs=[], media=b'EXAMPLE_IMAGE', media_fail=False,
                            redirect=False, event_fail=False, upload_fail=False,
                            legacy=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            state.calls.append({'method': self.command, 'path': path, 'query': query,
                                'body': body, 'auth': self.headers.get('Authorization')})
            code, value = 200, {}
            if state.redirect:
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/unexpected')
                self.end_headers()
                return
            if path.endswith('/whoami'):
                value = {'user_id': state.user}
            elif path.endswith('/state/m.room.encryption'):
                code = 200 if state.encrypted else 404
                value = {'algorithm': 'm.megolm.v1.aes-sha2'} if state.encrypted else {'errcode': 'M_NOT_FOUND'}
            elif '/state/m.room.member/' in path:
                value = {'membership': state.membership}
            elif '/send/m.room.message/' in path:
                code = 500 if state.event_fail else 200
                value = {'event_id': '$event-example'} if not state.event_fail else {'error': AUTH}
            elif path == '/_matrix/media/v3/upload':
                value = {} if state.upload_fail else {'content_uri': 'mxc://example.invalid/example-media'}
            elif path.endswith('/sync'):
                assert state.syncs, 'unexpected extra sync'
                value = state.syncs.pop(0)
            elif '/media/download/' in path or '/media/v3/download/' in path:
                if state.media_fail or (state.legacy and '/client/v1/' in path):
                    code, value = 404, {'errcode': 'M_NOT_FOUND'}
                else:
                    value = state.media
            else:
                code, value = 500, {'error': 'unexpected test path'}
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = handle_request

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    auth = tmp_path / 'auth.hdr'
    auth.write_text('Authorization: ' + AUTH)
    config = tmp_path / 'config.json'
    settings = {'schema': 'matrix-bridge/v1', 'homeserver': f'http://127.0.0.1:{httpd.server_port}',
                'room_id': ROOM, 'user_id': USER, 'auth_file': 'auth.hdr',
                'state_file': 'state/since', 'inbox_dir': 'downloads'}
    config.write_text(json.dumps(settings))
    state.config = config
    state.settings = settings
    state.root = tmp_path
    state.cursor = tmp_path / 'state/since'
    state.inbox = tmp_path / 'downloads'
    state.client = lambda: bridge.Client(bridge.Config.load(config))
    state.run = lambda kind, *args: bridge.main(kind, ['--config', str(config), *args])
    yield state
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def batch(events=(), token='next', **timeline):
    return {'next_batch': token, 'rooms': {'join': {ROOM: {'timeline': {'events': list(events), **timeline}}}}}


def message(body='hello', sender=PEER, **content):
    return {'type': 'm.room.message', 'sender': sender, 'origin_server_ts': 1000,
            'content': {'msgtype': 'm.text', 'body': body, **content}}


def seed(server, token='old'):
    server.cursor.parent.mkdir(parents=True, exist_ok=True)
    server.cursor.write_text(token)


def test_text_only_uses_configured_room_and_auth(server, capsys):
    assert server.run('send', 'hello', 'phone') == 0
    call = next(c for c in server.calls if c['method'] == 'PUT')
    assert call['path'].startswith(f'/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/')
    assert json.loads(call['body']) == {'msgtype': 'm.text', 'body': 'hello phone'}
    assert all(c['auth'] == AUTH for c in server.calls)
    assert '$event-example' in capsys.readouterr().out
    assert not server.cursor.exists() and not server.inbox.exists()


def test_upload_image_and_file_keep_filenames_and_bytes(server):
    image = server.root / 'picture # 1&.png'
    other = server.root / 'report.pdf'
    image.write_bytes(b'EXAMPLE_PNG')
    other.write_bytes(b'EXAMPLE_PDF')
    assert server.run('send', str(image), str(other)) == 0
    uploads = [c for c in server.calls if c['method'] == 'POST']
    events = [json.loads(c['body']) for c in server.calls if c['method'] == 'PUT']
    assert [c['body'] for c in uploads] == [b'EXAMPLE_PNG', b'EXAMPLE_PDF']
    assert [c['query']['filename'][0] for c in uploads] == [image.name, other.name]
    assert [e['msgtype'] for e in events] == ['m.image', 'm.file']
    assert events[0]['info'] == {'mimetype': 'image/png', 'size': len(b'EXAMPLE_PNG')}


def test_force_text_and_explicit_missing_file(server):
    file = server.root / 'text'
    file.write_text('not the intended message')
    assert server.run('send', '--text', str(file)) == 0
    assert json.loads(server.calls[-1]['body'])['body'] == str(file)
    server.calls.clear()
    assert server.run('send', '--file', str(file), str(file) + '-missing') == 1
    assert not server.calls


@pytest.mark.parametrize('failure', ['event_fail', 'upload_fail'])
def test_send_failure_is_not_reported_as_success_or_retried(server, capsys, failure):
    setattr(server, failure, True)
    if failure == 'upload_fail':
        path = server.root / 'image.png'
        path.write_bytes(b'EXAMPLE_IMAGE')
        args = [str(path)]
    else:
        args = ['text']
    assert server.run('send', *args) == 1
    output = capsys.readouterr()
    assert 'sent ' not in output.out
    assert AUTH not in output.err + output.out
    assert len([c for c in server.calls if c['method'] in ('POST', 'PUT')]) == 1


@pytest.mark.parametrize('attribute,value', [('encrypted', True), ('membership', 'leave'), ('user', PEER)])
def test_wrong_account_or_room_refuses_send(server, attribute, value):
    setattr(server, attribute, value)
    assert server.run('send', 'hello') == 1
    assert not any(c['method'] in ('POST', 'PUT') for c in server.calls)


def test_doctor_does_not_consume_messages_or_create_runtime_files(server):
    assert server.run('recv', '--doctor') == 0
    assert not server.cursor.parent.exists() and not server.inbox.exists()
    assert not any(c['path'].endswith('/sync') for c in server.calls)


def test_first_receive_initializes_without_replaying_history(server, capsys):
    server.syncs = [batch([message('old history')], token='initial', limited=True)]
    assert server.run('recv') == 0
    assert server.cursor.read_text() == 'initial'
    assert 'old history' not in capsys.readouterr().out
    assert not server.inbox.exists()
    sync = server.calls[-1]
    assert 'since' not in sync['query']
    assert json.loads(sync['query']['filter'][0])['room']['rooms'] == [ROOM]


def test_receive_text_image_and_ignore_own_messages(server, capsys):
    seed(server)
    server.syncs = [batch([message('OWN_MESSAGE_MUST_BE_FILTERED', sender=USER), message('phone text'),
                          message('../../outside.png', msgtype='m.image', url='mxc://example.invalid/media')])]
    assert server.run('recv') == 0
    output = capsys.readouterr().out
    assert 'phone text' in output and 'OWN_MESSAGE_MUST_BE_FILTERED' not in output
    downloads = list(server.inbox.iterdir())
    assert len(downloads) == 1 and downloads[0].read_bytes() == server.media
    assert downloads[0].name.endswith('-outside.png')
    assert not (server.root / 'outside.png').exists()
    assert server.cursor.read_text() == 'next'
    sync = next(c for c in server.calls if c['path'].endswith('/sync'))
    assert sync['query']['since'] == ['old']


def test_download_failure_keeps_cursor_and_retry_delivers(server, capsys):
    seed(server)
    event = message('image.png', msgtype='m.image', url='mxc://example.invalid/media')
    server.syncs = [batch([message('also must retry'), event])]
    server.media_fail = True
    assert server.run('recv') == 1
    assert server.cursor.read_text() == 'old'
    assert 'also must retry' not in capsys.readouterr().out
    server.media_fail = False
    server.syncs = [batch([message('also must retry'), event])]
    assert server.run('recv') == 0
    assert server.cursor.read_text() == 'next'
    assert 'also must retry' in capsys.readouterr().out


def test_legacy_media_endpoint_fallback(server):
    seed(server)
    server.legacy = True
    server.syncs = [batch([message('file', msgtype='m.file', url='mxc://example.invalid/media')])]
    assert server.run('recv') == 0
    assert any('/_matrix/media/v3/download/' in c['path'] for c in server.calls)


@pytest.mark.parametrize('kind', ['limited', 'encrypted', 'encrypted_file', 'missing_url', 'missing_cursor', 'oversize'])
def test_unreadable_batch_does_not_advance_cursor(server, kind):
    seed(server)
    event = message('phone text')
    result = batch([event])
    if kind == 'limited':
        result = batch([event], limited=True)
    elif kind == 'encrypted':
        event['type'] = 'm.room.encrypted'
    elif kind == 'encrypted_file':
        event['content'] = {'msgtype': 'm.file', 'file': {'key': 'EXAMPLE'}}
    elif kind == 'missing_url':
        event['content'] = {'msgtype': 'm.image'}
    elif kind == 'missing_cursor':
        result.pop('next_batch')
    else:
        event['content'] = {'msgtype': 'm.file', 'url': 'mxc://example.invalid/media'}
        server.settings['max_file_bytes'] = 2
        server.config.write_text(json.dumps(server.settings))
    server.syncs = [result]
    assert server.run('recv') == 1
    assert server.cursor.read_text() == 'old'


def test_wait_uses_next_sync_token_and_stops_on_message(server, capsys):
    seed(server)
    server.syncs = [batch([], token='middle'), batch([message('arrived')], token='end')]
    assert server.run('recv', '--wait', '--wait-seconds', '5') == 0
    syncs = [c for c in server.calls if c['path'].endswith('/sync')]
    assert [c['query']['since'][0] for c in syncs] == ['old', 'middle']
    assert server.cursor.read_text() == 'end'
    assert 'arrived' in capsys.readouterr().out


def test_receiver_lock_keeps_existing_cursor(server):
    seed(server)
    with bridge.receive_lock(server.cursor):
        assert server.run('recv') == 1
    assert server.cursor.read_text() == 'old'
    assert not any(c['path'].endswith('/sync') for c in server.calls)


def test_empty_cursor_is_not_treated_as_first_use(server):
    seed(server, '')
    assert server.run('recv') == 1
    assert server.cursor.read_text() == ''
    assert not any(c['path'].endswith('/sync') for c in server.calls)


def test_redirect_does_not_forward_authorization(server, capsys):
    server.redirect = True
    assert server.run('recv', '--doctor') == 1
    assert len(server.calls) == 1
    output = capsys.readouterr()
    assert AUTH not in output.out + output.err


def test_home_paths_and_config_environment_follow_caller(server, monkeypatch):
    home = server.root / 'another user'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('MATRIX_BRIDGE_CONFIG', str(server.config))
    server.settings.update(auth_file='~/auth.hdr', state_file='$HOME/state/since', inbox_dir='~/downloads')
    server.config.write_text(json.dumps(server.settings))
    cfg = bridge.Config.load()
    assert cfg.auth_file == home / 'auth.hdr'
    assert cfg.state_file == home / 'state/since'
    assert cfg.inbox_dir == home / 'downloads'
    assert not home.exists()


@pytest.mark.parametrize('schema', ['matrix-bridge/v1', 'phone-bridge/v1'])
def test_schema_preserves_default_storage_and_pending_cursor(server, monkeypatch, schema):
    monkeypatch.setenv('HOME', str(server.root))
    server.settings['schema'] = schema
    server.settings.pop('state_file')
    server.settings.pop('inbox_dir')
    server.config.write_text(json.dumps(server.settings))
    name = schema.split('/')[0]
    cursor = server.root / '.local/state' / name / 'since'
    cursor.parent.mkdir(parents=True)
    cursor.write_text('pending-transfers')
    cfg = bridge.Config.load(server.config)
    assert cfg.state_file == cursor
    assert cfg.inbox_dir == server.root / '.cache' / name / 'inbox'
    server.syncs = [batch()]
    assert server.run('recv') == 0
    call = next(c for c in server.calls if c['path'].endswith('/sync'))
    assert call['query']['since'] == ['pending-transfers']
    assert cursor.read_text() == 'next'


def test_configuration_precedence_retains_legacy_installations(server, monkeypatch):
    for key in ('MATRIX_BRIDGE_CONFIG', 'PHONE_BRIDGE_CONFIG'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(server.root / 'config root'))
    legacy = server.root / 'config root/phone-bridge/config.json'
    current = legacy.parent.parent / 'matrix-bridge/config.json'
    settings = {**server.settings, 'auth_file': str(server.root / 'auth.hdr')}
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({**settings, 'room_id': '!legacy:example.invalid'}))
    assert bridge.Config.load().room_id == '!legacy:example.invalid'
    current.parent.mkdir()
    current.write_text(json.dumps({**settings, 'room_id': '!current:example.invalid'}))
    assert bridge.Config.load().room_id == '!current:example.invalid'
    monkeypatch.setenv('PHONE_BRIDGE_CONFIG', str(legacy))
    assert bridge.Config.load().room_id == '!legacy:example.invalid'
    monkeypatch.setenv('MATRIX_BRIDGE_CONFIG', str(current))
    assert bridge.Config.load().room_id == '!current:example.invalid'
    assert bridge.Config.load(server.config).room_id == ROOM


@pytest.mark.parametrize('broken_link', [False, True])
def test_invalid_new_default_never_selects_legacy_destination(server, monkeypatch, broken_link):
    for key in ('MATRIX_BRIDGE_CONFIG', 'PHONE_BRIDGE_CONFIG', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HOME', str(server.root))
    legacy = server.root / '.config/phone-bridge/config.json'
    current = legacy.parent.parent / 'matrix-bridge/config.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({**server.settings, 'auth_file': str(server.root / 'auth.hdr')}))
    current.parent.mkdir()
    if broken_link:
        current.symlink_to(server.root / 'missing')
    else:
        current.write_text('invalid configuration')
    assert bridge.main('send', ['--text', 'must not send']) == 1
    assert not server.calls


@pytest.mark.parametrize('field,value', [('homeserver', 'http://example.invalid'), ('homeserver', 'https://user:password@example.invalid'),
    ('room_id', 'missing-id'), ('user_id', 'missing-id'), ('max_file_bytes', True), ('timeline_limit', 0), ('schema', 'unknown')])
def test_invalid_config_fails_without_network(server, field, value):
    server.settings[field] = value
    server.config.write_text(json.dumps(server.settings))
    assert server.run('recv', '--doctor') == 1
    assert not server.calls


def test_unknown_config_and_cursor_auth_alias_fail(server):
    server.settings['token'] = 'EXAMPLE_VALUE'
    server.config.write_text(json.dumps(server.settings))
    assert server.run('recv', '--doctor') == 1
    server.settings.pop('token')
    server.settings['state_file'] = 'auth.hdr'
    server.config.write_text(json.dumps(server.settings))
    assert server.run('recv', '--doctor') == 1
    assert not server.calls


def test_missing_or_invalid_auth_is_sanitized(server, capsys):
    auth = server.root / 'auth.hdr'
    auth.write_text('PRIVATE_SENTINEL\nAuthorization: Bearer EXAMPLE_TOKEN')
    assert server.run('recv', '--doctor') == 1
    output = capsys.readouterr()
    assert 'PRIVATE_SENTINEL' not in output.out + output.err
    assert not server.calls


def test_entrypoints_run_from_another_directory_without_sibling_packages(server):
    env = {**os.environ, 'MATRIX_BRIDGE_CONFIG': str(server.config)}
    for name in ('mx-send', 'mx-recv'):
        result = subprocess.run([str(ROOT / name), '--doctor'], cwd=server.root, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'cursor unchanged' in result.stdout
    assert not server.cursor.exists()


def test_existing_symlink_is_not_replaced(server):
    protected = server.root / 'protected'
    protected.write_bytes(b'keep')
    link = server.root / 'link'
    link.symlink_to(protected)
    with pytest.raises(bridge.BridgeError):
        bridge.atomic_write(link, b'new')
    assert protected.read_bytes() == b'keep'
