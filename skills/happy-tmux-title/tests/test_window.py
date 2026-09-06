import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import happy_tmux_title

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(tmp_path):
    executable = shutil.which('tmux')
    assert executable, 'tmux is required for real integration tests'
    socket = str(tmp_path / 's')
    env = {**os.environ, 'HOME': str(tmp_path), 'SHELL': '/bin/sh'}
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)

    def run(*args):
        return subprocess.run([executable, '-S', socket, '-f', '/dev/null', *args],
                              env=env, capture_output=True, text=True, check=True).stdout.strip()

    run('new-session', '-d', '-s', 'origin', '-n', 'initial', '/bin/sleep 300')
    try:
        pane = run('new-window', '-d', '-t', 'origin:3', '-P', '-F', '#{pane_id}', '/bin/sleep 300')
        pid = run('display-message', '-p', '-t', 'origin:', '#{pid}')
        session = run('display-message', '-p', '-t', 'origin:', '#{session_id}').removeprefix('$')
        context = {**env, 'TMUX': f'{socket},{pid},{session}', 'TMUX_PANE': pane}
        yield SimpleNamespace(run=run, env=context, socket=socket, pane=pane, pid=pid)
    finally:
        subprocess.run([executable, '-S', socket, 'kill-server'], env=env, capture_output=True)


def test_inactive_own_window_is_resolved_without_changing_active_window(server):
    server.run('new-window', '-d', '-t', 'origin:9', '/bin/sleep 300')
    server.run('select-window', '-t', 'origin:9')
    assert happy_tmux_title.window_index(server.env) == 3
    assert server.run('display-message', '-p', '-t', 'origin:', '#{window_index}') == '9'


def test_linked_pane_uses_its_originating_session_number(server):
    server.run('new-session', '-d', '-s', 'other', '/bin/sleep 300')
    server.run('link-window', '-s', 'origin:3', '-t', 'other:7')
    assert happy_tmux_title.window_index(server.env) == 3
    other = server.run('display-message', '-p', '-t', 'other:', '#{session_id}').removeprefix('$')
    env = {**server.env, 'TMUX': f'{server.socket},{server.pid},{other}'}
    assert happy_tmux_title.window_index(env) == 7


def test_pane_identifiers_are_matched_exactly(server):
    assert server.pane == '%1'
    for index in range(10, 21):
        server.run('new-window', '-d', '-t', f'origin:{index}', '/bin/sleep 300')
    assert '%10' in server.run('list-panes', '-a', '-F', '#{pane_id}')
    assert happy_tmux_title.window_index(server.env) == 3


def test_window_renumbering_is_derived_again(server):
    assert happy_tmux_title.window_index(server.env) == 3
    server.run('move-window', '-s', 'origin:3', '-t', 'origin:8')
    assert happy_tmux_title.window_index(server.env) == 8


@pytest.mark.parametrize('change', [{'TMUX_PANE': ''}, {'TMUX': ''}, {'TMUX': 'invalid'}, {'TMUX_PANE': '%999999'}])
def test_missing_or_stale_context_never_uses_another_window(server, change):
    with pytest.raises(ValueError):
        happy_tmux_title.window_index({**server.env, **change})


def test_deleted_origin_uses_unanimous_remaining_number(server):
    server.run('new-session', '-d', '-s', 'other', '/bin/sleep 300')
    server.run('link-window', '-s', 'origin:3', '-t', 'other:7')
    server.run('kill-session', '-t', 'origin')
    assert happy_tmux_title.window_index(server.env) == 7


def test_deleted_origin_with_conflicting_links_is_ambiguous(server):
    server.run('new-session', '-d', '-s', 'other', '/bin/sleep 300')
    server.run('new-session', '-d', '-s', 'third', '/bin/sleep 300')
    server.run('link-window', '-s', 'origin:3', '-t', 'other:7')
    server.run('link-window', '-s', 'origin:3', '-t', 'third:9')
    server.run('kill-session', '-t', 'origin')
    with pytest.raises(ValueError):
        happy_tmux_title.window_index(server.env)


def test_cli_works_outside_package_and_propagates_lookup_failure(server, tmp_path):
    env = {**server.env, 'HAPPY_TMUX_TITLE_PYTHON': os.sys.executable}
    result = subprocess.run([str(ROOT / 'scripts/window-index')], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == '3\n'
    env['TMUX_PANE'] = '%999999'
    result = subprocess.run([str(ROOT / 'scripts/window-index')], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr.startswith('FAIL ')
