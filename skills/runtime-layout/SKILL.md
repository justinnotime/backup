---
name: runtime-layout
description: Resolve caller-configured credential, state, cache and lock paths across legacy and consolidated layouts, and inspect or explicitly apply a guarded local migration. Use for portable runtime configuration and directory moves; does not discover accounts or select services to migrate.
---

# Runtime layout

Use one private configuration for Python path queries, generated Bash bindings
and an explicit migration plan. The package has no account names, deployment
paths, credential contents or service defaults.

Inspect paths with `scripts/paths --config /private/layout.json KEY [ARG ...]`.
The Python `runtime_layout.Layout` API accepts the same configuration and an
explicit `repository_source`. Add this package's `src` directory to the caller's
Python path, or use the executable interface. Configuration remains caller-owned.

For an existing Bash integration, `scripts/paths --config /private/layout.json
--repository-source /path/to/project --shell` emits bindings declared by that
trusted configuration. Load the output once. Subsequent path queries do not start
Python or Git; environment overrides and directory existence remain dynamic.

`scripts/migrate --config /private/layout.json` only inspects the configured
paths and Git worktree metadata. It does not create directories, open writer
locks or call services. Inspect the complete selected scope before executing
`scripts/migrate --config /private/layout.json --apply`. A plan does not grant
permission to stop services, move credentials or reinstall schedules.

Read [configuration and compatibility](references/configuration.md) for path
selection, shell behavior, migration item types and recovery limits. Actual
migration requires an explicit complete writer-lock set and bounded caller-owned
service commands. Distinct existing old/new lock files are refused; the tool does
not replace lock inodes or repair arbitrary historical partial migrations.

Runtime dependencies: Python 3.10+, Bash for shell bindings, Git for repository
or worktree operations, and a local filesystem supporting advisory locks and hard
links for migration. No third-party Python runtime dependency or model calls.

Package checks:

```bash
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked skills-ref validate "$PWD"
```

The Skill format checker requires Python 3.11+; the runtime tests also support
Python 3.10. Use synthetic paths and fake service commands for development.
