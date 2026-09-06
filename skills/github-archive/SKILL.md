---
name: github-archive
description: Mirror configured GitHub issues, pull requests, and comments into a local Markdown archive, or build dependency graphs, activity timelines and inventories from an explicitly selected local mirror. Uses caller-owned repository selection, configuration and state.
---

# GitHub archive

Use this package's `scripts/sync`. It performs read-only GitHub requests through
an externally authenticated `gh` command and writes only the configured local
archive and state. It does not commit, push, send messages, or call a language
model.

Use `scripts/graph --input-dir /path/to/selected/archive` to build local DOT
graphs, a text timeline, inventory JSON or statistics. This entry makes no
network requests or language-model calls and does not need GitHub credentials.
Read [local graphs and timelines](references/graphs.md) for filters, output
modes and how to interpret the archived relationships.

## Setup and run

Install dependencies in this Skill directory:

```bash
uv sync --project /path/to/github-archive --locked
/path/to/github-archive/scripts/sync --config /path/to/private/config.yaml --dry-run
/path/to/github-archive/scripts/sync --config /path/to/private/config.yaml
```

Without `uv`, use Python 3.10+ with PyYAML installed. The wrapper uses its own
`.venv/bin/python` when present, otherwise `python3`. `GITHUB_ARCHIVE_PYTHON`
can select an existing interpreter.

Keep configuration, output, and state outside the Skill directory. Start with
[the synthetic configuration](references/config.example.yaml) and read
[configuration and state behavior](references/config.md). Ask for missing
repository selection or destination information; do not infer accounts,
repositories, private paths, or credentials from another checkout.

`--dry-run` may fetch issue bodies and comments to discover references, but
writes no archive or state. `--repo owner/name` selects one configured
repository. `--full` refreshes all known targets. Relative output and state
paths resolve from the configuration directory, or explicit `--base-dir`.
`--output-dir` and `--state-file` override configuration paths.

## Failure and publication boundaries

A nonzero exit means the caller must not publish staged results or promote
staged state. Run serially for each output/state pair. A repository failure
preserves its incremental cursor; already written files and state for other
successful repositories may remain. Transaction ownership belongs to the caller.

Archived text and runtime logs can contain private repository names, people,
links, and content. Keep them private unless the user explicitly authorizes
publication of that exact content. Public examples and tests must use synthetic
data; never copy real configuration, API responses, or authentication files
into this package.

## Verify

```bash
uv run --project /path/to/github-archive --locked pytest /path/to/github-archive/tests
```

Code, dependencies, and tests belong to this Skill. It needs no sibling Skill
source or repository-wide Python package.
