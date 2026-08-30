---
name: agent-session-extraction
description: Extract, audit, reconcile, and stage agent-harness sessions through the deterministic manifest-driven runtime. Use for Claude Code, Codex, OpenCode, DeepSeek Harness, Cursor, or OpenClaw session archives; do not use it to infer access from Backup profile labels or to switch a production writer without separate authorization.
---

# Agent Session Extraction

Use the scripts in this package as the behavior authority. A scheduler calls
the scripts directly; it does not invoke this Skill conversationally.

Before reading a source, require a valid versioned manifest. Treat source
paths, node labels, ownership, project policy, cleanup authority, redaction,
and publication as explicit configuration. Never infer a consumer realm,
machine identity, or read authority from a hostname, current directory, or
Backup profile label.

## Operations

- Run `scripts/doctor --manifest PATH` to check configuration, source-path
  policy, decoder availability, and redaction self-tests without decoding
  transcript text.
- Run `scripts/extract --manifest PATH --dry-run` for the complete read,
  decode, policy, render, redaction, audit, cleanup-plan, and reconciliation
  path with no persistent source/output, Git, marker, or cleanup mutation.
- Run `scripts/reconcile --manifest PATH [--failure-marker PATH]` to compare
  accepted source sessions with preserved and planned output. Its report may
  contain source/session identifiers and counts, never transcript text.
- Run `scripts/extract --manifest PATH` only when publication to the manifest's
  owned subtree is authorized. `git-worktree` publication also requires an
  explicit `--prepare-worktree PATH`; it stages but never commits or pushes.

Do not place real manifests, private patterns, account labels, host maps, or
source paths in this shared package. Keep them with the consumer that owns
them. A reconciliation or required-source failure blocks cleanup and
publication.

## Contracts

Read [references/manifest.md](references/manifest.md) when creating or
reviewing a consumer manifest. Validate it against
[schemas/manifest-v1.json](schemas/manifest-v1.json).

Read [references/normalized-session.md](references/normalized-session.md)
when integrating a new decoder, shadow comparison, renderer, or indexer.
