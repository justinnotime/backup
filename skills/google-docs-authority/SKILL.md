---
name: google-docs-authority
description: Publish or republish a configured local source as a Google Doc, check document authority and online text drift, or regenerate a source-derived registry. Use when a user asks about tracked Google documents, publication, authoritative copies, or handoff. Requires caller-owned configuration and Google credentials; does not provide a mirror downloader or bidirectional merge.
---

# Google Docs authority

Read an optional private profile from `GOOGLE_DOCS_AUTHORITY_PROFILE`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/google-docs-authority/profile.md`. It selects
repository policy and existing mirror operations; do not copy its contents into
this package. Missing configuration is a constraint, not a reason to guess an
account or document ID. Follow the source repository's write protocol.

Use [configuration and commands](references/config.md). Install this package's
own dependencies with `uv sync --locked`; command wrappers use
`GOOGLE_DOCS_AUTHORITY_PYTHON` or `python3`. Select an explicit private config
with `--config`, `GOOGLE_DOCS_AUTHORITY_CONFIG`, or the default configuration
path described in the reference.

Read the registry and exact source `gdoc` metadata before acting. There is one
authoritative side:

- `published`: the local source is authoritative. The publisher checks the last
  recorded live text fingerprint before replacing Google content.
- `mirror`: Google is authoritative. Read the caller's existing mirror; never
  publish that mirror back to Google.
- `handed-off`: Google became authoritative; the local source is frozen. Do not
  republish it. Handoff requires the caller's explicit source/mirror procedure;
  this package does not automate it.

Start a publication review with `scripts/publish source.md --dry-run`; this
reads local inputs only. To update an existing document, pass its exact ID as
`--update DOCUMENT_ID`. An actual publication needs the user's authorization to
write that document. An inspection or sync request does not authorize discarding
online edits. Use `--force` only when the user has explicitly chosen that result.
It still requires a readable live export and cannot bypass document identity or
authority checks.

After an authorized upload, the publisher re-exports the live document and
records its fingerprint only when it matches the source. If a failure occurs
after upload, Google may already have changed. Use the reported document ID to
inspect the outcome before retrying; never blindly create another document.

The fingerprint compares normalized text. It excludes presentation, image
bytes, link destinations, whitespace and some punctuation. Equality is not
proof that images, formatting or every semantic distinction are unchanged.
Use `scripts/fingerprint source.md` and the shared Python API described in the
reference; do not create a second normalization algorithm.

Regenerate the derived registry with `scripts/registry --write`, then verify
with `scripts/registry --check`. These commands read configured local records
and make no Google requests. Commit source metadata and registry changes through
the caller's normal process. Report the exact document, authoritative side,
whether Google changed, verification outcome and remaining action.
