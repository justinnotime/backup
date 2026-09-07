# Configuration and command contract

Run the package directly with Python 3.10 or newer; runtime dependencies are
limited to the standard library:

```bash
/path/to/structure-lint/scripts/check --root /path/to/project --config /private/rules.json
```

`STRUCTURE_LINT_CONFIG` can select the file instead of `--config`.
`STRUCTURE_LINT_PYTHON` selects the Python executable for the shell entry.
The root defaults to the current directory. No repository layout, account,
identity list, source ownership policy, or schema is inferred from the machine.
Missing configuration, an empty check list, unknown check types, unreadable
input, and failed external checks do not report a clean result.

Configuration is a JSON object with `schema: "structure-lint/v1"` and a
nonempty `checks` array. Each check has a `type`, optional `id`, and a
`severity` of `error` (default) or `warn`. Rules are trusted operator inputs:
an `external` check can execute its configured command, and `git_freshness`
can fetch when explicitly enabled. Review a configuration before running it.

Checks that select files use `include`, an array of relative Python glob
patterns. `**` is recursive. Optional `exclude` patterns are matched against
complete relative paths with shell wildcards, including `/`; `exclude_regex`
contains regular expressions matched against those paths. Paths must be
relative to the selected root and cannot contain `..`. Matches are sorted and
deduplicated. Empty selections are permitted because rules can describe optional
content; use `layout` or `required_files` for required content.

| Type | Required fields | Behavior and optional fields |
|---|---|---|
| `layout` | `document`, `section` | Read a Markdown section and check backtick directory declarations. `pattern` can replace the capture regex. Empty or missing sections fail. `undocumented` defaults true; undeclared visible top-level directories warn. Configure `ignore_directories`, `include_hidden`, and `undocumented_severity`. |
| `metadata` | `include` | Check initial `---` frontmatter, `fields` for top-level field presence, and `values` mapping fields to allowed strings. `nonempty` can require nonempty fields. `empty_frontmatter` defaults true and reports empty blocks. This checks field lines, not general YAML syntax or nested schemas; use an external schema validator for those. |
| `source_references` | `include` | Check indented list entries under a frontmatter `field` (default `sources`). Optional `prefixes` select local references. Exact targets must exist; `allow_parent` explicitly permits a missing target whose directory exists. `strip_annotations` removes trailing comments or parenthetical annotations. Inline YAML lists are outside this check's supported format. |
| `inline_paths` | `include`, `pattern` | Check each path captured by the regex, normally inside backticks. `ignore_suffixes` omits configured file suffixes. The regex must have exactly one capture group. |
| `navigation` | `include`, `indexes` | Require each selected document's filename stem to appear literally, ignoring case, in one selected index. `when_exists` can make this an optional content class. This is discoverability by name, not Markdown link or anchor validation. |
| `taxonomy` | `include`, `section` | Selected provenance documents declare allowed values in the first table column. `pattern` overrides that capture regex. Check sibling `members` (default `*.md`) except `exclude_names` for a `- **kind:**` bullet (`field` changes the name). Missing bullets/tables use `missing_severity` (default warn). `required_when` entries contain `value`, optional directory glob, and extra required bullet `fields`. |
| `required_files` | `include`, `files` | Select directories and require each relative file under them. |
| `forbidden_paths` | `include` | Report selected files, with optional `message`. Useful for an explicitly documented layout restriction. |
| `forbidden_text` | `include`, `pattern` | Report the first matching line per file. Optional `ignore_case`, `first_lines`, `skip_first_line_pattern`, and `message`. This enforces a configured text convention; it cannot establish who authored text or whether an LLM processed it. |
| `external` | `argv` | Execute an argument array without a shell, replacing `@root@` in arguments and setting the working directory to the root. Optional `include`/`exclude` skips invocation when no path is selected. `environment_defaults` and `expand_environment` support explicitly configured external commands as described below. The timeout defaults to 120 seconds. Output must be one `ERROR<TAB>message` or `WARN<TAB>message` per line. Invalid output, stderr, a timeout, or nonzero exit without an error fails the check. |
| `git_freshness` | none | Compare HEAD to `remote`/`branch` (defaults origin/main). `fetch` defaults false. An absent remote or failed fetch is skipped. `skip_environment` names an optional variable whose value `1` skips the check; command timeout defaults to 30 seconds. |

The default text output ends with `=== Summary: N errors, M warnings ===`.
`--format json` returns an object containing `root`, totals, and findings with
`level`, `message`, `check`, and `path`. `--format tsv` emits only the finding
lines, making this command usable by another explicit checker. Exit codes are
0 for no errors (warnings allowed), 1 for findings with errors, and 2 when a
check cannot complete. The runtime never rewrites repository content.

See [example.json](example.json) for a synthetic starting point. Keep personal
paths, ownership rules, and external policy adapters in private configuration.

## External command paths

An external check inherits the process environment. Its optional
`environment_defaults` object supplies defaults in declaration order: an existing
nonempty value wins, otherwise the default string strictly expands `$VAR` or
`${VAR}` from the inherited environment and preceding defaults. The child process
receives those values; the checker's own environment is unchanged.

Set `expand_environment: true` to expand the same variable syntax in each argv
element, followed by a leading `~` or `~/` using that child environment's `HOME`.
`@root@` is replaced last, so dollar signs in the repository path remain literal.
Without this flag, argv retains its previous literal behavior except `@root@`.
Missing variables, malformed substitutions, and a missing executable fail the
check. Use `$$` for a literal dollar sign when expansion is enabled. This is
single-pass substitution: shell expressions, nested defaults, globbing and
command substitution are not supported or executed.

For example, a caller can select one independently installed validator:

```json
{
  "type": "external",
  "include": ["Docs/**/*.md"],
  "environment_defaults": {
    "XDG_CONFIG_HOME": "$HOME/.config",
    "DOCUMENT_VALIDATOR": "$HOME/.local/bin/document-validator",
    "DOCUMENT_VALIDATOR_CONFIG": "$XDG_CONFIG_HOME/document-validator/config.json"
  },
  "expand_environment": true,
  "argv": ["$DOCUMENT_VALIDATOR", "--config", "$DOCUMENT_VALIDATOR_CONFIG", "--root", "@root@"]
}
```

With `include`, no matching path skips the command before resolving its
environment. Unlike built-in file checks, an external selector retains matching
directories and broken symlinks so the external validator can reject them.
Selection controls whether to run; it does not append file paths to argv.
Configure any file-selection arguments required by the external CLI.
Package locations belong to the caller: this checker does not search other
packages or import their source. Use a configured executable or installed
discovery link, with explicit overrides for a nonstandard installation.

Development checks run within this package:

```bash
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked skills-ref validate "$PWD"
```

The tests exercise failure cases and run a copied package under an unrelated
home directory. They do not contact a live service or inspect private material.
