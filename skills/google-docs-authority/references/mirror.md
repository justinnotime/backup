# Mirroring selected documents

The same package owns native Markdown and HTML/pandoc exports, nested tabs,
original-image matching, Git LFS pointer recognition, incremental state and
linked-document discovery. It makes no model calls and does not commit or push.
Install the package's locked Python dependencies, including Pillow, and pandoc.
Git LFS is needed when existing original images are stored as LFS objects.

Add `read_token_file` and `mirror` to the existing configuration schema. This
synthetic example is independent of any private repository:

```json
{
  "schema": "google-docs-authority/v1",
  "read_token_file": "~/private/google/read.json",
  "mirror": {
    "repository_root": "~/documents",
    "output_directory": "archive/google-docs",
    "source_list": "settings/documents.yaml",
    "discovered_list": "settings/discovered.yaml",
    "state_file": "~/private/google/progress.json",
    "cache_directory": "~/.cache/google-documents",
    "engine": "markdown",
    "redact_enabled": true,
    "redact_command": ["/path/to/redactor", "--json", "--tier", "@tiers@"]
  }
}
```

Source lists use `docs: [{id: example-document, slug: example-document, title:
Example}]`. Document IDs and slugs must be unique. Each entry may select
`mask_tier` as a comma-separated subset of `hard,ctx,heur`. Keep real lists and
credentials private. An optional discovery list uses the same document format.

`--root` selects the repository for both inputs and outputs. Output, source lists
and the optional legacy `cache_link` resolve within that root; state and cache
storage remain external. Keep the selected source files in a transaction's
checkout. `--state-file` selects staged progress supplied by the publisher.
Paths expand HOME, `~` and environment variables. Preserve the existing state
file and archive together when migrating an installation.

The redactor receives document text on stdin and must emit JSON with `text`
and a nonnegative integer `replacements`. It must exit zero; malformed output,
timeouts or execution failures stop the mirror. Arguments are passed directly,
without shell evaluation; `@tiers@` becomes the comma-separated selected tiers.
The public `secret-lint/scripts/redact --json --tier @tiers@` implements this
external interface. No sibling source code is imported. A caller choosing to
archive without redaction must explicitly set `redact_enabled: false`; `--no-mask`
cannot override an enabled private policy.

```bash
scripts/sync --config /private/documents.json --doctor
scripts/sync --config /private/documents.json --dry-run
scripts/sync --config /private/documents.json --root /private/task-worktree \
  --state-file /private/staged-progress.json
scripts/sync --config /private/documents.json --only example-document
scripts/sync --config /private/documents.json --from-cache --force
```

Native Markdown is preferred, with HTML conversion for unsupported native
exports. A failed HTTP request or timeout during the initial Drive version/name
preflight falls back to a full export; this optimization is not required for
mirroring. Authentication/configuration failures and metadata needed to complete
an export remain errors. The fallback still validates the document and saves
progress only when the run succeeds.

Large-document export links may redirect between allowed Google HTTPS asset
hosts. Every destination is validated, and a host change removes authorization
and cookie headers; the standard redirect limit still applies. OAuth exchanges,
ordinary API calls and original-image requests do not follow redirects.

Unchanged documents retain their archive. Titles can rename directories
while their document identity stays stable. Multi-tab documents retain a linked
index and nested tab paths. Images retain content-derived names and original
bytes where available. LFS pointers are never treated as decoded pixels, and a
comparable image set that falls below `image_shrink_floor` (default 0.7) is
refused. `allow_image_shrink` and `allow_no_pillow` default to false and are
explicit caller choices, not automatic recovery actions.

Selected-document errors or refusals return nonzero and leave the progress file
unchanged. A caller's isolated worktree may contain partial outputs after
failure; discard or inspect that failed transaction instead of publishing it.
Cache downloads can remain for retry. A wrapper must propagate this exit status
before registry updates, commits or durable progress promotion. Inaccessible
selected documents are not silently removed from configuration or archive.

`--from-cache` is strictly offline and fails when required cached data is absent.
`--crawl` deliberately discovers accessible linked documents and updates the
configured discovery list; it expands future selection and should be requested
explicitly. `--setup-cache` creates only the optional configured legacy cache
link and refuses a conflicting existing path. Current attachment storage does
not require callers to introduce a legacy link.

Advanced settings include `pandoc_command` (argv prefix), `pandoc_timeout`
(positive seconds), optional `pandoc_memory_max` for available user-systemd
scopes, and `readme_header` for an established archive's generated comment.
`allow_unauthenticated` defaults to false; enabling public-document exports does
not suppress a rejected configured credential. HTTP error bodies and credential
values are not printed. Logs can still contain selected document names and
identifiers and belong in private storage.
