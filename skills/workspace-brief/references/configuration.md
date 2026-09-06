# Configuration

Use JSON with `schema_version: "workspace-brief/v1"` and an explicit absolute
`repository_root` (or a home-relative value). Every other section is optional.
Relative source paths resolve under that root; the selected directories and
explicit external paths define the read scope. Markdown enumeration does not
follow directory or file symlinks. No directory names or personal labels are
built into the reader.

Text lists `header`, `after_health` and `footer` appear in that order around the
selected sections. `@root@` and `$HOME` expand anywhere in text. `missing_root`
is printed only in debug mode and accepts `{root}`; an unavailable repository
otherwise produces no context. `debug_line` accepts `{project_dir}`.

| Section | Fields and behavior |
| --- | --- |
| `queue` | Explicit `argv`, `heading`, optional `indent`, `unavailable`, and `timeout_seconds` (default 2). No shell is invoked. Failed command output is suppressed. Use an installed reader such as `markdown-issues ... brief`. |
| `projects` | `directory`, `heading` (`{shown}`, `{total}`), optional `limit` (5), `readme` (`README.md`), `exclude_status_prefixes`, `latest_pattern` (`20*.md`), `file_line` (`{age}`, `{path}`, `{title}`), and `missing_readme` (`{project}`). Immediate project directories are ordered by newest recursive Markdown modification time; the README and latest selected top-level dated document are displayed. |
| `latest` | `directory`, `heading`, optional filename `pattern`, `exclude` names, and `file_line` with the same fields. Reads the most recently modified selected file. |
| `health` | `heading`, optional `marker_source`, `logs`, `cadence_multiplier` (2), `overdue_line`, `healthy_line`, and `artifacts`; details below. |
| `worktrees` | Inspect the configured repository's registered worktrees, excluding the main selected root and optional `exclude_roots`. `idle_days` defaults to 2, `base_ref` to HEAD, and `line` receives `{name}`, `{idle_days}`, `{dirty}`, `{ahead}`. Git uses `--no-optional-locks`; no fetch or mutation is requested. |
| `storage` | Explicit `path`, `minimum_free_inodes`, and `line` receiving `{path}`, `{free}`, `{used_percent}`. Uses portable `statvfs`, not GNU command flags. |

`marker_source` contains `path` to a selected JSON file, its required
`schema_version`, a `field` naming the absolute marker path, `line` receiving
`{detail}` and `{path}`, and an optional `error_line`. Only the marker's first
line is displayed. A missing marker file means no recorded failure. A missing
or malformed selected configuration is reported without exposing exception text.

Each log has `name`, ordered fallback `paths`, and `cadence_minutes`. The first
existing path wins. `overdue_line` receives `{name}`, `{age_minutes}` and
`{cadence_minutes}`. `healthy_line` requires every selected log to exist and be
within its interval; missing observations or a failed marker read instead use
`unknown_line` (default: a short unavailable warning). Log activity does not
certify output completeness.
Artifacts independently check that expected files exist.

Each artifact has `period` (`daily` or `weekly`), a path containing `{date}`,
`ready_hour_utc`, and `missing_line` receiving `{date}` and `{path}`. Daily
reports are expected for yesterday after that UTC hour, and two days ago before
it. Weekly reports use `period_end_weekday` (Monday 0 through Sunday 6): the
most recent strictly earlier end day, except the next day's pre-release hours
continue to expect the preceding week. No calendar service is contacted.

Queue inspection has a per-command timeout. All worktree Git calls share
`worktrees.budget_seconds` (default 2), so registering more worktrees does not
multiply the external-command budget. Exhaustion reports the section as
unavailable, never healthy. The hook's configured timeout remains the final
process limit; filesystem latency can still exceed a local inspection budget.
The reader reports data; it never kills stale worktrees or repairs a writer.

## Hook installation

The optional `hook` section declares `settings_path`, an `argv` list to quote
as one shell command, literal `previous_commands` to replace/remove, and
`timeout_seconds`. The installer only owns exact selected command strings.
Other hooks sharing an entry, matchers and extra fields are preserved. Existing
JSON settings must be a regular file, not a symlink. Changes create a private
timestamped backup and an atomic replacement; unchanged operations write nothing.

Install dependencies using `uv sync --locked --group dev` when developing.
Normal execution needs only Python and, when selected, Git or the external
queue reader. Copying this package alone is supported; configuration and private
data remain at caller-selected locations.
