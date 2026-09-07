---
name: prompt-translation
description: Translate timestamped Chinese prompt archives into English learning pairs, validate their source records, and derive an optional expression cheatsheet. Includes incremental daily ledgers and configured scheduled publication; uses caller-selected models, prompts, credentials and repository paths.
---

# Prompt translation

This package includes the translator, provenance validator and scheduled
entrypoint. Translation and cheatsheet generation make **LLM calls > 0**.
Accounts, model names, system prompts and publication policy are selected by
private configuration. It does not discover credentials in other applications.

Read [configuration](references/configuration.md) when setting up or moving a
profile. Install the package dependencies into a caller-selected Python
environment; set `PROMPT_TRANSLATION_PYTHON` for the executable wrappers.

Start with local checks and a cost estimate:

```bash
scripts/translate --config /private/translation.json --doctor
scripts/translate --config /private/translation.json --dry-run
```

Both commands make no network calls and write no output. `--doctor` reads only
the selected local credential and verifies required dependencies. `--dry-run`
does not read credentials. Estimate the full archive without date or file caps;
see [costs and recovery](references/costs-and-recovery.md) before a paid run.

Use complete UTC dates for incremental daily output:

```bash
scripts/translate --config /private/translation.json --root /work/task \
  --since-date 2025-01-01 --through-date 2025-01-07 --oldest-first
scripts/validate --config /private/translation.json --root /work/task \
  /work/task/learning/pairs/2025-01-01.md
```

The original prompt heading format is `### YYYY-MM-DD HH:MM:SSZ`. Daily files
include every record in a ledger, including prompts filtered or classified as
trivial. Translated records retain the original timestamp, Chinese text and
source identity. Valid unchanged translations remain reusable after a cache
loss, source move or source file split. The source body hash determines when a
record needs translation again.

Other supported modes are `--date`, legacy `--days`, `--only`, `--limit-days`,
`--limit-files`, `--force`, `--no-classify`, `--cheatsheet` and
`--cheatsheet-only`. `--force` can spend money on already translated material;
use it when intentionally changing a translation policy. Do not alter selected
sources, model prompts or credential settings merely to make a failing run pass.

Before publishing, run strict validation. `--allow-source-ahead` is for an
asynchronous inspection where source capture may be newer than the translation;
it does not replace strict publication validation.
For a repository checker, `scripts/validate --scan-output --format tsv` selects
the configured output directory. An explicit `--legacy-source-only` inspection
can preserve older provenance references without weakening daily ledger checks;
see [validation options](references/configuration.md#validation-for-repository-checks).

For an existing schedule, `scripts/run --config /private/schedule.json` delegates
Git operations to explicitly configured external commands. Read
[scheduled operation](references/scheduled.md) before configuring that entrypoint.
A failure returns nonzero while completed day files remain available for the
caller to validate, publish or reuse. Scheduling and sending source text to a
configured model must remain within the user's authorized scope.
