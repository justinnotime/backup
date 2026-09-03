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
  `--output-root ABSOLUTE_PATH` overrides only the manifest's output repository
  for that invocation, so a consumer can target its authorized disposable
  checkout without rewriting source policy or the manifest on disk.

For `git-crypt` publication, the key is linked into the throwaway worktree and
is never copied. Its target is relative to that worktree's private Git
directory: `git-crypt/keys/default` requires the `git-crypt` filter, while
`git-crypt/keys/<name>` requires `git-crypt-<name>`. The publisher creates a
no-checkout worktree, populates only its private index, validates cached
attributes for every tracked owned file and planned write, and links the key
before checkout can invoke a smudge filter. After checkout it refuses
ciphertext, and after staging every planned write's index blob must carry the
git-crypt ciphertext header without its planned plaintext. Any failure removes
the throwaway worktree and leaves the source repository untouched.

Because a throwaway worktree starts from `HEAD`, `git-worktree` publication
requires every owned output subtree to match `HEAD` immediately before and
after inventory is read. The same check runs again immediately before worktree
preparation. Tracked changes, untracked files, ignored files,
`skip-worktree`, and `assume-unchanged` state inside those subtrees fail
closed. Repository dirt outside the owned subtrees remains allowed.

Do not place real manifests, private patterns, account labels, host maps, or
source paths in this shared package. Keep them with the consumer that owns
them. A reconciliation or required-source failure blocks cleanup and
publication.

Use `sqlite-readonly` for a live or WAL-backed OpenCode database. Use
`sqlite-immutable` only for a checkpointed snapshot whose producer guarantees
immutability. Configure per-harness history directories and filename
strategies in the manifest rather than adding consumer-specific render paths.
Use a legacy compatibility rule only while adopting existing output; it does
not relax the contract for newly written output. The frozen legacy rule keeps
every recognized legacy file out of cleanup and policy removal; legacy history
and prompt files are rendered again when their session grows, and indexes
continue to be rebuilt from the complete preserved inventory. Remove that rule
explicitly when adopted legacy files may be cleaned like managed output.

Parity adapters that already hold caller-frozen byte snapshots may import
`decode_source_snapshots` from `agent_skills.sessions.api`. It applies the
shared decoder, policy, normalization, and deduplication contract without
source discovery, rendering, cleanup, or publication. It rejects direct-path
snapshots; production extraction remains responsible for path validation and
source revalidation.

## Contracts

Read [references/manifest.md](references/manifest.md) when creating or
reviewing a consumer manifest. Validate it against
[schemas/manifest-v1.json](schemas/manifest-v1.json).

Read [references/normalized-session.md](references/normalized-session.md)
when integrating a new decoder, shadow comparison, renderer, or indexer.
