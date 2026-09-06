---
name: markdown-issues
description: Create, validate and review local Markdown issue records with Git-backed append-only notes, due reviews, dependency references and watched-path signals. Use for a caller-configured file-based issue tracker; does not operate a hosted issue service or assign external work.
---

# Markdown issue tracker

Use the configured repository and vocabulary. Directory names, actor identities,
review thresholds and issue templates belong in the caller's configuration.
Read [the configuration and command reference](references/configuration.md) when
setting up a tracker or interpreting a failed historical comparison.

```bash
scripts/issues --config /private/tracker.json brief
scripts/issues --config /private/tracker.json lint --tsv
scripts/issues --config /private/tracker.json watch-signals
scripts/issues --config /private/tracker.json create "Review sample output" \
  --priority=P2 --assignee=writer --dry-run
```

Brief, lint and watched-path inspection are read-only. Creation writes one new
issue only when `--dry-run` is absent; it does not stage, commit, publish or send
anything. A close candidate means every acceptance checkbox is checked, not that
the tool has independently established completion or authorization to close it.

Issue IDs and creation timestamps remain stable across title edits and moves.
When changing open/closed state, move the file to the configured directory,
advance `updated`, and append a dated note. Keep existing note records unchanged.
Interpret historical lint failures before changing data: a base ahead of the
checkout cannot establish append-only compliance against those newer commits.

The package includes the complete runtime and uses Python 3.10 or newer with no
third-party runtime dependencies. Git is used for history and watched-path checks.
