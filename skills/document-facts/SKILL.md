---
name: document-facts
description: Extract structured facts from local document mirrors, rebuild digests and chronological timelines, or synthesize configured thematic threads. Use for document evidence processing with explicit sources and model configuration; does not parse video files or fetch remote documents.
---

# Document facts

Use the bundled runtime with a caller-owned JSON configuration. It reads Google
Docs mirror directories containing `manifest.yaml` and either `README.md` or
ordered tab files. Generated facts and synthesized reports belong in an output
directory separate from the source archive.

Read [configuration and modes](references/configuration.md) for the schema,
credential selection, source identity, estimates, and incremental behavior.

```sh
uv sync --project /path/to/document-facts --locked --no-dev
DOCUMENT_FACTS_PYTHON=/path/to/document-facts/.venv/bin/python \
  /path/to/document-facts/scripts/extract --config /private/document-facts.json --dry-run
```

Run extraction by removing `--dry-run` when model processing is authorized.
`--root /path/to/worktree` redirects repository-relative input and output paths
into that worktree. The runtime does not commit or publish generated files.

- `--doctor` checks local configuration and explicitly selected documents.
- `--dry-run` never creates a client, reads credentials, writes files, or calls
  a model. Estimates include unchanged chunks only when `--force` is supplied.
- `--digests-only` and `--build-timeline` use saved facts without model calls.
- `--build-threads` synthesizes the configured themes and therefore uses a model;
  combine with `--dry-run` to estimate first.
- Failed or incomplete responses return failure. Successful chunk files remain
  usable for a retry. Inspect that failure before attempting publication.

LLM calls > 0 in extraction and thematic-thread modes. Model output is derived
content, not an authoritative replacement for the original source. Follow source
citations when judging a factual claim. Do not describe this as video extraction:
it processes document text even when the documents discuss videos.

Verification with synthetic data:

```sh
uv sync --project /path/to/document-facts --locked --group dev
cd /path/to/document-facts
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync agentskills validate "$PWD"
```
