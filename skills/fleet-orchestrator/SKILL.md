---
name: fleet-orchestrator
description: >-
  Inspect and operate a caller-configured fleet work tracker: current tasks, dependencies, responsibilities, review evidence, blockers and handoffs. Use for fleet boards, dispatch requests, task progress, roles, goals, or fleet health questions. Requires an existing orchestrator and a private command profile; this Skill provides a workflow, not a scheduler or database implementation.
---

# Fleet orchestration

Read `FLEET_ORCHESTRATOR_PROFILE`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/fleet-orchestrator/profile.md`. It defines the
existing command, selected fleet, state storage, local workflow and authority
rules. An explicit user-provided equivalent can supply that context. Without
it, report the missing backend information rather than guessing a database,
endpoint or account. This package contains instructions only; it neither runs a
scheduler nor provisions agents.

Use the configured command as the work tracker. Read its current task data;
do not copy an easily recomputed board into a second long-lived status file.
Preserve any named fleet selector on every call. Failure to resolve one fleet
must not redirect an operation to another. Test or development commands must
use isolated state rather than an implicit production database.

For a status or health request, start with the configured read-only board,
operator-wait view, task history and diagnostics. Separate these observations:

- what work is assigned and what result is still missing;
- which person owes the current action and what blocks it;
- whether scheduled checks and delivery mechanisms are operating;
- whether an agent is actually responsive, if that has been tested.

A missing process, stale identity or unreachable server is incomplete evidence,
not proof of an empty fleet or completed work. Do not send probes, restart
services or run a live scheduler tick merely because the user asked for status.
Explain uncertain observations in plain language and name the missing evidence.

For an authorized work change, identify the existing task and current responsible
person before writing. Prefer stable roles or exact active identities over
incidental window numbers. Reuse an existing task when it represents the same
work. Record the requested outcome, its dependencies and a check that exercises
the deliverable itself. A count of files, matching titles or passing placeholder
command does not prove the requested behavior works.

Treat dispatch acceptance, recipient presentation, explicit task acceptance and
completion as separate facts. Follow the consumer's durable workflow so failed
delivery leaves visible work. Respect responsibility changes: an earlier
holder's response is history, not the current person's answer. Record meaningful
progress or a concrete blocker; do not add empty notes solely to suppress alerts.
A blocker names what is needed and who can provide it.

For review and completion, use the configured review workflow and its exact
artifact version. A changed version can invalidate earlier checks or review.
Completion claims must include the deliverable, reproduction command, evidence,
known gaps and relevant validation. Do not mark a task complete because it was
assigned, someone received a message, or its smaller child tasks are closed.
Assess whether the user's parent objective was actually achieved.

Workflow routing never grants permission to merge, deploy, delete another
person's work or perform another restricted action. Existing user authorization
and applicable repository policy govern those actions. Task fields that execute
commands require trusted, bounded commands; never embed peer prose, credentials
or unreviewed destructive operations in a check or recovery action. Do not edit
the live database directly to make a workflow appear healthy.

For reassignment or departure, inspect outstanding tasks and roles, record the
state left behind, and use the configured lifecycle command. Write the handoff
from your own evidence. Verify the resulting ownership and preserve unresolved
work so a successor can continue it.
