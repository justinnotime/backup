"""Resolve this process's tmux window without using the active tab."""
import os
import re
import subprocess
import sys


def window_index(environment=None):
    env = os.environ if environment is None else environment
    parts = env.get('TMUX', '').rsplit(',', 2)
    pane = env.get('TMUX_PANE', '')
    if (len(parts) != 3 or not parts[0] or not parts[1].isdigit()
            or not parts[2].isdigit() or not re.fullmatch(r'%[0-9]+', pane)):
        raise ValueError('this process has no complete tmux session and pane context')
    socket, _, session = parts
    try:
        result = subprocess.run(
            ['tmux', '-S', socket, 'list-panes', '-a', '-F',
             '#{session_id}\t#{window_index}\t#{pane_id}'],
            env=dict(env), text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('the originating tmux server is unavailable') from None
    if result.returncode:
        raise ValueError('the originating tmux server is unavailable')
    matches = set()
    origin_matches = set()
    for line in result.stdout.splitlines():
        columns = line.split('\t')
        if len(columns) == 3 and columns[2] == pane:
            if not columns[1].isdigit():
                raise ValueError('tmux returned an invalid window number')
            matches.add(int(columns[1]))
            if columns[0] == '$' + session:
                origin_matches.add(int(columns[1]))
    candidates = origin_matches or matches
    if len(candidates) != 1:
        raise ValueError('the pane has no unique window number in the available tmux context')
    return candidates.pop()


def main():
    try:
        print(window_index())
        return 0
    except ValueError as error:
        print('FAIL ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
