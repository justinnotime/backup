# Scanner and redaction commands

Use this package alone: `uv sync --locked`, then `uv run secret-lint --help`.
The standalone shell entries select `SECRET_LINT_PYTHON`, defaulting to `python3`.
The runtime uses only the Python standard library and makes no network or model
requests. Git and other Skill packages are not needed.

```bash
scripts/check /path/to/repository --json
scripts/check file.py config.yaml .env --output /private/review.json
scripts/mask document.md --tier hard,ctx
scripts/redact --tier hard,ctx < input.md > redacted.md
```

`check` and its `report` alias inspect all selected UTF-8 text, including hidden
and untracked files, regardless of extension or ignore files. They skip Git
metadata. A symlink is not followed, and binary/non-UTF-8 files, missing inputs,
special files and read errors are listed under `incomplete`; they cause exit 2.
Directories that cannot be read also make the scan incomplete. Explicit duplicate
files are scanned once. There is no implicit scan of Git history or decoded
binary attachments.

The JSON report has `schema: secret-lint-report/v1`, `scanned_files`, `findings`,
`incomplete` and `scope`. Findings contain only `file`, `line`, `category` and
`tier`. There are no matched values, excerpts, raw-value reports or credential
hashes. Credential-shaped values in filenames are masked when displaying paths.
Reports are written only when `--output` is explicit, using mode 0600. Keep
reports private because ordinary filenames may reveal personal context.

The three selectable detector tiers are:

| Tier | Meaning |
|---|---|
| `hard` | Known provider formats, credential fields/headers, URL credentials, private-key blocks and related shapes |
| `ctx` | High-entropy strings in credential-related context |
| `heur` | Bare high-entropy strings without that context |

All three are selected by default; `--tier hard,ctx` omits the noisier bare
entropy tier. An unknown or empty tier selection is an error. These are
candidate detectors, not proof of an active credential. Public certificates,
encoded blobs, examples and sufficiently random identifiers need contextual
review. Conversely, short or unfamiliar credentials may be missed. Exact
provider detectors still inspect logs and URLs even where generic entropy
checks avoid false positives. Inline image data is excluded without skipping
other credentials on the same line.

`mask` preserves the original source unless `--in-place` is explicit. Default
output is `filename.ext.masked` with mode 0600. In-place output preserves the
source permission bits. Writes replace one file atomically. Directory masking
selects Markdown files, preserving the document-conversion use case; individual
non-Markdown UTF-8 files can be explicitly supplied. Files with no changes are
not written unless `--write-unchanged` is present. Masking never follows symlinks.

Non-block values keep up to four characters at each end, replacing the middle
with stars and preserving length. Private-key bodies are replaced completely,
preserving delimiters and line structure. Once a value is detected, all its
occurrences within that document are masked. Repeating the transformation does
not change already masked text. Redaction is mechanical; it does not authenticate
a value, revoke a key, remove Git history or decide whether source edits are
permitted.

## External pipeline integration

Use an explicitly configured argument array for `scripts/redact --tier hard,ctx`.
Send the original UTF-8 document through stdin, never argv. A successful command
returns redacted UTF-8 through stdout with no status preamble. `--json` instead
returns `{"text": "...", "replacements": 0}`. Stderr contains diagnostic labels,
not input text. Nonzero exit means the caller must not publish the original
unredacted input as a fallback.

For Python integrations, install the package and import `secret_lint`.
`mask_text(text, tiers)` returns `(masked_text, replacement_count)`.
`scan_text(text)` retains low-level `(line, category, tier, value, target)`
findings for internal transformation; **do not log or serialize these tuples**.
Use `inspect_inputs(paths, tiers)` for a safe structured report. Public packages
must use this installed API or the executable contract, rather than locating
another Skill's source directory.
