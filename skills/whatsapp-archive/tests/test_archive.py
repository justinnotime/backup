"""Synthetic spool, privacy selection, and external publication contracts."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
import whatsapp_archive as archive


@pytest.fixture
def fixture(tmp_path):
    spool = tmp_path / 'device'
    (spool / 'spool').mkdir(parents=True)
    (spool / 'chats.json').write_text(json.dumps({
        'example@g.us': {'name': 'Example project', 'type': 'group'},
        'private@g.us': {'name': 'Excluded chat', 'type': 'group'},
    }))
    cfg = tmp_path / 'config.yaml'
    settings = {'base_dir': '.', 'output_dir': 'archive', 'state_file': 'state/progress.json',
                'spool_dir': str(spool), 'mode': 'whitelist', 'chats': ['Example']}
    def configure(**changes):
        settings.update(changes)
        cfg.write_text(yaml.safe_dump({'whatsapp': settings}))
        return cfg
    configure()
    return cfg, spool, configure


def message(mid='m1', **changes):
    return {'v': 1, 'ts': 1767225600, 'chat_jid': 'example@g.us', 'msg_id': mid,
            'text': 'Example body', 'sender_name': 'Example sender', **changes}


def write_rows(spool, rows, filename='2026-01-01.ndjson'):
    p = spool / 'spool' / filename
    p.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return p


def run(cfg, *options):
    return archive.main(['--config', str(cfg), *options])


def files(root):
    return {str(f.relative_to(root)): f.read_bytes() for f in root.rglob('*') if f.is_file()}


def test_month_render_selection_history_dedup_and_idempotence(fixture, tmp_path):
    cfg, spool, _ = fixture
    write_rows(spool, [message('m2', ts=1767225602, text='second'), message(),
                       message(source='history'), message('hidden', chat_jid='private@g.us', text='PRIVATE_SENTINEL')])
    assert run(cfg) == 0
    output = tmp_path / 'archive/Example-project/2026-01.md'
    text = output.read_text()
    assert text.count('<!-- id: m1 -->') == 1
    assert text.index('id: m1') < text.index('id: m2')
    assert 'PRIVATE_SENTINEL' not in text and len(list((tmp_path / 'archive').rglob('*.md'))) == 1
    before = files(tmp_path)
    assert run(cfg) == 0
    assert files(tmp_path) == before


def test_selection_change_backfills_unchanged_days_and_retains_old_archive(fixture, tmp_path):
    cfg, spool, configure = fixture
    write_rows(spool, [message(), message('private', chat_jid='private@g.us')])
    assert run(cfg) == 0
    configure(chats=['Excluded'])
    assert run(cfg) == 0
    assert len(list((tmp_path / 'archive').rglob('*.md'))) == 2


def test_same_size_change_rebuilds_and_legacy_size_state_is_accepted(fixture, tmp_path):
    cfg, spool, _ = fixture
    day = write_rows(spool, [message(text='first')])
    assert run(cfg) == 0
    state_path = tmp_path / 'state/progress.json'
    state = json.loads(state_path.read_text())
    state['files'][day.name] = day.stat().st_size
    state_path.write_text(json.dumps(state))
    write_rows(spool, [message(text='other')])
    assert run(cfg) == 0
    assert 'other' in (tmp_path / 'archive/Example-project/2026-01.md').read_text()
    assert json.loads(state_path.read_text())['files'][day.name] == hashlib.sha256(day.read_bytes()).hexdigest()


@pytest.mark.parametrize('damage', ['truncated', 'json', 'shape', 'missing_day', 'bad_state', 'bad_index'])
def test_corrupt_input_never_changes_archive_or_progress(fixture, tmp_path, damage, capsys):
    cfg, spool, _ = fixture
    day = write_rows(spool, [message()])
    assert run(cfg) == 0
    if damage == 'truncated':
        day.write_text(day.read_text() + '{"PRIVATE_SENTINEL":')
    elif damage == 'json':
        day.write_text(day.read_text() + 'PRIVATE_SENTINEL\n')
    elif damage == 'shape':
        day.write_text(day.read_text() + '[]\n')
    elif damage == 'missing_day':
        day.unlink()
        write_rows(spool, [message('m2')], '2026-01-02.ndjson')
    elif damage == 'bad_state':
        (tmp_path / 'state/progress.json').write_text('PRIVATE_SENTINEL')
    else:
        (spool / 'chats.json').write_text('PRIVATE_SENTINEL')
    before = files(tmp_path / 'archive'), files(tmp_path / 'state')
    assert run(cfg) == 1
    assert (files(tmp_path / 'archive'), files(tmp_path / 'state')) == before
    captured = capsys.readouterr()
    assert 'PRIVATE_SENTINEL' not in captured.out + captured.err


@pytest.mark.parametrize('setting', [
    {'mode': 'invalid'}, {'mode': 'blacklist', 'chats': [{}]}, {'chats': 'Example'},
    {'chats': [{'match': 'Example', 'alias': '../escape'}]},
    {'chats': [{'match': 'Example', 'alias': '..'}]}, {'enabled': 'false'},
])
def test_invalid_selection_fails_without_output(fixture, tmp_path, setting):
    cfg, spool, configure = fixture
    configure(**setting)
    write_rows(spool, [message()])
    assert run(cfg) == 1
    assert not (tmp_path / 'archive').exists() and not (tmp_path / 'state').exists()


def test_empty_whitelist_and_blacklist(fixture, tmp_path):
    cfg, spool, configure = fixture
    write_rows(spool, [message(), message('hidden', chat_jid='private@g.us')])
    configure(chats=[])
    assert run(cfg) == 0
    assert not (tmp_path / 'archive').exists()
    configure(mode='blacklist', chats=['Excluded'])
    assert run(cfg) == 0
    assert len(list((tmp_path / 'archive').rglob('*.md'))) == 1


def test_dry_run_never_creates_files_or_refreshes_bridge(fixture, tmp_path, monkeypatch):
    cfg, spool, configure = fixture
    write_rows(spool, [message()])
    configure(refresh_before_sync=True)
    monkeypatch.setattr(archive, 'refresh_bridge', lambda cfg: pytest.fail('dry run refreshed bridge'))
    before = files(tmp_path)
    assert run(cfg, '--dry-run') == 0
    assert files(tmp_path) == before


def test_missing_spool_requires_explicit_non_owner_skip(fixture, tmp_path):
    cfg, _, _ = fixture
    assert run(cfg) == 1
    assert run(cfg, '--allow-missing-spool') == 0
    assert not (tmp_path / 'state').exists()


def test_media_unicode_multiline_metadata_and_slug_collision(fixture, tmp_path):
    cfg, spool, configure = fixture
    configure(chats=[{'match': '@g.us', 'alias': '共同'}])
    (spool / 'chats.json').write_text('{}')
    write_rows(spool, [message(chat_name='Example\nname', media={'kind': 'image', 'caption': '图'}),
                       message('b', chat_jid='second@g.us'), message('c', chat_jid='third@g.us')])
    assert run(cfg) == 0
    months = list((tmp_path / 'archive').rglob('*.md'))
    assert len(months) == 3
    for path in months:
        front = yaml.safe_load(path.read_text().split('---')[1])
        assert front['platform'] == 'whatsapp'
    assert '*[image]*' in (tmp_path / 'archive/共同/2026-01.md').read_text()


def test_output_symlink_and_unsafe_stored_slug_refused(fixture, tmp_path):
    cfg, spool, _ = fixture
    write_rows(spool, [message()])
    (tmp_path / 'archive').mkdir()
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'archive/Example-project').symlink_to(tmp_path / 'outside', target_is_directory=True)
    assert run(cfg) == 1
    assert not list((tmp_path / 'outside').iterdir())
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state/progress.json').write_text(json.dumps({'chats': {'example@g.us': {'slug': '../outside'}}}))
    assert run(cfg) == 1


def test_peek_and_list_are_read_only(fixture, tmp_path):
    cfg, spool, _ = fixture
    write_rows(spool, [message()])
    before = files(tmp_path)
    assert run(cfg, '--list-chats') == 0
    assert run(cfg, '--peek', 'Example') == 0
    assert files(tmp_path) == before


def publisher():
    return {'command': ['publisher', '--repo', '{base_dir}', '--paths', '{output_dir}', '--state', '{state_dir}', '--'],
            'base_env': 'EXAMPLE_WORKTREE', 'state_env': 'EXAMPLE_STATE'}


def test_publication_runs_literal_argv_and_propagates_failure(fixture, tmp_path, monkeypatch):
    cfg, _, configure = fixture
    settings = publisher()
    settings['command'].insert(1, 'literal;$(false)')
    configure(publish=settings)
    seen = []
    def call(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 29)
    monkeypatch.setattr(archive.subprocess, 'run', call)
    assert run(cfg, '--publish', '--full') == 29
    argv, kwargs = seen.pop()
    assert argv[1] == 'literal;$(false)' and not kwargs.get('shell')
    assert '--transaction-writer' in argv and '--full' in argv
    assert not (tmp_path / 'state').exists()


def test_transaction_writer_isolates_archive_and_state_but_keeps_input(fixture, tmp_path, monkeypatch):
    cfg, spool, configure = fixture
    configure(publish=publisher())
    write_rows(spool, [message()])
    monkeypatch.setenv('EXAMPLE_WORKTREE', str(tmp_path / 'checkout'))
    monkeypatch.setenv('EXAMPLE_STATE', str(tmp_path / 'staged'))
    assert run(cfg, '--transaction-writer') == 0
    assert (tmp_path / 'checkout/archive/Example-project/2026-01.md').exists()
    assert (tmp_path / 'staged/progress.json').exists()
    assert not (tmp_path / 'archive').exists() and not (tmp_path / 'state').exists()


@pytest.mark.parametrize('work,state', [('', ''), ('relative', '/staged'), ('ORIGINAL', '/staged'), ('/checkout', 'ORIGINAL')])
def test_writer_refuses_missing_or_nonisolated_environment(fixture, tmp_path, monkeypatch, work, state):
    cfg, spool, configure = fixture
    configure(publish=publisher())
    write_rows(spool, [message()])
    monkeypatch.setenv('EXAMPLE_WORKTREE', str(tmp_path) if work == 'ORIGINAL' else work)
    monkeypatch.setenv('EXAMPLE_STATE', str(tmp_path / 'state') if state == 'ORIGINAL' else state)
    assert run(cfg, '--transaction-writer') == 1
    assert not (tmp_path / 'archive').exists() and not (tmp_path / 'state').exists()


def test_refresh_failure_prevents_rendering(fixture, tmp_path):
    cfg, spool, configure = fixture
    write_rows(spool, [message()])
    configure(refresh_before_sync=True, bridge={'node': '/bin/false'})
    assert run(cfg) == 1
    assert not (tmp_path / 'archive').exists() and not (tmp_path / 'state').exists()


def test_bridge_uses_explicit_store_dependencies_and_environment(fixture, tmp_path):
    cfg, spool, configure = fixture
    configure(bridge={'node': '/configured/node', 'dependencies_dir': 'runtime'}, command_environment={'EXAMPLE': 'yes'})
    settings = archive.configure(cfg)
    command, env = archive.bridge_command(settings, 'drain', 17)
    assert command[0] == '/configured/node' and command[-3:] == ['drain', '--seconds', '17']
    assert env['WA_BRIDGE_DIR'] == str(spool)
    assert env['WHATSAPP_BRIDGE_DEPENDENCIES'] == str(tmp_path / 'runtime') and env['EXAMPLE'] == 'yes'


def test_wrapper_works_with_explicit_interpreter(fixture, tmp_path):
    cfg, spool, _ = fixture
    write_rows(spool, [message()])
    entry = Path(__file__).resolve().parents[1] / 'scripts/sync'
    result = subprocess.run([str(entry), '--config', str(cfg), '--dry-run'],
                            env={**os.environ, 'WHATSAPP_ARCHIVE_PYTHON': sys.executable}, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'archive').exists()


@pytest.mark.parametrize('option', ['--dry-run', '--allow-missing-spool', '--peek', '--bridge'])
def test_publish_rejects_incompatible_modes(fixture, option):
    cfg, _, configure = fixture
    configure(publish=publisher())
    options = [option] + (['Example'] if option == '--peek' else ['status'] if option == '--bridge' else [])
    with pytest.raises(SystemExit):
        run(cfg, '--publish', *options)
