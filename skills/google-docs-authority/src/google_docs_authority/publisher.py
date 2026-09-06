"""Publish a caller-selected source while preserving explicit document authority."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import atomic_write, default_config, load
from .fingerprint import fingerprint

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DOCS = "https://docs.googleapis.com/v1/documents"
DRIVE = "https://www.googleapis.com/drive/v3/files"
DOC_MIME = "application/vnd.google-apps.document"
EXIT_INPUT, EXIT_API, EXIT_DRIFT = 2, 3, 5


def die(code: int, msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    raise SystemExit(code)


def access_token(path: Path) -> str:
    try:
        cfg = json.loads(path.read_text())
        fields = {
            name: cfg[name] for name in ("client_id", "client_secret", "refresh_token")
        }
        if any(not isinstance(value, str) or not value for value in fields.values()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        die(EXIT_API, "write-token-file-unreadable-or-invalid")
    body = urllib.parse.urlencode({**fields, "grant_type": "refresh_token"}).encode()
    try:
        with open_request(TOKEN_ENDPOINT, body, timeout=30) as response:
            tok = json.load(response)
        scopes = set(tok.get("scope", "").split())
        if not scopes.intersection(
            {
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive",
            }
        ):
            die(EXIT_API, "write-scope-required")
        token = tok.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError
        return token
    except urllib.error.HTTPError as exc:
        die(EXIT_API, f"token-refresh-http-{exc.code}")
    except (OSError, ValueError, TypeError, AttributeError):
        die(EXIT_API, "token-refresh-unreadable-or-invalid")


def call(
    url: str,
    token: str,
    payload=None,
    method="GET",
    raw: bytes | None = None,
    content_type="application/json",
):
    data = (
        raw
        if raw is not None
        else json.dumps(payload).encode()
        if payload is not None
        else None
    )
    request = urllib.request.Request(
        url,
        data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with open_request(request, timeout=180) as response:
            body = response.read()
            return (json.loads(body) if body.strip().startswith(b"{") else body, None)
    except urllib.error.HTTPError as exc:
        return None, f"http-{exc.code}"
    except (OSError, ValueError):
        return None, "response-unreadable-or-invalid"


def markdown_body(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    fm = re.match("^---\\n(.*?)\\n---\\n", text, flags=re.S)
    body = text[fm.end() :] if fm else text
    heading = re.search("^#\\s+(.+)$", body, flags=re.M)
    if heading:
        title = heading.group(1).strip().strip("*")
    elif fm and (m := re.search('^title:\\s*"?(.+?)"?\\s*$', fm.group(1), flags=re.M)):
        title = m.group(1)
    else:
        title = path.stem
    return (body, title)


def html_body(path: Path) -> str:
    if path.suffix.lower() in (".html", ".htm"):
        return path.read_text(encoding="utf-8")
    if not (pandoc := shutil.which("pandoc")):
        die(EXIT_INPUT, f"{path.suffix} needs pandoc to become HTML; not found")
    proc = subprocess.run(
        [pandoc, "--from", "gfm", "--to", "html", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        die(EXIT_INPUT, f"pandoc failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def multipart(meta: dict, body: str, mime: str) -> tuple[bytes, str]:
    boundary = f"gdocs-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}",
        "Content-Type: application/json; charset=UTF-8",
        "",
        json.dumps(meta),
        f"--{boundary}",
        f"Content-Type: {mime}; charset=UTF-8",
        "",
        body,
        f"--{boundary}--",
        "",
    ]
    return ("\r\n".join(parts).encode("utf-8"), boundary)


def live_markdown(doc_id: str, token: str) -> str | None:
    url = f"{DRIVE}/{urllib.parse.quote(doc_id, safe='')}/export?mimeType={urllib.parse.quote('text/markdown')}"
    out, err = call(url, token)
    if err:
        return None
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else None


def live_fingerprint(doc_id: str, token: str) -> str | None:
    text = live_markdown(doc_id, token)
    return fingerprint(text) if text is not None else None


def recorded_gdoc(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not match:
        return {}
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
        state = metadata.get("gdoc", {})
        if "gdoc" in metadata and (
            not re.search(r"(?m)^gdoc:[ \t]*(?:#[^\n]*)?(?:\n|$)", match.group(1))
            or len(re.findall(r"(?m)^gdoc:", match.group(1))) != 1
        ):
            raise ValueError
        if not isinstance(state, dict) or any(
            not isinstance(state[key], str)
            for key in ("id", "mode", "fingerprint")
            if key in state
        ):
            raise ValueError
    except (yaml.YAMLError, ValueError, AttributeError):
        die(EXIT_INPUT, "source-frontmatter-invalid")
    return state


def record_publication(
    path: Path,
    doc_id: str,
    live_fp: str,
    published_at: str,
    expected: bytes | None = None,
) -> None:
    text = path.read_text(encoding="utf-8")
    fm = re.match("\\A---\\n(.*?)\\n---\\n", text, flags=re.S)
    if not fm:
        die(
            EXIT_INPUT,
            f"cannot record publication state: {path} has no YAML frontmatter",
        )
    lines = fm.group(1).splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.fullmatch("gdoc:\\s*(?:#.*)?", line)),
        None,
    )
    values = {
        "id": doc_id,
        "mode": "published",
        "fingerprint": live_fp,
        "published_at": f'"{published_at}"',
    }
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["gdoc:"] + [f"  {key}: {value}" for key, value in values.items()])
    else:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0].isspace()):
            end += 1
        block = lines[start + 1 : end]
        indent = next(
            (
                re.match("\\s+", line).group(0)
                for line in block
                if line.strip() and re.match("\\s+", line)
            ),
            "  ",
        )
        found: set[str] = set()
        for i, line in enumerate(block):
            match = re.match(
                "^("
                + re.escape(indent)
                + ")(id|mode|fingerprint|published_at):(?:\\s.*)?$",
                line,
            )
            if match:
                key = match.group(2)
                block[i] = f"{match.group(1)}{key}: {values[key]}"
                found.add(key)
        block.extend(
            (
                f"{indent}{key}: {value}"
                for key, value in values.items()
                if key not in found
            )
        )
        lines[start + 1 : end] = block
    new_fm = "\n".join(lines)
    updated = f"---\n{new_fm}\n---\n" + text[fm.end() :]
    if expected is not None and path.read_bytes() != expected:
        die(EXIT_INPUT, "source-changed-during-publication; local state preserved")
    atomic_write(path, updated)


def set_pageless(doc_id: str, token: str) -> str:
    _, err = call(
        f"{DOCS}/{urllib.parse.quote(doc_id, safe='')}:batchUpdate",
        token,
        {
            "requests": [
                {
                    "updateDocumentStyle": {
                        "documentStyle": {
                            "documentFormat": {"documentMode": "PAGELESS"}
                        },
                        "fields": "documentFormat.documentMode",
                    }
                }
            ]
        },
        "POST",
    )
    return "pageless" if not err else f"pageless FAILED ({err})"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("source", type=Path)
    ap.add_argument("--config", default=default_config())
    ap.add_argument(
        "--update",
        metavar="DOC_ID",
        help="republish into this document instead of creating a new one",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="republish even when the live document has drifted",
    )
    ap.add_argument(
        "--pageless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the configured layout; public default is paged",
    )
    ap.add_argument("--folder", help="Drive folder id (create only)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--token", type=Path, help="explicit write-token file override")
    args = ap.parse_args(argv)
    settings = load(args.config)
    args.token = (
        args.token.expanduser() if args.token else settings.get("write_token_file")
    )
    if args.pageless is None:
        args.pageless = settings["pageless"]
    if args.force and not args.update:
        die(EXIT_INPUT, "force-requires-update")
    if args.folder and args.update:
        die(EXIT_INPUT, "folder-is-create-only")
    if not args.source.is_file():
        die(EXIT_INPUT, f"no such file: {args.source}")
    original_bytes = args.source.read_bytes()
    is_md = args.source.suffix.lower() in (".md", ".markdown")
    if is_md and (
        not re.match(
            "\\A---\\n.*?\\n---\\n", args.source.read_text(encoding="utf-8"), re.S
        )
    ):
        die(
            EXIT_INPUT,
            "Markdown sources need YAML frontmatter so publication state can be recorded",
        )
    if is_md:
        body, title = markdown_body(args.source)
        mime = "text/markdown"
    else:
        body, title = (html_body(args.source), args.source.stem)
        mime = "text/html"
    fp = fingerprint(args.source.read_text(encoding="utf-8"))
    state = recorded_gdoc(args.source) if is_md else {}
    if state.get("mode") and state["mode"] != "published":
        die(EXIT_INPUT, "source-is-not-authoritative-for-publication")
    if args.update:
        if state.get("id") and state["id"] != args.update:
            die(EXIT_INPUT, f"source is linked to {state['id']}, not {args.update}")
        if state.get("mode") and state["mode"] != "published":
            die(
                EXIT_INPUT,
                f"source mode is {state['mode']}; change authority to published explicitly before updating Google",
            )
    elif state.get("id"):
        die(
            EXIT_INPUT,
            f"source is already linked to {state['id']}; use --update to keep its URL",
        )
    if args.dry_run:
        print(
            f"OK would {('update ' + args.update if args.update else 'create')} {title!r} from {args.source} via {mime} ({len(body):,} chars)"
        )
        print(f"NOTE source fingerprint {fp}")
        print(f"NOTE token {args.token}")
        return 0
    if args.token is None:
        die(EXIT_INPUT, "write-token-file-required")
    token = access_token(args.token)
    if args.update:
        recorded = state.get("fingerprint")
        if not recorded and (not args.force):
            die(
                EXIT_INPUT,
                "the source has no recorded Google Docs fingerprint; use --force only after confirming which copy is authoritative",
            )
        live = live_fingerprint(args.update, token)
        if live is None:
            die(
                EXIT_API,
                "cannot export the live document, so it is unsafe to overwrite it",
            )
        if recorded and live != recorded and (not args.force):
            print(
                f"FAIL the live document has drifted from what we published.\n   recorded: {recorded}\n   live:     {live}\n   Someone edited it in Google. Decide: --force to overwrite their edits, or hand authority over to Google (see the configured authority workflow).",
                file=sys.stderr,
            )
            return EXIT_DRIFT
        payload, boundary = multipart({"name": title, "mimeType": DOC_MIME}, body, mime)
        url = f"{UPLOAD}/{urllib.parse.quote(args.update, safe='')}?uploadType=multipart&fields=id,name,webViewLink"
        out, err = call(
            url,
            token,
            raw=payload,
            method="PATCH",
            content_type=f"multipart/related; boundary={boundary}",
        )
    else:
        meta: dict = {"name": title, "mimeType": DOC_MIME}
        if args.folder:
            meta["parents"] = [args.folder]
        payload, boundary = multipart(meta, body, mime)
        url = f"{UPLOAD}?uploadType=multipart&fields=id,name,webViewLink"
        out, err = call(
            url,
            token,
            raw=payload,
            method="POST",
            content_type=f"multipart/related; boundary={boundary}",
        )
    if err:
        die(EXIT_API, f"Drive rejected the upload: {err}")
    if (
        not isinstance(out, dict)
        or not isinstance(out.get("id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", out["id"])
    ):
        die(
            EXIT_API,
            "upload-result-id-missing-or-invalid; do not retry creation blindly",
        )
    doc_id = out["id"]
    print(f"NOTE uploaded document {doc_id}; verify this ID before retrying")
    if args.update and doc_id != args.update:
        die(EXIT_API, "upload-returned-unexpected-document-id")
    notes = []
    if args.pageless:
        notes.append(set_pageless(doc_id, token))
    link = out.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"
    accepted_fp = live_fingerprint(doc_id, token)
    if accepted_fp is None:
        die(
            EXIT_API,
            "upload succeeded, but the live document could not be exported for verification; local publication state was not changed",
        )
    if accepted_fp != fp:
        die(
            EXIT_API,
            f"upload succeeded, but Google exported different content (source {fp}, live {accepted_fp}); local publication state was not changed",
        )
    published_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    if is_md:
        record_publication(
            args.source, doc_id, accepted_fp, published_at, expected=original_bytes
        )
    print(
        f"OK {('updated' if args.update else 'created')} {out.get('name')!r}"
        + (f" [{', '.join(notes)}]" if notes else "")
    )
    print(f"   {link}")
    if is_md:
        print(f"   recorded live fingerprint {accepted_fp} in {args.source}")
    else:
        print(
            f"   live fingerprint {accepted_fp} (non-Markdown source; record it separately)"
        )
    return 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect-refused", headers, fp
        )


def open_request(*args, **kwargs):
    return urllib.request.build_opener(NoRedirect()).open(*args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        die(EXIT_INPUT, "configuration-or-source-unreadable-or-invalid")


if __name__ == "__main__":
    raise SystemExit(main())
