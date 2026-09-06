---
name: structure-lint
description: Check that a repository's documented layout, content metadata, source references, and discovery links agree with its current files and rules. Use after moves or structural edits, before a required repository check, or for a vault-consistency audit. Apply the repository's own schema and source-immutability rules rather than assuming a fixed directory convention.
---

# Repository structure check

Read the current repository contract from disk and identify the files affected
by the requested change. The contract is the authority for directory roles,
required metadata, immutable sources, exclusions, indexes, and required checks.
Do not copy another repository's folder names or frontmatter schema into it.

Read optional local instructions from `STRUCTURE_LINT_PROFILE` or
`${XDG_CONFIG_HOME:-$HOME/.config}/structure-lint/profile.md` when present.
A profile can locate an existing checker and explain local conventions; current
user instructions and the actual repository contract take precedence.

Use the repository's maintained structure checker when available. Run it from
an authorized task worktree if it creates output or the repository requires
worktree isolation. Record the baseline before edits when needed to distinguish
new failures from existing warnings. Inspect the actual exit status and full
summary; a command being present or starting successfully is not a passing check.

Check the parts relevant to the change:

- Documented directory roles match actual maintained directories and files.
- Maintained documents have the metadata required by their content class.
- Source references and local links still resolve after moves or renames.
- Pages remain discoverable through the indexes or entry points required by
  that repository; apply its documented exceptions.
- Skill instructions and tool references name existing, intended resources.

If no checker covers a required part, inspect that part directly and describe
the limitation. Use an available local-link checker for link resolution when
appropriate; it does not establish semantic correctness or source provenance.
Do not invent an automatic content classifier to infer which text an LLM wrote.
A heading such as "Summary" in an original document is not evidence that the
source was synthesized.

Repair the smallest cause within the requested scope. Update references when
a linked file moves. Respect immutable-source rules: do not move, summarize, or
rewrite raw records solely to satisfy a heuristic warning. Do not create empty
directories merely to silence stale documentation. Prefer correcting an obsolete
reference or removing an unnecessary duplicate rule.

Keep durable documentation focused on rules and query commands. Compute changing
counts and current status from their authoritative sources rather than saving
another inventory that will become stale.

After edits, rerun the affected checks and any checks required by the contract.
Report the command, inspected revision or worktree, result location, error and
warning totals with their baseline, unresolved findings, and what was not checked.
Do not describe a partial check as complete repository consistency.

This Skill is a procedure for using the repository's checks, not a bundled
schema or detection engine. Development format validation is
`uv run --project /path/to/structure-lint --locked skills-ref validate /path/to/structure-lint`.
