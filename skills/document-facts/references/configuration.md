# Configuration and operating modes

The entry point is `scripts/extract --config FILE`. Configuration is JSON with
`schema: "document-facts/v1"`. No configuration, account, model, provider address,
or document selection is discovered from a personal directory.

```json
{
  "schema": "document-facts/v1",
  "repository_root": "${HOME}/archive",
  "source_directory": "Raw/documents",
  "output_directory": "Wiki/document-facts",
  "state_file": "Wiki/document-facts/.extract_state.json",
  "timeline_file": "Wiki/document-timeline.md",
  "threads_directory": "Wiki/document-threads",
  "documents": [
    {"id": "synthetic-document-id", "output_slug": "sample-document"}
  ],
  "year_range": [2030, 2032],
  "llm": {
    "model": "caller-selected-model",
    "base_url": "https://provider.example.invalid/api",
    "api_key_env": "DOCUMENT_MODEL_KEY",
    "required": true,
    "timeout_seconds": 120,
    "max_attempts": 3
  },
  "metadata": {"timeline_title": "Document history"},
  "threads": [
    {
      "slug": "interface-design",
      "title": "Interface design",
      "what_it_covers": "Changes to the interface described in the selected documents.",
      "search_terms": ["interface"],
      "exclude_terms": []
    }
  ]
}
```

Repository paths must remain inside `repository_root`; symlinks in data paths are
rejected. Relative paths and `${HOME}` expansion are supported. `--root` remaps
these paths into another repository worktree. Absolute paths under the original
root also remap. Source and extraction output directories cannot overlap.
Credential and prompt paths are relative to the configuration file, not the
repository, and do not change under `--root`.

`documents` is a nonempty explicit selection. Prefer full `manifest.docId` values:
they survive title changes. `output_slug` preserves an existing extraction
directory name independently from the source directory name. Without it, the
explicit `slug` or full document ID determines the output directory. A `slug`
selector can use a current directory name; old `id-prefix--title` selectors are
resolved only when the ID prefix matches exactly one document. `target_slugs`
is accepted as an alternative to `documents` for older integrations. Missing,
ambiguous, or duplicate selections fail. Selected tab files must exist, stay in
their document directory, and follow the manifest's order.

If an extraction directory has already been renamed while its historical YAML
still stores the old `doc_slug`, add `previous_slugs: ["old-output-name"]` to that
document selection. Only these explicit aliases are accepted. The current path,
chunk ID, input hash, and any stored full document ID must still match. Reports
use the current `output_slug`; historical YAML files are not rewritten.

`year_range: [minimum, maximum]` limits inferred document-year context. If omitted,
explicit document dates establish context; the default prompt omits genuinely
ambiguous dates. `prompts.extract` and `prompts.thread` override the generic system
prompts with caller text. Use `extract_file` or `thread_file` instead of inline
text to load a prompt from a configured file. Provider system prompts use
ephemeral prompt caching.

`llm.model` and an HTTPS `llm.base_url` are required for model modes. The provider
must implement Anthropic's messages API. Set an explicit `api_key_env` and/or
`credential_file`. A credential file contains a JSON string under `credential_key`
(default `api_key`). When both are configured, a nonempty selected environment
variable takes precedence, then the selected file. This does not change the
configured provider. No conventional environment key or credential path is read
implicitly. Missing or unreadable credentials fail by default. An explicitly
optional client (`required: false`) may skip when its credential is absent;
unreadable explicitly configured credential files still fail. Redirects are not
followed, and error output omits provider response bodies and credential values.

`metadata` supplies additional report frontmatter such as a caller's project
label. Generated `title`, `type`, `created`, and `sources` remain runtime-owned;
`timeline_title` and `threads_title` configure report titles. `generator_label`
is accepted for caller compatibility and is not inserted into source facts.

`budget` options and defaults:

| Option | Default | Meaning |
| --- | ---: | --- |
| `max_chunk_chars` | 16000 | Maximum source chunk size |
| `soft_split_chars` | 12000 | Preferred split size for oversized tabs |
| `max_tokens` | 8000 | Maximum extraction output tokens per call |
| `thread_max_tokens` | 5000 | Maximum thread output tokens per call |
| `thread_prompt_chars` | 30000 | Maximum compacted evidence characters; source listing and theme metadata are additional |
| `thread_chunk_chars` | 1800 | Maximum compacted contribution per chunk |

Thread selection uses case-insensitive `search_terms` and `exclude_terms` over
tasks, decisions, concepts, blockers, quotes, and headings. An optional
`include_slugs` list restricts the configured source set by stable ID, unique ID
prefix, or output/source slug. The generated prompt distinguishes listed source
chunks from content omitted by the budget. Thematic synthesis is selective and
compacts evidence; per-chunk facts remain available for inspection.

# Outputs and resuming

Each chunk is written as `<output_directory>/<output_slug>/<chunk-id>.yaml`.
The schema preserves `doc_slug`, `chunk_id`, `heading`, `chunk_index`,
`chunk_total`, `input_sha1`, `char_count`, `extracted_at`, `model`, and the arrays
`dates_found`, `tasks`, `decisions`, `concepts`, `blockers_solutions`,
`notable_quotes`, `people`, `references`. New files additionally record `doc_id`
and `extraction_signature` for the configured model, prompt, and document context.

The state file retains the legacy mapping of document slug to chunk ID to
`sha1`/`extracted_at`. Successful atomic YAML files are the durable checkpoint;
removing the state file does not force new model calls. Legacy YAMLs containing
`input_sha1` (or `content_sha1`) can resume without a signature. New output is
invalidated when its recorded extraction signature changes. Failed calls and
invalid schema never mark a chunk complete. Each successful chunk is saved
before its state entry, so interrupted state writes recover from saved YAML.

Chunk names retain the numbered heading-anchor format. Normal extraction builds
its digest from the current chunk set. Old files remain for explicit cleanup,
rather than deleting historical extraction results automatically.
The independent digest, timeline, and thread modes read all saved extraction
YAMLs for the selected documents as a snapshot, including older chunks that may
no longer occur in current source text. They validate file identity and structure
without requiring source hashes or model/prompt settings to match. Current source
manifests identify the selected documents, but their text need not be present.
These reports can lag the source documents; refreshing the facts is a separate
model operation. A capped extraction
does not overwrite a document digest with a partial result.

Each document's `README.md` is its mechanical digest. The chronological timeline
uses the earliest date in each chunk; undated chunks inherit the most recent
date from a preceding chunk of the same document. Threads record their input
hash in frontmatter and rerun when their source facts or settings change. Legacy
thread files without that hash need one synthesis pass to gain resumable state.
Existing report creation dates are retained across regeneration.

# Preview, filtering, and cost

`--dry-run` applies to every mode. `--doctor`, `--digests-only`, and
`--build-timeline` never call a model. `--only TEXT` restricts configured sources
by slug or document ID; no match is an error. `--only-thread TEXT` requires
`--build-threads`. `--limit-chunks N` bounds the number of actual attempted chunks,
including failures, rather than merely stopping between whole documents.

The estimate uses roughly four characters per input token and reports the
configured maximum output tokens. For example, a synthetic corpus of one million
characters split into about 84 calls at 12000 characters has about 250000 source
input tokens, plus prompt overhead, and at most 672000 output tokens at the
default cap. This is an order-of-magnitude budget, not measured provider usage;
language mix, tokenization, caching, and retries change actual consumption.
Prompts are cached when the configured provider supports the API caching field.
No current model price is built in. Optional `cost.input_per_million` and
`cost.output_per_million` use caller-supplied rates to report an upper output
allowance estimate; actual successful token usage is also reported.

The legacy chunker ignores prefaces of at most 200 characters before the first
H1 heading (normally a mechanical export notice) and empty heading-only sections.
Oversized sections use the configured character boundaries. This preserves
existing chunk identifiers and input hashes; it is not a byte-for-byte archive.
