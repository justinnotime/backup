"""Configured publication keeps archive/state staged and credentials stable."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_archive import config, graph, archive as archive


def publish_config(tmp_path, **settings):
    return config(tmp_path, publish={
        'command': ['publisher', '--paths', '{output_dir}', '--state', '{state_dir}', '--'],
        'base_env': 'EXAMPLE_WORKTREE', 'state_env': 'EXAMPLE_STATE',
    }, **settings)


def test_publisher_argv_failure_and_literal_arguments(archive, monkeypatch, tmp_path):
    cfg = publish_config(tmp_path)
    doc = yaml.safe_load(cfg.read_text())
    literal = 'argument with spaces; $(no-shell)'
    doc['teams']['publish']['command'].insert(1, literal)
    cfg.write_text(yaml.safe_dump(doc))
    seen = []
    monkeypatch.setattr(archive.subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)) or SimpleNamespace(returncode=23))
    assert archive.main(['--config', str(cfg), '--publish']) == 23
    argv, kw = seen.pop()
    assert argv[1] == literal
    assert argv[argv.index('--paths') + 1] == 'archive'
    assert argv[argv.index('--state') + 1] == str(tmp_path / 'state')
    assert argv[argv.index('--token-cache') + 1] == str(tmp_path / 'credentials/cache.json')
    assert '--transaction-writer' in argv and not kw.get('shell')
    assert not (tmp_path / 'state').exists()


def test_writer_keeps_credentials_and_registry_outside_staging(archive, monkeypatch, tmp_path):
    cfg = publish_config(tmp_path, registry_file='metadata/chats.json')
    graph(monkeypatch, archive)
    monkeypatch.setenv('EXAMPLE_WORKTREE', str(tmp_path / 'worktree'))
    monkeypatch.setenv('EXAMPLE_STATE', str(tmp_path / 'staged'))
    assert archive.main(['--config', str(cfg), '--transaction-writer']) == 0
    assert archive.GRAPH_CFG['token_cache'] == str(tmp_path / 'credentials/cache.json')
    assert archive.REGISTRY_FILE == tmp_path / 'metadata/chats.json'
    assert (tmp_path / 'worktree/archive/Example-project/2026-01.md').is_file()
    assert (tmp_path / 'staged/progress.json').is_file()
    assert not (tmp_path / 'archive').exists() and not (tmp_path / 'state').exists()


@pytest.mark.parametrize('environment', [{}, {'EXAMPLE_WORKTREE': 'relative', 'EXAMPLE_STATE': '/state'},
                                      {'EXAMPLE_WORKTREE': 'ORIGINAL', 'EXAMPLE_STATE': '/state'},
                                      {'EXAMPLE_WORKTREE': '/worktree', 'EXAMPLE_STATE': 'ORIGINAL'}])
def test_writer_requires_separate_absolute_directories(archive, monkeypatch, tmp_path, environment):
    cfg = publish_config(tmp_path)
    monkeypatch.delenv('EXAMPLE_WORKTREE', raising=False)
    monkeypatch.delenv('EXAMPLE_STATE', raising=False)
    for key, value in environment.items():
        if value == 'ORIGINAL':
            value = str(tmp_path if key == 'EXAMPLE_WORKTREE' else tmp_path / 'state')
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(archive, 'list_chats', lambda: pytest.fail('invalid writer contacted remote'))
    assert archive.main(['--config', str(cfg), '--transaction-writer']) == 1


def test_base_dir_config_and_relative_cli_compatibility(archive, monkeypatch, tmp_path):
    nested = tmp_path / 'configuration'
    nested.mkdir()
    cfg = config(nested, base_dir='..')
    archive.configure(cfg)
    assert archive.OUTPUT_DIR == tmp_path / 'archive'
    monkeypatch.chdir(tmp_path)
    archive.configure(cfg, base_dir='separate')
    assert archive.OUTPUT_DIR == tmp_path / 'separate/archive'


@pytest.mark.parametrize('option', ['--dry-run', '--dump-raw', '--peek', '--login'])
def test_publication_refuses_other_modes(archive, tmp_path, option):
    args = ['--config', str(publish_config(tmp_path)), '--publish', option]
    if option == '--peek':
        args.append('Example')
    with pytest.raises(SystemExit):
        archive.main(args)


def test_connector_receives_configured_environment(archive, tmp_path):
    tool = tmp_path / 'connector'
    tool.write_text('#!' + sys.executable + '\nimport json, os\nprint(json.dumps({"value":os.environ["EXAMPLE_CONTEXT"]}))\n')
    tool.chmod(0o755)
    archive.configure(config(tmp_path, gsk_command='connector', command_environment={
        'PATH': str(tmp_path), 'EXAMPLE_CONTEXT': 'configured only',
    }))
    assert archive.gsk_available()
    assert archive.run_gsk(['inspect']) == {'value': 'configured only'}
    assert 'EXAMPLE_CONTEXT' not in os.environ


def test_public_entry_calls_external_publisher_without_shell(tmp_path):
    cfg = publish_config(tmp_path)
    capture = tmp_path / 'arguments.json'
    publisher = tmp_path / 'publisher.py'
    publisher.write_text('import json, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nsys.exit(27)\n')
    doc = yaml.safe_load(cfg.read_text())
    doc['teams']['publish']['command'] = [sys.executable, str(publisher), str(capture), 'literal;$(false)', '--']
    cfg.write_text(yaml.safe_dump(doc))
    entry = Path(__file__).resolve().parents[1] / 'scripts/sync'
    result = subprocess.run([str(entry), '--config', str(cfg), '--publish'],
                            env={**os.environ, 'TEAMS_ARCHIVE_PYTHON': sys.executable}, capture_output=True)
    assert result.returncode == 27
    args = json.loads(capture.read_text())
    assert args[0] == 'literal;$(false)' and '--transaction-writer' in args
    assert not (tmp_path / 'state').exists()


def test_staged_attachment_uses_environment_for_download_and_cleanup(archive, monkeypatch, tmp_path):
    import io
    import urllib.request
    from test_archive import attachment_message
    tool = tmp_path / 'connector'
    tool.write_text('#!' + sys.executable + '''
import json, os, pathlib, sys
with pathlib.Path(os.environ['EXAMPLE_COMMAND_LOG']).open('a') as stream:
    stream.write(sys.argv[2] + '\\n')
if 'download_attachment' in sys.argv:
    print(json.dumps({'data': {'success': True, 'file_wrapper_url': 'https://example.invalid/wrapper', 'content_type': 'image/png'}}))
elif 'download_file' in sys.argv:
    print('Download Complete')
elif 'get_readable_url' in sys.argv:
    print(json.dumps({'url': 'https://example.invalid/image.png'}))
else:
    print('{}')
''')
    tool.chmod(0o755)
    cfg = publish_config(tmp_path, attachments=True, gsk_command='connector',
                         command_environment={'PATH': str(tmp_path), 'EXAMPLE_COMMAND_LOG': str(tmp_path / 'commands')})
    graph(monkeypatch, archive, [attachment_message()])
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'SYNTHETIC_IMAGE'))
    monkeypatch.setenv('EXAMPLE_WORKTREE', str(tmp_path / 'worktree'))
    monkeypatch.setenv('EXAMPLE_STATE', str(tmp_path / 'staged'))
    assert archive.main(['--config', str(cfg), '--transaction-writer']) == 0
    assert (tmp_path / 'commands').read_text().splitlines() == [
        'download_attachment', 'mkdir', 'download_file', 'get_readable_url', 'rm']
    images = list((tmp_path / 'worktree/archive').rglob('*.png'))
    assert len(images) == 1 and images[0].read_bytes() == b'SYNTHETIC_IMAGE'
    assert (tmp_path / 'staged/progress.json').is_file()
    assert not (tmp_path / 'state').exists()


def test_home_paths_and_environment_follow_the_caller(archive, monkeypatch, tmp_path):
    home = tmp_path / 'another user'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('EXAMPLE_TOOLS', str(home / 'tools'))
    cfg = publish_config(tmp_path, state_file='~/state/teams.json', command_environment={
        'PATH': '$HOME/bin:${EXAMPLE_TOOLS}:/usr/bin',
    })
    document = yaml.safe_load(cfg.read_text())
    document['teams']['publish']['command'] = ['publisher', '--lock', '{home}/locks/chat.lock', '--']
    cfg.write_text(yaml.safe_dump(document))
    seen = []
    monkeypatch.setattr(archive.subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)) or SimpleNamespace(returncode=0))
    assert archive.main(['--config', str(cfg), '--publish']) == 0
    argv, options = seen.pop()
    assert argv[2] == str(home / 'locks/chat.lock')
    assert archive.STATE_FILE == home / 'state/teams.json'
    assert options['env']['PATH'] == f'{home}/bin:{home}/tools:/usr/bin'
    assert not home.exists()
