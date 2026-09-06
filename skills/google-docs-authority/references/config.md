# Configuration and runtime

Requires Python 3.10+, PyYAML and Pillow. `uv sync --locked` installs dependencies into
this package's environment; set `GOOGLE_DOCS_AUTHORITY_PYTHON` to its Python when
calling the shell wrappers, or use `uv run --locked gdocs-publish` and the other
installed commands. Markdown upload is native. Non-Markdown inputs use HTML
and require `pandoc` unless already HTML; their tracking metadata must be
maintained separately, so Markdown is preferred.

The config is selected by `--config`, then `GOOGLE_DOCS_AUTHORITY_CONFIG`, then
`${XDG_CONFIG_HOME:-$HOME/.config}/google-docs-authority/config.json`. Example
with synthetic paths:

```json
{
  "schema": "google-docs-authority/v1",
  "write_token_file": "~/private/google/write-token.json",
  "pageless": false,
  "registry": {
    "repository_root": "~/documents",
    "output": "metadata/google-docs.json",
    "source_directories": ["articles", "learning"],
    "mirror_directory": "mirrors/google-docs",
    "source_lists": {"selected": "~/private/google/documents.yaml"}
  }
}
```

Paths expand `~` and environment variables. Relative root, token and source-list
paths are relative to the config file. Output, source directories and mirror
paths are relative to the selected repository and must stay within it.
`registry --root /selected/worktree` overrides only that repository root;
source-list and token selection do not change. Source directories must exist.
Only configure directories needed for the registry. A publication-only config
may omit `registry`; a registry-only config may omit `write_token_file`.

A write-token file is an existing OAuth authorized-user JSON object containing
`client_id`, `client_secret`, and `refresh_token`. The command refreshes an access
token in memory and never rewrites that file. The response must grant exactly
`https://www.googleapis.com/auth/drive.file` or
`https://www.googleapis.com/auth/drive`; read-only Drive scope is insufficient.
Account setup uses the included, explicitly invoked `scripts/auth` command;
see [authorization and inspection](tools.md). `--token` selects an explicit alternative.

The same configuration supports [complete mirroring](mirror.md) through a
`mirror` section and separate `read_token_file`. These fields are optional for
publication-only use. `render.pdftoppm_command` optionally configures page
rasterization. The package never chooses an account or source list on behalf of
an unconfigured caller.

Commands, from this package directory:

```bash
scripts/publish --config /path/to/private/config.json article.md --dry-run
scripts/publish --config /path/to/private/config.json article.md
scripts/publish --config /path/to/private/config.json article.md --update example-document
scripts/registry --config /path/to/private/config.json --write
scripts/registry --config /path/to/private/config.json --check
scripts/fingerprint article.md
scripts/fingerprint # synthetic self-test
```

Actual publish commands mutate Google and local source frontmatter. `--folder`
is create-only; `--pageless`/`--no-pageless` override layout. Pageless styling
failure is reported separately; it does not reverse an accepted upload.
Publication exit statuses: 0 verified, 2 local input/configuration or record
failure, 3 authentication/API/verification failure, 5 online text drift. A
nonzero result after upload does not mean Google was unchanged. No automatic
upload retry is made. HTTP response bodies are not printed, and redirects are
refused to avoid forwarding credentials to another endpoint.

Markdown sources require YAML frontmatter. A verified publication maintains:

```yaml
gdoc:
  id: example-document
  mode: published
  fingerprint: sha256:example
  published_at: "2030-01-01T00:00:00Z"
```

The fingerprint above is illustrative, not a real content hash. Existing
unrelated metadata and body text are preserved. Before recording, the publisher
checks that the original source bytes have not changed during the operation.
Local records are replaced atomically. Run one publisher per source; this is not
a distributed concurrency or merge system.

Registry source lists use `docs: [{id: example-document}]`. Mirror directories
contain one `*/manifest.yaml` per document with `docId` and optional `title`.
The registry is deterministic and derived from those manifests and source
frontmatter. It rejects conflicting ownership, duplicate published sources and
malformed records before replacing the old output. Missing mirrors for configured
IDs are warnings. `authority: vault` means the configured local repository.
Registry exit status is 0 for success and 1 for invalid/stale/unreadable inputs.

Private consumers may import `canonical`, `fingerprint` and `self_test` from
`google_docs_authority.fingerprint` using an installed package or this package's
`src` directory. The canonical algorithm is a compatibility interface: changing
normalization invalidates previously recorded hashes. No sibling package or
private repository source is imported.

Run `uv run --locked pytest tests`, `uv run --locked ruff check .`, and
`uv run --locked ruff format --check .`. Tests use synthetic files and mocked
HTTP responses; they do not validate a live account or authorize publication.
