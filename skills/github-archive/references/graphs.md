# Local graphs, timelines and inventories

`scripts/graph` reads an explicitly selected directory of archived issue and
pull-request Markdown files. It does not fetch GitHub records, call a model,
commit files or choose a repository. Use the same Python environment as
`scripts/sync`; only PyYAML is required.

```sh
scripts/graph --input-dir /work/archive/example_project \
  --label example --title-include 'foundation|delivery' \
  --trackers 101,102 --out-dir /work/generated --prefix example
scripts/graph --input-dir /work/archive/example_project --mode timeline
scripts/graph --input-dir /work/archive/example_project --mode stats
```

Input and output paths support `~` and environment variables. No input directory
is inferred from the current directory, account, Git checkout or another Skill.
Relative paths resolve against the calling directory. The input is the single
repository's archive directory, not its parent collection.

## Input and selection

The parser reads direct `*.md` children with YAML frontmatter. Its fields are
`number`, `title`, `state`, `labels`, `related`, `created`, `closed`, `type` and
`author`. `number` identifies each item; `related` contains issue numbers in the
same archive. Files without numbered mapping frontmatter, including ordinary
indexes, are ignored. Malformed YAML frontmatter is also ignored, preserving
the existing archive-reader behavior; stderr reports the count successfully
parsed. The command is a visualization of readable archive metadata, not an
archive completeness validator.

Repeated `--label` filters and the case-insensitive `--title-include` regular
expression combine with OR. `--title-exclude` is applied first. With no inclusion
filter, all parsed issues are selected. Relationships to unselected or missing
issues are omitted.

`--trackers 101,102` selects explicit tracker numbers. Otherwise trackers are
identified by `[tracker]` or `[master]` in the title, a `tracker` or `master`
label, or references to at least `--tracker-min-outdeg` selected issues
(default 10). Tracker detection is a heuristic. Arrows represent archived
`related` references; a reference alone does not prove a blocking dependency.

## Output modes

| Mode | Output |
| --- | --- |
| `all` (default) | All four artifacts below |
| `full-dot` | `<prefix>-issue-graph.dot`: tracker clusters, open children, selected closed children and shared relationships |
| `spine` | `<prefix>-tracker-spine.dot`: tracker nodes and references between them |
| `timeline` | `<prefix>-timeline.txt`: daily open/close density attributed to trackers |
| `inventory` | `<prefix>-issues.json`: all selected issue metadata with a tier |
| `stats` | Counts and tracker summaries on stdout; writes no files |

`--prefix` defaults to the local calendar date and must be one filename
component. With `--out-dir`, selected artifacts are atomically replaced in that
directory; file symlinks are refused. Without it, artifacts appear on stdout
with filename headings. `--quiet` suppresses informational stderr output.

The full graph includes all selected open children, up to
`--closed-per-tracker` closed children ranked by incoming reference count
(default 6), and up to `--top-cross` issues referenced by several trackers
(default 8). Limits affect the graph display, not the inventory. Closed children
and shared references can therefore be absent from the drawing while remaining
in the inventory.

Timeline `--start` and `--end` use inclusive `YYYY-MM-DD` dates. Without explicit
dates, the range runs from the Monday preceding the earliest recorded activity
through the latest activity. Ranges longer than 366 days produce an explanatory
message. Open and close dates count separately; one shared issue contributes
to every tracker that references it. Totals therefore count tracker-attributed
events, not unique issues. The text uses Unicode density characters.

The inventory's historical A/B/C/D tiers depend only on archived type and
state: A is a closed pull request, B a closed issue, C an open item, and D
other metadata. The preserved stats caption calls A `merged-PR`, but no merge
status is checked. Neither a tier nor a graph label establishes that work
shipped or that a closed pull request was merged.

DOT files are plain Graphviz source. Rendering is an optional separate local
step; Graphviz is not a runtime dependency:

```sh
dot -Tsvg /work/generated/example-issue-graph.dot -o /work/generated/example.svg
```

Artifacts preserve selected titles, authors, labels and relationships. Publish
them only within the authorization for that particular archive. Public examples
and tests use synthetic records.
