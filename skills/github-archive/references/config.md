# Configuration and state

Configuration is a YAML mapping. No repository, account, output directory, or
state location is selected by default. Authentication comes from the caller's
GitHub CLI environment; never put credentials in this YAML.

| Field | Meaning |
| --- | --- |
| `output_dir` | Archive root; required unless `--output-dir` is passed. |
| `state_file` | Incremental JSON state outside the archive; required unless `--state-file` is passed. |
| `filename_template` | One `{number}` token in a Markdown basename; defaults to `{number}.md`. |
| `repos` | Nonempty list of selected repositories. |
| `repos[].owner`, `name` | GitHub repository identity. |
| `repos[].directory` | Optional single output directory name; defaults to `<owner>_<name>`. |
| `repos[].seeds.all` | All issues and pull requests through the paginated Issues API. |
| `repos[].seeds.labels`, `authors` | Lists of issue filters, combined as a union. |
| `repos[].seeds.involves` | Best-effort issue mention filters, combined with the other seeds. |
| `repos[].closure.depth` | Same-repository reference expansion depth; default 1. |
| `repos[].closure.max_total` | Maximum membership for new reference expansion; default 300. Seeds and existing members are retained even above this limit. |
| `repos[].exclude.labels` | Skip rendering records matching any label. |
| `repos[].exclude.closed_before` | Skip rendering records closed before this quoted ISO timestamp. |

Filtered discovery uses `gh issue list` and its 1000-item limit; it does not
provide complete pull-request discovery. Use `seeds.all: true` for a complete
issue and pull-request archive. Closure follows same-repository references;
cross-repository references are recorded but not fetched automatically. An
exclude rule prevents writes and does not remove an existing archive file.

Relative paths use the configuration directory unless `--base-dir` is supplied.
Explicit CLI path overrides use the same base. Output/state inside the Skill
are refused. Repository directory and filename components cannot contain path
separators; output directory collisions and symlink escapes are rejected.
Choose a destination that you own. Changing a layout does not delete old files;
use a new output directory or migrate existing files deliberately.

The state object is keyed by `owner/name`. It records `last_sync`,
`target_numbers`, `last_target_count`, and optional `excluded_numbers`. Existing
archive filenames recover membership if state is absent. Missing known files
are fetched again. Unchanged records skip body/comment requests and preserve
file contents. Keep state paired with its archive and filter configuration;
when changing selection, use `--full` or a fresh output/state pair.

Writes are local. The state file is replaced atomically, but the whole archive
is not a transaction. Use a caller-owned staging directory and promote both
archive and state only after exit code 0 when atomic publication is needed.
Schedule at most one writer for an output/state pair. Exit 1 reports a sync
failure; exit 2 reports invalid configuration/state or argument usage.
