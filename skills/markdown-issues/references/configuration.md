# Configuration and commands

Start from [the synthetic example](example.json). Pass an explicit configuration
file to every command. `--repo` overrides `repository_root` for an isolated Git
worktree; `--today YYYY-MM-DD` provides a reproducible date for review calculations.
Paths in the configuration are not a declaration that source data may be published.

| Field | Meaning |
| --- | --- |
| `schema` | `markdown-issues/v1` |
| `repository_root` | Selected repository; `~` and environment variables are expanded. |
| `open_directory`, `closed_directory` | Repository-relative directories containing flat `*.md` issue files. |
| `actors` | Allowed assignees and note authors. |
| `default_actor`, `default_assignee`, `unassigned_actor` | Creation defaults and the marker for work with no assignee. |
| `priorities` | Allowed priorities, ordered highest first; the first two are urgent. |
| `kinds`, `sub_states` | Allowed workflow values. The `watch`, `waiting-human` and `scheduled` values retain their scheduling meaning. |
| `related_path_prefixes` | Prefixes identifying repository paths inside the otherwise opaque `related` field. |
| `stale_days`, `idle_days` | Warning thresholds for unchanged issues and note inactivity. |
| `headings` | Exact second-level headings for `context`, `acceptance` and `notes`. |
| `base_refs` | Ordered fallback Git refs for historical lint, normally `origin/main`, then `HEAD`. |
| `body_template` | Optional creation body with `{{title}}`, `{{actor}}`, `{{now}}`, and the three `{{..._heading}}` placeholders. |

Sources and watched paths stay inside the repository. Symlinked issue directories
and issue files are rejected. Creation validates the proposed record, installs it
atomically, and refuses an existing destination. It never changes existing records.
`MARKDOWN_ISSUES_PYTHON` selects the executable used by the shell entry point.

Actor labels use lowercase letters, digits and hyphens, matching the note-record
syntax. The runtime retains the `action`, `watch` and `external` kind meanings,
`waiting-human`/`scheduled` deferral behavior, and the missing-field defaults
`action` and `P2`. Keep these values in the configured vocabulary when using those
behaviors; configuration selects accepted values rather than defining another
state-machine language. Creation always requires an explicit priority.

## Record format

Frontmatter is a deliberately limited scalar/block-list/inline-list subset of
YAML. It is not a general YAML parser. Required scalars are `id`, `title`,
`created`, `updated`, `state`, `assignee` and `priority`. Optional fields include
`kind`, `sub_state`, `project` and `review_after`; list fields are `labels`,
`sources`, `related`, `external_refs`, `blocks`, `blocked_by` and `watch_paths`.
Double-quoted strings and string-only inline lists support JSON escaping. Simple
single-quoted strings and unquoted legacy values remain supported. Creation uses
JSON quoting for titles and list entries, preserving quotes and backslashes.

The filename stem equals `id`. `state` is `open` or `closed` and agrees with the
directory. UTC timestamps use `YYYY-MM-DDTHH:MM:SSZ`. Note records begin with
`- <UTC timestamp> [actor] <text>` and may have wrapped continuation lines.
Context and acceptance sections must contain text; action/external acceptance
contains at least one checkbox. Dependencies resolve to other issue IDs.

Creation retains a stable date/ASCII-slug/eight-hex identifier format. The suffix
is derived from the UTC date, slug, creator and timestamp; it is an identity aid,
not a cryptographic security mechanism. If two requests produce the same name,
the second fails without overwriting the first.

## Inspection and history

`lint [--base-ref REF] [--tsv]` checks structure, references, IDs, vocabulary and
the append-only contract. Existing malformed note records remain warnings when
an unchanged base record proves they are inherited. New malformed or rewritten
records are errors. Deletions, renames, changed creation dates and meaningful
edits without an advanced timestamp and appended note are errors.

An ahead/diverged comparison ref with issue changes produces an error rather than
misattributing newer notes to this checkout. An available merge base is still
used to identify actual local violations and preserve inherited warning levels.
No fetch, pull, reset or commit is performed by the inspector.

`brief [--limit N] [--assignee NAME] [--watch-ref REF]` lists actionable issues
before future scheduled reviews. Source changes under `watch_paths` make an
otherwise future-scheduled issue actionable again. `watch-signals [--ref REF]`
prints tab-separated issue IDs and changed paths since each issue's last committed
edit, including current tracked worktree changes.

## Creation

`create TITLE --priority VALUE` accepts `--actor`, `--assignee`, `--kind`,
`--project`, `--sub-state`, `--review-after`, comma-separated `--labels`, and repeated
`--source`/`--watch-path`. Both `--name=value` and `--name value` work. A watch with a
review date defaults to sub-state `scheduled`. `--dry-run` prints the proposed
document without creating directories, files, Git state or external requests.

## Verification

From this package:

```bash
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked agentskills validate "$PWD"
```

The Skill validator is available on Python 3.11 or newer; runtime tests also cover
Python 3.10. Tests use synthetic files and local Git repositories, with no account,
private repository, service or model access.
