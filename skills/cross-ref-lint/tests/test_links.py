import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cross_ref_lint import exclusions, scan

ROOT = Path(__file__).resolve().parents[1]


def write(root, name, text=''):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def check(root):
    return scan(root, excluded=exclusions(root))


def test_existing_targets_fragments_queries_spaces_and_directories(tmp_path):
    write(tmp_path, 'chapter/hello world.md')
    write(tmp_path, 'chapter/a(b).md')
    write(tmp_path, 'chapter/中文.md')
    write(tmp_path, 'index.md', '[a](chapter/hello%20world.md#heading)\n'
          '[b](chapter/a(b).md?download=1) [c](chapter/) [d](chapter/中文.md)')
    report = check(tmp_path)
    assert report['files_checked'] == 4
    assert report['links_checked'] == 4
    assert not report['broken'] and not report['errors']


def test_rename_reports_inbound_links_and_repair_clears_them(tmp_path):
    target = write(tmp_path, 'chapter/old.md')
    source = write(tmp_path, 'index.md', '[chapter](chapter/old.md)')
    assert not check(tmp_path)['broken']
    target.rename(target.with_name('new.md'))
    assert check(tmp_path)['broken'] == [{'file': 'index.md', 'line': 1, 'target': 'chapter/old.md'}]
    source.write_text('[chapter](chapter/new.md)')
    assert not check(tmp_path)['broken']


def test_reference_links_images_and_table_links(tmp_path):
    write(tmp_path, 'index.md', '[one][ref]\n\n![picture](image.png)\n\n'
          '| Column |\n| --- |\n| [table](table.md) |\n\n[ref]: missing.md "A title"\n')
    report = check(tmp_path)
    assert {item['target'] for item in report['broken']} == {'missing.md', 'image.png', 'table.md'}
    assert report['links_checked'] == 3


def test_code_comments_frontmatter_and_external_references_are_not_files(tmp_path):
    write(tmp_path, 'index.md', '''---
example: '[yaml](absent.md)'
---
`[inline](absent.md)`

```markdown
[fenced](absent.md)
```

    [indented](absent.md)

<!-- [comment](absent.md) -->

[web](https://example.com/missing) [mail](mailto:reader@example.com)
[network](//example.com/missing) [root](/missing) [anchor](#missing)
[custom](custom:missing) [undefined][no-definition]
<a href="absent.md">raw HTML</a>
''')
    report = check(tmp_path)
    assert report['files_checked'] == 1 and report['links_checked'] == 0
    assert not report['broken'] and not report['errors']


def test_frontmatter_does_not_change_reported_source_line(tmp_path):
    write(tmp_path, 'index.md', '---\ntitle: Sample\n---\n\n[missing](gone.md)')
    assert check(tmp_path)['broken'][0]['line'] == 5


def test_linked_image_checks_both_image_and_link(tmp_path):
    write(tmp_path, 'index.md', '[![image](picture.png)](page.md)')
    assert {x['target'] for x in check(tmp_path)['broken']} == {'picture.png', 'page.md'}


def test_image_alt_text_does_not_create_clickable_links(tmp_path):
    write(tmp_path, 'picture.png')
    write(tmp_path, 'index.md', '![caption [not clickable](absent.md)](picture.png)')
    report = check(tmp_path)
    assert report['links_checked'] == 1 and not report['broken']


def test_relative_parent_and_literal_encoded_percent(tmp_path):
    write(tmp_path, 'file%20name.md')
    write(tmp_path, 'nested/index.md', '[parent](../file%2520name.md)')
    assert check(tmp_path)['links_checked'] == 1
    assert not check(tmp_path)['broken']


def test_private_exclusions_and_git_metadata_are_not_scanned(tmp_path):
    write(tmp_path, '.cross-ref-lint.json', json.dumps({'exclude': ['generated', '.settings']}))
    for name in ['generated/log.md', '.settings/note.md', '.git/object.md', 'nested/.git/internal.md']:
        write(tmp_path, name, '[bad](gone.md)')
    write(tmp_path, 'authored/index.md', '[bad](gone.md)')
    report = check(tmp_path)
    assert report['files_checked'] == 1
    assert report['broken'][0]['file'] == 'authored/index.md'


def test_exclusions_skip_source_content_but_not_inbound_link_validation(tmp_path):
    write(tmp_path, '.cross-ref-lint.json', '{"exclude": ["generated"]}')
    write(tmp_path, 'generated/source.md', '[irrelevant](gone.md)')
    write(tmp_path, 'index.md', '[exists](generated/source.md) [missing](generated/missing.md)')
    report = check(tmp_path)
    assert report['files_checked'] == 1 and report['links_checked'] == 2
    assert [x['target'] for x in report['broken']] == ['generated/missing.md']


@pytest.mark.parametrize('config', ['not json', '[]', '{"unknown": []}', '{"exclude": "generated"}',
                                  '{"exclude": [null]}', '{"exclude": [".."]}', '{"exclude": ["."]}'])
def test_invalid_config_never_silently_changes_scan_scope(tmp_path, config):
    write(tmp_path, '.cross-ref-lint.json', config)
    with pytest.raises(ValueError):
        exclusions(tmp_path)


def test_explicit_config_and_additional_exclusions(tmp_path):
    write(tmp_path, '.cross-ref-lint.json', 'invalid')
    config = write(tmp_path, 'alternate.json', '{"exclude": ["one"]}')
    for name in ['one/a.md', 'two/b.md', 'three/c.md']:
        write(tmp_path, name, '[bad](gone.md)')
    report = scan(tmp_path, excluded=exclusions(tmp_path, config, ['two']))
    assert [x['file'] for x in report['broken']] == ['three/c.md']
    with pytest.raises(FileNotFoundError):
        exclusions(tmp_path, tmp_path / 'missing.json')


def test_directory_symlinks_are_not_followed_and_file_targets_are_checked(tmp_path):
    source = write(tmp_path, 'index.md', '[link](broken.md)')
    (tmp_path / 'loop').symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / 'broken.md').symlink_to(tmp_path / 'missing.md')
    report = scan(tmp_path, ['index.md'])
    assert report['files_checked'] == 1 and len(report['broken']) == 1
    report = scan(tmp_path, ['index.md', str(source)])
    assert report['files_checked'] == 1
    assert scan(tmp_path, ['loop'])['errors']


def test_missing_non_markdown_and_outside_inputs_are_scan_errors(tmp_path):
    write(tmp_path, 'config.json', '{}')
    report = scan(tmp_path, ['missing.md', 'config.json', '../outside.md'])
    assert len(report['errors']) == 3 and report['files_checked'] == 0


def test_invalid_utf8_is_not_a_clean_scan(tmp_path):
    (tmp_path / 'bad.md').write_bytes(b'\xff')
    report = check(tmp_path)
    assert report['files_checked'] == 0 and len(report['errors']) == 1


def test_unreadable_directory_is_not_a_clean_scan(tmp_path, monkeypatch):
    def denied(*args, onerror, **kwargs):
        onerror(PermissionError('synthetic access denied'))
        return iter(())
    monkeypatch.setattr(os, 'walk', denied)
    assert check(tmp_path)['errors'] == ['synthetic access denied']


def test_cli_relocates_root_and_preserves_failure_exit_codes(tmp_path):
    project = tmp_path / 'different home' / 'project'
    write(project, 'index.md', '[bad](gone.md)')
    env = {**os.environ, 'CROSS_REF_LINT_PYTHON': sys.executable, 'HOME': str(tmp_path / 'different home')}
    command = [str(ROOT / 'scripts/check'), '--root', str(project), '--json']
    run = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 1 and len(json.loads(run.stdout)['broken']) == 1
    write(project, 'gone.md')
    run = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 0 and not json.loads(run.stdout)['broken']
    run = subprocess.run(command + ['missing.md'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert run.returncode == 2 and json.loads(run.stdout)['errors']


def test_explicit_missing_or_invalid_configuration_exits_two(tmp_path):
    command = [sys.executable, str(ROOT / 'src/cross_ref_lint.py'), '--root', str(tmp_path),
               '--config', str(tmp_path / 'missing.json')]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 2 and run.stderr.startswith('FAIL ')


def test_cli_accepts_a_symlink_or_parent_navigation_in_root(tmp_path):
    project = tmp_path / 'project'
    write(project, 'index.md')
    (project / 'child').mkdir()
    (tmp_path / 'alias').symlink_to(project, target_is_directory=True)
    for root in [tmp_path / 'alias', project / 'child/..']:
        run = subprocess.run([sys.executable, str(ROOT / 'src/cross_ref_lint.py'), '--root', str(root), '--json'],
                             capture_output=True, text=True)
        assert run.returncode == 0 and json.loads(run.stdout)['files_checked'] == 1
