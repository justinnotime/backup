---
name: workspace-brief
description: Read a caller-configured local workspace briefing with a task queue, recent projects and documents, log and expected-output checks, Git worktree status, and inode availability. Use for startup context or local workspace inspection; does not fetch data, call a model, send messages, or repair reported problems.
---

# Workspace briefing

Run the complete reader with an explicit private configuration:

```bash
scripts/brief --config /private/workspace-brief.json
scripts/brief --config /private/workspace-brief.json --doctor
```

The normal command reads local files and prints context. A failed section emits
a short warning while the remaining sections continue. Normal configuration
errors return zero so an informational startup hook does not block a session.
`--doctor` checks configuration, the selected root and local executables without
running readers; a successful doctor is not a workspace health result.

Read [configuration.md](references/configuration.md) when selecting sources,
wording, expected publication dates or an external task-queue reader. The
[synthetic example](references/example.json) demonstrates the available sections.
Nothing discovers a personal repository, credentials, schedules or account.
The optional queue command must itself be a local read-only reader; configuring
it does not authorize network access or mutation. The package does not import
another Skill's implementation.

`--root` explicitly replaces the configured repository. `--project-dir` supplies
debug context only; it does not select extra sources. `--project-limit`,
`--marker-config` and `--debug` support existing caller conventions. Paths accept
`~`, environment variables, `@root@` and `$HOME`. `WORKSPACE_BRIEF_PYTHON` selects
a Python 3.10 or newer interpreter. Hook JSON on stdin is consumed through EOF;
its contents are not executed or used to discover additional data.

The separate `scripts/install-hook --config FILE [install|uninstall|check]`
command changes only explicitly selected SessionStart commands in the configured
settings file. Use it when installation or removal is requested. `check` is
read-only. Installation backs up changed settings, preserves unrelated hooks
and fields, and is idempotent. A briefing invocation never installs itself.

Run package-owned checks with `uv run --locked --group dev pytest -q` and
`uv run --locked --group dev ruff check src tests`. Tests use synthetic files,
temporary Git repositories and isolated settings; no account or model is needed.
