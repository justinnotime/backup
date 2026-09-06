"""Export a Google document as PDF and render selected pages to PNG."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .auth import path_value, same_file, settings_for
from .oauth import OAuthError, open_request, refresh_access_token

EXIT_INPUT, EXIT_API, EXIT_TOOL = 2, 3, 4
WRITE_EXPORT_SCOPES = {
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
}


def doc_id(raw: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return raw
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "docs.google.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("document-id-or-google-docs-url-required")
    match = re.fullmatch(
        r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)(?:/[^\s]*)?", parsed.path
    )
    if not match:
        raise ValueError("document-id-or-google-docs-url-required")
    return match.group(1)


def command_argv(raw, default):
    command = json.loads(raw) if isinstance(raw, str) else (raw or default)
    if (
        not isinstance(command, list)
        or not command
        or any(
            not isinstance(item, str) or not item or "\0" in item for item in command
        )
    ):
        raise ValueError("tool-command-must-be-nonempty-argv")
    return [os.path.expandvars(os.path.expanduser(item)) for item in command]


def access_token(path: Path) -> str:
    return refresh_access_token(path, required_any_scopes=WRITE_EXPORT_SCOPES)


def export_pdf(doc: str, token: str, dest: Path) -> int:
    ident = doc_id(doc)
    url = f"https://www.googleapis.com/drive/v3/files/{ident}/export?mimeType=application%2Fpdf"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with open_request(request, timeout=180) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"pdf-export-http-{exc.code}") from None
    except OSError:
        raise OAuthError("pdf-export-unreachable") from None
    if not data.startswith(b"%PDF-"):
        raise OAuthError("pdf-export-invalid")
    dest.write_bytes(data)
    return len(data)


def page_range(value):
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", value.strip())
    if not match:
        raise ValueError("pages-must-be-N-or-N-M")
    first, last = int(match.group(1)), int(match.group(2) or match.group(1))
    if not 1 <= first <= last:
        raise ValueError("page-range-invalid")
    return first, last


def render_pages(doc, token_path, out, first, last, dpi, command):
    out.mkdir(parents=True, exist_ok=True)
    # All output discovery is confined to this run. A stale page can never be
    # mistaken for successful rasterization, and failures preserve prior output.
    with tempfile.TemporaryDirectory(prefix=".render-", dir=out) as temporary:
        stage = Path(temporary)
        pdf = stage / "doc.pdf"
        export_pdf(doc, access_token(token_path), pdf)
        try:
            result = subprocess.run(
                [
                    *command,
                    "-png",
                    "-r",
                    str(dpi),
                    "-f",
                    str(first),
                    "-l",
                    str(last),
                    str(pdf),
                    str(stage / "page"),
                ],
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("rasterizer-unavailable-or-timed-out") from None
        if result.returncode:
            raise RuntimeError("rasterizer-failed")
        pages = sorted(stage.glob("page-*.png"))
        if not pages:
            raise RuntimeError("rasterizer-produced-no-pages")
        numbers = []
        for page in pages:
            match = re.fullmatch(r"page-(\d+)\.png", page.name)
            if (
                not match
                or page.is_symlink()
                or not page.is_file()
                or not page.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                or not first <= int(match.group(1)) <= last
            ):
                raise RuntimeError("rasterizer-output-invalid")
            numbers.append(int(match.group(1)))
        if sorted(numbers) != list(range(first, max(numbers) + 1)):
            raise RuntimeError("rasterizer-output-pages-incomplete")
        old_pages = [
            p for p in out.glob("page-*.png") if re.fullmatch(r"page-\d+\.png", p.name)
        ]
        owned = [out / "doc.pdf", *old_pages, *(out / page.name for page in pages)]
        if any(p.is_symlink() or (p.exists() and not p.is_file()) for p in owned):
            raise ValueError("render-output-must-be-regular-files")
        os.replace(pdf, out / "doc.pdf")
        for page in pages:
            os.replace(page, out / page.name)
        current = {page.name for page in pages}
        for old in old_pages:
            if old.name not in current:
                old.unlink()
        return [out / page.name for page in pages]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "doc", help="document ID or https://docs.google.com/document/d/ID URL"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--token", type=Path, help="explicit write credential override")
    parser.add_argument("--pages", default="1-3")
    parser.add_argument("--dpi", type=int, default=110)
    parser.add_argument("--out", type=Path, default=Path("docshot"))
    parser.add_argument(
        "--pdftoppm-command", help="JSON argv prefix for the rasterizer"
    )
    args = parser.parse_args(argv)
    try:
        ident = doc_id(args.doc)
        first, last = page_range(args.pages)
        if not 1 <= args.dpi <= 2400:
            raise ValueError("dpi-out-of-range")
        settings = settings_for(args.config)
        token = args.token or settings.get("write_token_file")
        if not token:
            raise ValueError("write-token-file-required")
        command = command_argv(
            args.pdftoppm_command or settings.get("render", {}).get("pdftoppm_command"),
            ["pdftoppm"],
        )
        if not shutil.which(command[0]):
            raise RuntimeError("rasterizer-unavailable")
        out = path_value(args.out)
        if out.is_symlink():
            raise ValueError("render-output-directory-must-not-be-symlink")
        protected = [
            path_value(token),
            *[
                Path(settings[k])
                for k in ("read_token_file", "write_token_file")
                if settings.get(k)
            ],
        ]
        if args.config:
            protected.append(path_value(args.config))
        owned = [
            out / "doc.pdf",
            *out.glob("page-*.png"),
        ]
        if any(
            same_file(destination, source)
            for destination in owned
            for source in protected
        ) or any(
            source.resolve().parent == out.resolve()
            and re.fullmatch(r"(?:doc\.pdf|page-\d+\.png)", source.name)
            for source in protected
        ):
            raise ValueError("render-output-overlaps-protected-input")
        pages = render_pages(
            ident, path_value(token), out, first, last, args.dpi, command
        )
        print(f"OK rendered {len(pages)} pages from PDF export")
        for page in pages:
            print(page)
        return 0
    except OAuthError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_API
    except RuntimeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_TOOL
    except (OSError, ValueError, TypeError):
        print("FAIL render-configuration-or-output-invalid", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
