---
name: pr-status-table
description: Report the current state of pull requests in a compact, self-contained table. Use when asked for PR status, review progress, which changes can merge, or a comparison of outstanding PRs, and when proactively reporting PR status. Derive checks, reviews, next actions, and waiting time from current platform data.
---

# Pull request status table

Report what each pull request changes, its actual state, and the next action
needed. Query the hosting platform during this request; previous messages and
remembered status are not a current source.

Apply a format supplied by the user. Otherwise, read `$PR_STATUS_TABLE_PROFILE`
when set, or `${XDG_CONFIG_HOME:-$HOME/.config}/pr-status-table/profile.md` when
present. This optional private profile can specify column names, language,
summary format, and local review policy. Current instructions take precedence.
If an explicitly selected profile cannot be read, disclose that limitation.

Without a specified format, use columns for pull request, change in plain
language, checks, review, next action, responsible party, and waiting time.
Link the PR identifier and include its repository when more than one repository
is involved. Each row must make sense without earlier conversation: explain the
concrete problem and resulting behavior, not just a branch name or internal code.

Query at least the PR state, draft flag, current head revision, check runs,
review decision, outstanding review discussions, and mergeability. A PR can be
closed while its detail endpoint still returns full review and check data.
Do not treat an unknown mergeability result as ready; retry a transient unknown
once and otherwise report the uncertainty.

Distinguish a required check that passed on the current revision from one that
is missing, pending, skipped, failed, or belongs to an older revision. A green
aggregate without the required jobs is insufficient. Distinguish review approval
from a clean CI result, and a new review request from an unanswered change request.
Apply the repository's current merge and review requirements before describing
a PR as ready. Status reporting itself does not authorize merging, closing,
publishing, or contacting reviewers.

Give the next concrete action and the person or service responsible. Derive
waiting time from an actual event for that action, such as a review request,
fix push, check start, or explicit decision request. Do not reuse the PR creation
time for every state or invent a start time. If the responsible party or time is
unknown, say so. Do not claim a reminder was sent without evidence of that action.

If including a summary of recently merged or closed PRs, compute it from the
platform's events within the stated time zone and reporting window. Do not copy
counts from an older report or save changing status into durable documentation.

This is an instruction-only Skill. Development format validation:
`uv run --project /path/to/pr-status-table --locked skills-ref validate /path/to/pr-status-table`.
