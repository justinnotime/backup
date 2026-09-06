---
name: realign
description: Compare recent recorded work with the user's main objective and adjust priorities when progress has drifted. Use for an explicit priority audit, repeated status reports without movement on the essential work, prolonged coordination without delivery, a configured review interval, or a change that invalidates the current approach.
---

# Realign work with the objective

Identify the user's desired outcome in their own words and the smallest chain
of results still required to achieve it. Use the current request and authoritative
task records; do not substitute a convenient side project for the actual goal.
If the goal is unclear, recover the user's instructions before proposing a change.

Use the user's requested review window. Otherwise, read `$REALIGN_PROFILE` when
set, or `${XDG_CONFIG_HOME:-$HOME/.config}/realign/profile.md` when present, for
optional private reporting and prioritization preferences. A day of recent work
is a useful default when no window is specified. Current instructions take
precedence over the profile. Disclose an unreadable explicitly selected profile.

Inspect actual evidence: commits and diffs, task updates, pull requests, test
results, and available activity records. Separate evidence of a deliverable
from records merely describing intended work. Summarize activity by its effect:

| Contribution | Meaning |
|---|---|
| Essential result | Directly advances a result required for the main objective |
| Supporting work | Resolves a dependency, defect, or evidence gap for that result |
| Lower-priority result | Useful work that does not advance the current objective |
| Coordination and reporting | Messages, tracking, routing, and progress reports |

Use recorded durations when available. If the records do not establish time
spent, state that limit; commit or message counts are not hours, and speculative
percentages must not be presented as measured allocation. Avoid attributing
another person's work to this session.

Explain any mismatch between activity and the remaining objective. Common
causes include treating every incoming message as a new task, polishing process
documents while an implementation remains untouched, duplicating existing work,
or accumulating prerequisites that could be removed. Coordination can be necessary;
judge it by the dependency it resolves rather than declaring all coordination waste.

Make the adjustment concrete: identify the essential result to advance now,
what supporting work it needs, what lower-priority work can wait for a named
event, and what would justify interrupting the focus. Prefer deleting an
unnecessary prerequisite to inventing another mechanism. Preserve the user's
authorized scope and commitments; a priority audit does not authorize canceling
other tasks, changing external schedules, or assigning work to people.

Report the most important unfinished result first, the activity evidence, the
reason for any mismatch, and the next observable deliverable. Then take the next
authorized action that advances the objective. If an external dependency blocks
it, state the dependency and advance useful independent work. Keep the audit
proportional so it does not become more coordination without delivery.

This is an instruction-only Skill. Development format validation:
`uv run --project /path/to/realign --locked skills-ref validate /path/to/realign`.
