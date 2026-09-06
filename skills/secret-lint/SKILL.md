---
name: secret-lint
description: Audit repository or document content for exposed credentials, including API keys, passwords, authorization headers, URL credentials, private keys, and JWTs. Use for an explicit secret scan, before publishing repository changes, after editing configuration examples, or after copying external output into a repository. Report locations and categories without reproducing secret values.
---

# Secret lint

Establish the repository or file set being checked. For a repository audit,
inspect the working files, including hidden and untracked content; do not silently
exclude a directory because it contains logs, tests, examples, or source records.
Keep Git object storage out of a working-tree scan. History is a separate scope
and must not be described as checked unless it was actually inspected.

Use this package's `scripts/check PATH... --json`, or the installed `secret-lint
check` command, for local UTF-8 working files. The package includes its detector,
CLI and redaction API; it requires no private repository or configuration.
Read [commands and limits](references/commands.md) when scanning repositories,
writing a report or integrating redaction into an archive pipeline.

The scanner includes hidden and untracked files, omits Git metadata, and reports
candidate locations and categories without matched values. Binary, unreadable,
non-UTF-8 or untraversed symlink inputs make the result incomplete. Exit codes
are 0 for no candidates in a complete scan, 1 for candidates, and 2 for an
incomplete scan or operation failure. History is not scanned by this command;
use an appropriate history scanner when that scope is requested. Never paste
unredacted matching lines or credentials into chat, logs or external tools.

Cover these categories; matching a known prefix alone is not a complete scan:

| Category | Candidate indicators |
|---|---|
| Provider keys | Credential-shaped values with known prefixes such as `sk-`, `gsk-`, `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `glpat-`, `xoxb-`, `xoxp-`, `xoxa-`, `AKIA`, `AIza`, or `ya29.` |
| Authorization | Bearer credentials, authorization headers, session cookies, or equivalent authentication fields |
| URL credentials | A username and password embedded before a URL's host |
| Configuration values | Non-placeholder values assigned to password, token, API key, secret, signing-key, or private-key fields |
| Private keys | PEM private-key blocks, including RSA, DSA, EC, and OpenSSH formats |
| Structured tokens | JWT-shaped three-part tokens and similar encoded credentials |

Distinguish a candidate from a confirmed credential. Obvious placeholders,
deliberately truncated examples, environment-variable references, public keys,
certificates, fingerprints, and non-secret identifiers are not credentials.
Do not assume a value is synthetic merely because it appears in a test or sample.
Review ambiguous cases without exposing their values or testing them against a
live service. A waiver comment does not make a real credential safe.

If a real credential was exposed, report its location and provider/category,
stop the publication that would expose it further, and identify the owner action
needed to revoke or rotate it. Carry out credential changes only within existing
authorization. Replace the value with an obvious placeholder or a reference to
private credential storage where edits are authorized. Deleting the current line
does not revoke a credential or remove it from history. History rewriting requires
its own coordination and authorization; do not perform it as an automatic scan
step. Preserve immutable source records under the repository's rules and escalate
the specific exposure instead of silently rewriting them.

Report the exact scope, scanner/search method, inspected revision when relevant,
candidate locations and their disposition, unreadable or unsupported content,
and any remaining exposure. A partial scan or a tool failure is not a clean
result. State the result at the strength supported by the checks, not a guarantee
that arbitrary credentials cannot exist. Rerun affected checks after authorized
repairs. Keep secret values out of the report throughout.

An authorized transformation can use `scripts/redact --tier hard,ctx` with UTF-8
stdin, or `scripts/mask PATH` to create a `.masked` copy. The stdin command emits
only transformed text; `--json` explicitly includes the replacement count.
`mask --in-place` replaces the selected source and must stay within the user's
edit scope. Redaction keeps a short prefix/suffix for non-key-block values and
is not a substitute for revocation or a guarantee that arbitrary secrets are
recognized. Private-key block bodies are fully masked.

Development: `uv run --locked pytest tests -q`, `uv run --locked ruff check .`,
and `uv run --locked skills-ref validate .` (the format checker needs Python 3.11
or newer; the runtime supports Python 3.10 or newer).
