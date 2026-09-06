---
name: runtime-install
description: Preview or install explicitly configured Skill discovery links, private profile links and managed crontab blocks. Use for local runtime installation and migration; package selection, schedules and account setup commands belong to caller-owned configuration.
---

Use the bundled standalone commands with an explicit JSON configuration:

```sh
scripts/skills --config /private/links.json --dry-run
scripts/skills --config /private/links.json --print-sources
scripts/cron --config /private/cron.json --dry-run
```

Remove `--dry-run` only when installation is authorized. A preview reads the
current crontab and may run explicitly configured read-only checks; it creates
no links, locks, backups or directories and does not run `before_apply` commands.
A source listing validates the selected packages but creates no links.

Real installation validates all sources before changing discovery links,
preserves real entries and foreign profile links, and restores changed links
if a later link operation fails. Cron installation rereads under the configured
shared lock, preserves unrelated lines, backs up the old crontab, verifies the
installed bytes and restores the prior crontab on failure. All writers touching
the same crontab must use the same lock path.

This package does not discover accounts, choose packages or schedules, start
services, or run scheduled jobs. Optional `before_apply` commands are explicit
caller-owned installation prerequisites; their external effects are not rolled
back if crontab installation later fails.

Read [configuration.md](references/configuration.md) for the JSON fields and
synthetic examples. The package has no third-party runtime dependencies.
Development dependencies are in the default `dev` dependency group. From this
package directory, run the same checks as CI:

```sh
uv sync --locked --all-extras
uv run --no-sync pytest tests -q -rs
uv run --no-sync ruff check src tests
# Python 3.11 or later:
uv run --no-sync agentskills validate "$PWD"
```
