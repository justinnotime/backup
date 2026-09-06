---
name: cross-ref-lint
description: Check local Markdown links and images for missing relative-path targets after moving, renaming, or deleting files, before publishing a selected document set, or when asked to find broken references. Checks filesystem targets, not heading anchors or external URLs.
---

# Cross-reference lint

Run this package's `scripts/check` against the requested files or directory.
For a rename or deletion, include the documents that could link to the old path,
not just the moved file. Prefer a repository-wide scan when callers are unknown.

```bash
uv sync --project /path/to/cross-ref-lint --locked
uv run --project /path/to/cross-ref-lint --no-sync cross-ref-check --root /path/to/repo
```

The direct `scripts/check` entry uses Python 3.10 or newer with `markdown-it-py`
installed. `CROSS_REF_LINT_PYTHON` can select that interpreter. The program is
read-only and makes no network requests.

Use `--root` to select the base for inputs and exclusions; it defaults to the
current directory. Positional paths restrict the scan. With no paths, scan the
root. A root-local `.cross-ref-lint.json` can supply directory or file exclusions:

```json
{"exclude": ["generated", "vendor"]}
```

An explicit `--config PATH` overrides that file. `--exclude PATH` adds an
exclusion for this run. Exclusions are literal paths relative to the root,
including all descendants; they are not wildcard patterns. Git metadata is
always skipped. Directory symlinks are not traversed. Other hidden directories
are included unless explicitly excluded. Report the scope and exclusions with
the result; do not hide a broken authored link by excluding its source.

The checker parses CommonMark links and images, including reference-style links,
escaped parentheses, and URL-encoded paths. It ignores link-shaped text inside
code, comments, and YAML frontmatter. Existing file or directory targets pass.
Query strings and fragments are removed before checking the path. Pure anchors,
absolute host paths, and URLs with a scheme or network host are outside scope.
Raw HTML links, wiki links, heading anchors, and undefined reference labels are
not checked. The parser's [token API](https://markdown-it-py.readthedocs.io/en/latest/using.html#the-token-stream)
distinguishes actual links from examples in code.

`--json` emits a structured report. Each finding gives the source file, the
starting line of its Markdown block, and its target. Exit codes are `0` for no
missing targets, `1` for broken links, and `2` for invalid input or an incomplete
scan. An unreadable file or missing input is not a clean result.

For each broken link, update the target to its intended location, restore an
accidentally deleted target, or remove an obsolete reference. Do not create empty
files just to pass the check. Keep intentional syntax examples inside code.
Rerun after the repair and state any limitations; a passing file check does not
establish that anchors or external sites work.

Development: `uv run --project /path/to/cross-ref-lint --locked pytest /path/to/cross-ref-lint/tests -q`.
