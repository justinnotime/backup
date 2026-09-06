# Authorization and document inspection

These commands live in the same package as the publisher and mirror. Install
the locked dependencies first. All accounts, credential files and documents are
explicit inputs; installation performs no login or Google request.

## Obtain credentials

```bash
scripts/auth --config /private/documents.json /private/oauth-client.json --scopes drive
scripts/auth --config /private/documents.json --client-from-token /private/read.json \
  --scopes docs-create --output /private/write.json --manual
```

Read groups are `drive`, `chat` and `people`; write groups are `docs-create`,
`docs-write` and `drive-share`. Read and write groups cannot share a credential.
Choose only the required groups. The existing default group selection is
`drive,chat,people`; use `--scopes drive` for a Docs-only reader.
Read output defaults to `read_token_file`; write output defaults to
`write_token_file`. Explicit `--output` is also supported. The two paths must
be distinct. Existing files require `--replace`; review the intended credential
before replacing it. Storage is atomic with mode 0600.

Consent requires the user's browser and uses a loopback callback with state and
PKCE checks. `--port` selects an explicit local port. In `--manual` mode, paste
the complete redirected URL so the helper can validate its state; a bare code
is insufficient. Do not paste client secrets, refresh tokens or consent URLs
into a public conversation. The helper does not revoke grants or change sharing.

## Render pages

```bash
scripts/render --config /private/documents.json example-document \
  --pages 1-3 --dpi 110 --out /private/inspection
```

Rendering exports PDF with the selected write credential and converts it with
`pdftoppm` from Poppler. `--token` selects an explicit alternative;
`render.pdftoppm_command` in configuration or `--pdftoppm-command` selects a JSON
argv prefix. Only successfully generated PDF/page outputs replace earlier owned
files. Failed conversion cannot report old PNGs as fresh output. No document is
modified. PDF pages are paginated and cannot prove a pageless document's width.

## Compare exports

```bash
scripts/compare-exports --config /private/documents.json --limit 3
scripts/compare-exports --config /private/documents.json \
  --keep /private/export-review --json /private/export-review/result.json
```

Comparison uses the read credential, native Markdown, HTML/pandoc and existing
mirror manifests to check tab coverage, image references and the package's
stable text fingerprint. It does not write Google or the archive. By default,
reports contain measurements and divergence positions rather than document
text; `--include-content` deliberately adds excerpts. Retained exports and such
reports are private document content. `--mirror-directory`, `--token` and
`--pandoc-command` override explicit inputs. Missing, malformed or failed exports
produce a nonzero result instead of an incomplete success.

Text fingerprints do not prove formatting, images or every semantic distinction
are equal. Review the actual outputs when those properties matter.
