"""Read-only filesystem validation of parsed Markdown link targets."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


def document_links(text):
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == '---':
        for index in range(1, len(lines)):
            if lines[index].strip() in ('---', '...'):
                text = '\n' * (index + 1) + ''.join(lines[index + 1:])
                break
    parser = MarkdownIt('commonmark').enable('table')

    def children(tokens, line):
        for token in tokens:
            position = token.map[0] + 1 if token.map else line
            if token.type in ('link_open', 'image'):
                target = token.attrGet('href' if token.type == 'link_open' else 'src')
                if target is not None:
                    yield position, target
            if token.children and token.type != 'image':
                yield from children(token.children, position)

    yield from children(parser.parse(text), 1)


def local_path(target):
    if not target or target.startswith(('#', '/', '\\')):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    return unquote(parts.path)


def exclusions(root, config=None, extra=()):
    path = Path(config).expanduser() if config else root / '.cross-ref-lint.json'
    names = []
    if config or path.exists() or path.is_symlink():
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) - {'exclude'}
                or not isinstance(value.get('exclude', []), list)
                or any(not isinstance(item, str) or not item for item in value.get('exclude', []))):
            raise ValueError('config must contain only an exclude array of nonempty paths')
        names.extend(value.get('exclude', []))
    names.extend(extra)
    result = []
    for name in names:
        path = Path(os.path.abspath(root / name))
        if Path(name).is_absolute() or path == root or root not in path.parents:
            raise ValueError('exclusions must be paths inside the scan root')
        result.append(path)
    return result


def scan(root, paths=(), excluded=()):
    root = root.resolve()
    report = {'root': str(root), 'excluded': [str(p.relative_to(root)) for p in excluded],
              'files_checked': 0, 'links_checked': 0, 'broken': [], 'errors': []}

    def skip(path):
        return '.git' in path.relative_to(root).parts or any(path == p or p in path.parents for p in excluded)

    def record_error(error):
        report['errors'].append(str(error))

    files = set()
    for name in paths or ('.',):
        path = Path(os.path.abspath(root / name))
        if path != root and root not in path.parents:
            report['errors'].append(f'input is outside the scan root: {name}')
            continue
        if not path.exists():
            report['errors'].append(f'input does not exist: {name}')
        elif skip(path):
            continue
        elif path.is_dir():
            if path.is_symlink():
                report['errors'].append(f'input directory is a symlink: {name}')
                continue
            for parent, dirs, names in os.walk(path, followlinks=False, onerror=record_error):
                dirs[:] = sorted(d for d in dirs if not skip(Path(parent) / d))
                files.update(Path(parent) / f for f in names
                             if f.lower().endswith('.md') and not skip(Path(parent) / f))
        elif path.suffix.lower() == '.md' and path.is_file():
            files.add(path)
        else:
            report['errors'].append(f'input is not a Markdown file or directory: {name}')

    for path in sorted(files):
        relative = str(path.relative_to(root))
        try:
            text = path.read_text(encoding='utf-8')
            links = list(document_links(text))
        except (OSError, UnicodeError, ValueError) as error:
            report['errors'].append(f'{relative}: {error}')
            continue
        report['files_checked'] += 1
        for line, target in links:
            try:
                destination = local_path(target)
                if destination is None:
                    continue
                report['links_checked'] += 1
                if not (path.parent / destination).exists():
                    report['broken'].append({'file': relative, 'line': line, 'target': target})
            except (OSError, ValueError) as error:
                report['errors'].append(f'{relative}:{line}: {error}')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', help='files or directories relative to root')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--config', help='JSON exclusions; defaults to root/.cross-ref-lint.json')
    parser.add_argument('--exclude', action='append', default=[])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError('scan root must be an existing directory')
        excluded = exclusions(root, args.config, args.exclude)
        report = scan(root, args.paths, excluded)
    except (OSError, UnicodeError, ValueError) as error:
        print('FAIL ' + str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for finding in report['broken']:
            print(f"BROKEN {finding['file']}:{finding['line']} -> {finding['target']}")
        for error in report['errors']:
            print('FAIL ' + error, file=sys.stderr)
        print(f"Checked {report['files_checked']} Markdown files and {report['links_checked']} local links; "
              f"{len(report['broken'])} missing targets; {len(report['errors'])} scan errors")
    return 2 if report['errors'] else 1 if report['broken'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
