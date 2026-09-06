import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import google_chat_archive as archive

ROOT = Path(__file__).resolve().parents[1]
SPACE = {'name': 'spaces/EXAMPLE', 'displayName': 'Example space', 'spaceType': 'SPACE'}
TOKEN = 'EXAMPLE_ACCESS'


def message(mid='first', ts='2026-09-01T10:00:00.123456789Z', **extra):
    return {'name': f"{SPACE['name']}/messages/{mid}", 'createTime': ts,
            'text': 'example text', 'sender': {'name': 'users/SENDER', 'displayName': 'Example sender'}, **extra}


@pytest.fixture
def server(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], spaces=[SPACE.copy()], messages=[message()],
                            space_more=False, message_more=False, members_more=False,
                            fail_path=None, failure=500, redirect=False, token_failure=False,
                            people_failure=False, root=tmp_path)
    state.member_names = True

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_request(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            path = urllib.parse.unquote(parsed.path)
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state.calls.append({'method': self.command, 'path': path, 'query': query,
                                'body': body, 'auth': self.headers.get('Authorization')})
            code = 200
            if state.redirect and path != '/token':
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/unexpected')
                self.end_headers()
                return
            if path == '/token':
                code = 400 if state.token_failure else 200
                payload = {'access_token': TOKEN, 'expires_in': 3600} if code == 200 else {'error': 'PRIVATE_ERROR_SENTINEL'}
            elif state.fail_path and state.fail_path in path:
                code, payload = state.failure, {'error': 'PRIVATE_ERROR_SENTINEL'}
            elif path == '/chat/spaces':
                payload = {'spaces': state.spaces}
                if state.space_more:
                    payload['nextPageToken'] = 'more-spaces'
            elif path.endswith('/messages'):
                if query.get('pageToken'):
                    payload = {'messages': [message('second', '2026-09-01T10:00:01Z')]}
                else:
                    payload = {'messages': state.messages}
                    if state.message_more:
                        payload['nextPageToken'] = 'more-messages'
            elif path.endswith('/members'):
                member = {'name': 'users/PEER'}
                if state.member_names:
                    member['displayName'] = 'Example peer'
                payload = {'memberships': [{'member': member}]}
                if state.members_more:
                    payload['nextPageToken'] = 'more-members'
            elif '/members/' in path:
                payload = {'member': {'name': 'users/SELF'}}
            elif path.startswith('/people/'):
                code = 403 if state.people_failure else 200
                payload = ({'error': {'status': 'ACCESS_TOKEN_SCOPE_INSUFFICIENT'}} if code == 403
                           else {'names': [{'displayName': 'Resolved example'}]})
            else:
                code, payload = 500, {'error': 'unexpected test route'}
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    monkeypatch.setattr(archive, 'CHAT_API', base + '/chat')
    monkeypatch.setattr(archive, 'PEOPLE_API', base + '/people')
    monkeypatch.setattr(archive, 'TOKEN_ENDPOINT', base + '/token')
    monkeypatch.setattr(archive, 'RATE_DELAY', 0)
    monkeypatch.setattr(archive.time, 'sleep', lambda _: None)
    state.config = tmp_path / 'config.json'
    state.token = tmp_path / 'authorized-user.json'
    state.token.write_text(json.dumps({'client_id': 'EXAMPLE_CLIENT', 'client_secret': 'EXAMPLE_SECRET', 'refresh_token': 'EXAMPLE_REFRESH'}))
    state.settings = {'base_dir': str(tmp_path), 'output_dir': 'archive', 'state_file': 'state/state.json',
                      'token_file': 'authorized-user.json', 'mode': 'blacklist', 'chats': [],
                      'bootstrap_days': 365, 'max_pages': 3}
    state.output = tmp_path / 'archive'
    state.statefile = tmp_path / 'state/state.json'
    state.write_config = lambda: state.config.write_text(json.dumps({'googlechat': state.settings}))
    state.run = lambda *args: archive.main(['--config', str(state.config), *args])
    state.write_config()
    yield state
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_archive_monthly_layout_deduplication_and_oauth_read_only(server):
    token_before = server.token.read_bytes()
    assert server.run() == 0
    path = server.output / 'Example-space/2026-09.md'
    text = path.read_text()
    assert 'platform: "google-chat"' in text
    assert 'space_id: "spaces/EXAMPLE"' in text
    assert text.count('<!-- id: first -->') == 1
    assert '2026-09-01 10:00:00 — Example sender' in text
    assert server.run() == 0
    assert path.read_text() == text
    state = json.loads(server.statefile.read_text())
    assert state['spaces']['spaces/EXAMPLE']['watermark'] == message()['createTime']
    assert state['spaces']['spaces/EXAMPLE']['slug'] == 'Example-space'
    assert server.token.read_bytes() == token_before
    assert all(call['method'] == ('POST' if call['path'] == '/token' else 'GET') for call in server.calls)
    assert all(call['auth'] == f'Bearer {TOKEN}' for call in server.calls if call['path'] != '/token')
    assert all(c['query']['orderBy'] == ['createTime ASC'] for c in server.calls if c['path'].endswith('/messages'))
    form = urllib.parse.parse_qs(next(c['body'].decode() for c in server.calls if c['path'] == '/token'))
    assert form['grant_type'] == ['refresh_token'] and form['refresh_token'] == ['EXAMPLE_REFRESH']


@pytest.mark.parametrize('mode', ['--doctor', '--list-spaces', '--dry-run', '--peek'])
def test_read_modes_never_write_files(server, mode, capsys):
    before = files(server.root)
    args = (mode, 'spaces/EXAMPLE') if mode == '--peek' else (mode,)
    assert server.run(*args) == 0
    assert files(server.root) == before
    assert capsys.readouterr().out


def test_message_pages_are_complete_and_page_cap_drains_a_safe_prefix(server):
    server.message_more = True
    assert server.run() == 0
    path = server.output / 'Example-space/2026-09.md'
    assert '<!-- id: second -->' in path.read_text()
    assert any(c['query'].get('pageToken') == ['more-messages'] for c in server.calls)


def test_page_cap_preserves_oldest_prefix_and_next_run_overlap(server):
    server.message_more = True
    server.settings['max_pages'] = 1
    server.write_config()
    assert server.run() == 0
    state = json.loads(server.statefile.read_text())
    assert state['spaces']['spaces/EXAMPLE']['watermark'] == message()['createTime']
    server.settings['max_pages'] = 3
    server.write_config()
    assert server.run() == 0
    assert '<!-- id: second -->' in (server.output / 'Example-space/2026-09.md').read_text()
    requests = [c for c in server.calls if c['path'].endswith('/messages')]
    assert requests[-2]['query']['filter'] == ['createTime > "2026-09-01T09:45:00Z"']


@pytest.mark.parametrize('failure', [401, 403, 404, 429, 500])
def test_failed_reads_do_not_advance_state_or_disclose_body(server, failure, capsys):
    assert server.run() == 0
    before = server.statefile.read_bytes()
    server.fail_path = '/messages'
    server.failure = failure
    assert server.run() == 1
    assert server.statefile.read_bytes() == before
    output = capsys.readouterr()
    assert 'PRIVATE_ERROR_SENTINEL' not in output.out + output.err
    assert TOKEN not in output.out + output.err


def test_incomplete_space_list_never_publishes_partial_selection(server):
    server.space_more = True
    assert server.run() == 1
    assert not server.output.exists() and not server.statefile.exists()
    assert not any(c['path'].endswith('/messages') for c in server.calls)


@pytest.mark.parametrize('mode,chats,selected', [('whitelist', [], False), ('blacklist', [], True),
    ('blacklist', ['EXAMPLE'], False), ('whitelist', [{'match': 'spaces/EXAMPLE', 'alias': 'selected'}], True)])
def test_selection_and_aliases(server, mode, chats, selected):
    server.settings.update(mode=mode, chats=chats)
    server.write_config()
    assert server.run() == 0
    assert bool(list(server.output.rglob('*.md'))) == selected
    if mode == 'whitelist' and selected:
        assert (server.output / 'selected/2026-09.md').exists()


@pytest.mark.parametrize('changes', [{'mode': 'wrong'}, {'mode': 'blacklist', 'chats': [{}]},
    {'chats': [{'match': 'EXAMPLE', 'alias': '../escape'}]}, {'max_pages': 0}, {'bootstrap_days': True},
    {'unknown': 'EXAMPLE'}, {'state_file': 'authorized-user.json'}])
def test_invalid_configuration_fails_before_network(server, changes):
    server.settings.update(changes)
    server.write_config()
    assert server.run() == 1
    assert not server.calls


def test_staging_paths_never_relocate_credentials(server):
    stage = server.root / 'another home with spaces'
    assert server.run('--base-dir', str(stage), '--state-file', 'staged/state.json') == 0
    assert (stage / 'archive/Example-space/2026-09.md').exists()
    assert (stage / 'staged/state.json').exists()
    assert not server.output.exists() and not server.statefile.exists()
    assert archive.TOKEN_FILE == server.token


def test_home_and_environment_paths_follow_caller(server, monkeypatch):
    monkeypatch.setenv('HOME', str(server.root))
    monkeypatch.setenv('EXAMPLE_ARCHIVE', str(server.output))
    server.settings.update(base_dir='~', output_dir='$EXAMPLE_ARCHIVE', token_file='~/authorized-user.json')
    server.write_config()
    assert server.run() == 0
    assert (server.output / 'Example-space/2026-09.md').exists()


def test_missing_credential_skip_is_explicit_and_does_not_hide_invalid_tokens(server):
    server.token.unlink()
    assert server.run() == 1
    assert server.run('--skip-unconfigured') == 0
    server.token.write_text('invalid PRIVATE_ERROR_SENTINEL')
    assert server.run('--skip-unconfigured') == 1
    assert not server.statefile.exists()


def test_failed_token_refresh_and_redirect_are_sanitized(server, capsys):
    server.token_failure = True
    assert server.run('--doctor') == 1
    server.token_failure = False
    server.redirect = True
    assert server.run('--doctor') == 1
    assert not any(c['path'] == '/unexpected' for c in server.calls)
    output = capsys.readouterr()
    assert 'PRIVATE_ERROR_SENTINEL' not in output.out + output.err


def test_state_loss_recovers_existing_slug_and_does_not_duplicate(server):
    assert server.run() == 0
    before = files(server.output)
    server.statefile.unlink()
    server.spaces[0]['displayName'] = 'Renamed space'
    assert server.run() == 0
    assert files(server.output) == before
    assert json.loads(server.statefile.read_text())['spaces']['spaces/EXAMPLE']['slug'] == 'Example-space'


@pytest.mark.parametrize('content', ['invalid', '[]', '{"spaces": []}', '{"spaces": {"spaces/EXAMPLE": "invalid"}}'])
def test_state_corruption_fails_instead_of_resetting(server, content):
    server.statefile.parent.mkdir()
    server.statefile.write_text(content)
    assert server.run() == 1
    assert server.statefile.read_text() == content
    assert not server.calls


def test_name_lookup_and_optional_people_scope(server):
    server.spaces[0].pop('displayName')
    server.settings['self_email'] = 'reader@example.invalid'
    server.write_config()
    server.messages[0]['sender'].pop('displayName')
    server.people_failure = True
    assert server.run() == 0
    text = (server.output / 'Example-peer/2026-09.md').read_text()
    assert 'users/SENDER' in text
    assert any(c['path'].endswith('/members/reader@example.invalid') for c in server.calls)


def test_missing_member_names_still_archive_selected_space_under_stable_id(server):
    server.spaces[0].pop('displayName')
    server.member_names = False
    server.fail_path = '/people/'
    server.failure = 404
    assert server.run() == 0
    assert (server.output / 'EXAMPLE/2026-09.md').exists()
    assert json.loads(server.statefile.read_text())['spaces']['spaces/EXAMPLE']['slug'] == 'EXAMPLE'


def test_attachments_cards_and_bot_messages_keep_placeholders(server):
    server.messages = [message('file', text='', attachment=[{'contentName': 'example.pdf'}]),
                       message('card', text='', cardsV2=[{}], sender={'name': 'users/BOT', 'type': 'BOT'})]
    assert server.run() == 0
    text = (server.output / 'Example-space/2026-09.md').read_text()
    assert '*[file: example.pdf]*' in text and '*[card message]*' in text
    assert not any(c['path'].startswith('/people/') for c in server.calls)


def test_entrypoint_imports_without_repository_cwd(server):
    result = subprocess.run([str(ROOT / 'scripts/sync'), '--help'], cwd=server.root,
                            env={**os.environ, 'GOOGLE_CHAT_ARCHIVE_PYTHON': os.sys.executable}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--config' in result.stdout and '--doctor' in result.stdout
