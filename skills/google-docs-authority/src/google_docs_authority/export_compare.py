"""Compare Markdown and HTML exports against a caller-selected document mirror."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from .auth import WRITE_SCOPES, path_value, same_file, settings_for
from .config import atomic_write
from .fingerprint import canonical as fingerprint
from .fingerprint import self_test
from .oauth import OAuthError, open_request, refresh_access_token
from .render import command_argv, doc_id

EXPORT = "https://www.googleapis.com/drive/v3/files/{}/export?mimeType={}"
EXIT_INPUT, EXIT_API = 2, 3


def access_token(path):
    return refresh_access_token(path, forbidden_scopes=WRITE_SCOPES)


def export(document_id, mime, token):
    ident = doc_id(document_id)
    if mime not in ("text/markdown", "text/html"):
        raise ValueError("export-mime-unsupported")
    request = urllib.request.Request(
        EXPORT.format(ident, urllib.parse.quote(mime, safe="")),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with open_request(request, timeout=300) as response:
            return response.read(), None
    except urllib.error.HTTPError as exc:
        return None, f"export-http-{exc.code}"
    except OSError:
        return None, "export-unreachable"


def first_divergence(a: str, b: str, window: int = 36, *, include_content=False) -> str:
    if a == b:
        return "identical"
    offset = next(
        (i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b))
    )
    result = f"diverges at char {offset}; native length {len(a)}, other length {len(b)}"
    if include_content:
        result += f"; native {a[offset : offset + window]!r} vs other {b[offset : offset + window]!r}"
    return result


def tab_titles(manifest: str) -> list[str]:
    value = yaml.safe_load(manifest)
    if not isinstance(value, dict):
        raise ValueError("manifest-must-be-object")
    tabs = value.get("tabs") or []
    if not isinstance(tabs, list) or any(not isinstance(tab, dict) for tab in tabs):
        raise ValueError("manifest-tabs-invalid")
    titles = [tab.get("title", "") for tab in tabs]
    if any(not isinstance(title, str) for title in titles):
        raise ValueError("manifest-tab-title-invalid")
    return [html.unescape(title) for title in titles]


def tab_coverage(md, titles):
    flat = re.sub(r"\s+", "", html.unescape(md))
    return sum(bool(t) and re.sub(r"\s+", "", t) in flat for t in titles), len(titles)


def measure(md):
    lines = md.splitlines()
    delimiter = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
    return {
        "bytes": len(md.encode("utf-8")),
        "images": len(
            re.findall(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])|<img\b", md, flags=re.I)
        ),
        "table_rules": sum(bool(delimiter.fullmatch(line)) for line in lines),
        "table_rows": sum(
            "|" in line and not delimiter.fullmatch(line) for line in lines
        ),
        "raw_html": len(re.findall(r"</?[A-Za-z][^>]*>", md)),
        "fp": fingerprint(md),
    }


def public_measure(measurement):
    if measurement is None:
        return None
    return {
        **{key: value for key, value in measurement.items() if key != "fp"},
        "fingerprint": "sha256:"
        + hashlib.sha256(measurement["fp"].encode()).hexdigest(),
    }


def read_manifest(directory, root):
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ValueError("mirror-directory-outside-root")
    manifest = directory / "manifest.yaml"
    readme = directory / "README.md"
    if manifest.is_symlink() or readme.is_symlink():
        raise ValueError("mirror-input-symlink-refused")
    text = manifest.read_text(encoding="utf-8")
    value = yaml.safe_load(text)
    if not isinstance(value, dict) or not isinstance(value.get("docId"), str):
        raise ValueError("manifest-document-id-required")
    ident = doc_id(value["docId"])
    if ident != value["docId"]:
        raise ValueError("manifest-document-id-required")
    titles = tab_titles(text)
    if value.get("layout") == "tabs":
        tabs = value.get("tabs")
        if not tabs:
            raise ValueError("manifest-tab-layout-empty")
        chunks, seen = [], set()
        for tab in tabs:
            relative = tab.get("path")
            if not isinstance(relative, str) or not relative:
                raise ValueError("manifest-tab-path-required")
            relative = Path(relative)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("manifest-tab-path-outside-document")
            path = directory / relative
            if path in seen or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("manifest-tab-path-invalid")
            if any(
                (directory / Path(*relative.parts[:n])).is_symlink()
                for n in range(1, len(relative.parts) + 1)
            ):
                raise ValueError("manifest-tab-symlink-refused")
            seen.add(path)
            chunks.append(
                re.sub(
                    r"\A<!--.*?-->\s*", "", path.read_text(encoding="utf-8"), flags=re.S
                )
            )
        committed = "\n".join(chunks)
    else:
        committed = readme.read_text(encoding="utf-8") if readme.exists() else ""
    return ident, titles, committed


def compare_directory(
    directory, root, token, command, *, keep=None, include_content=False
):
    ident, titles, committed = read_manifest(directory, root)
    native, native_error = export(ident, "text/markdown", token)
    raw_html, html_error = export(ident, "text/html", token)
    record = {
        "slug": directory.name,
        "docId": ident,
        "errors": {
            route: error
            for route, error in (("native", native_error), ("html", html_error))
            if error
        },
    }
    converted = None
    if raw_html is not None:
        try:
            process = subprocess.run(
                [*command, "--from=html", "--to=gfm", "--wrap=none"],
                input=raw_html,
                capture_output=True,
                timeout=300,
            )
            if process.returncode:
                record["errors"]["conversion"] = "html-converter-failed"
            else:
                converted = process.stdout.decode("utf-8")
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            record["errors"]["conversion"] = (
                "html-converter-output-unavailable-or-invalid"
            )
    try:
        native_md = native.decode("utf-8") if native is not None else None
    except UnicodeError:
        record["errors"]["native"] = "native-export-not-utf8"
        native_md = None
    measurements = [
        measure(md) if md is not None else None
        for md in (native_md, converted, committed or None)
    ]
    sn, sh, sc = measurements
    record.update(
        dict(
            zip(
                ("native", "html_route", "committed"), map(public_measure, measurements)
            )
        )
    )
    record.update(
        {
            "tabs_total": len(titles),
            "tabs_in_native": tab_coverage(native_md or "", titles)[0],
            "tabs_in_html": tab_coverage(converted or "", titles)[0],
            "fp_vs_repo": "no-repo"
            if sc is None
            else "unavailable"
            if sn is None
            else "same"
            if sn["fp"] == sc["fp"]
            else "differs",
            "fp_native_eq_html": sn["fp"] == sh["fp"]
            if sn is not None and sh is not None
            else None,
            "vs_repo": first_divergence(
                sn["fp"], sc["fp"], include_content=include_content
            )
            if sn is not None and sc is not None
            else "unavailable",
            "vs_html": first_divergence(
                sn["fp"], sh["fp"], include_content=include_content
            )
            if sn is not None and sh is not None
            else "unavailable",
        }
    )
    if keep is not None:
        for route, text in (("native", native_md), ("html-route", converted)):
            if text is not None:
                destination = keep / f"{directory.name}.{route}.md"
                if destination.is_symlink():
                    raise ValueError("saved-export-symlink-refused")
                atomic_write(destination, text)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--token", type=Path, help="explicit read credential override")
    parser.add_argument("--mirror-directory", type=Path)
    parser.add_argument("--pandoc-command", help="JSON argv prefix for HTML conversion")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--keep",
        type=Path,
        help="save content-bearing export copies outside the mirror",
    )
    parser.add_argument("--json", type=Path, help="write comparison metadata")
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="include document excerpts in divergence diagnostics",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        if args.limit is not None and args.limit < 1:
            raise ValueError("limit-must-be-positive")
        settings = settings_for(args.config)
        mirror = (
            args.mirror_directory
            or settings.get("registry", {}).get("mirror_directory")
            or settings.get("mirror", {}).get("output_directory")
        )
        if not mirror:
            raise ValueError("mirror-directory-required")
        root = path_value(mirror).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("mirror-directory-invalid")
        token_path = args.token or settings.get("read_token_file")
        if not token_path:
            raise ValueError("read-token-file-required")
        token_path = path_value(token_path)
        keep = path_value(args.keep) if args.keep else None
        report_path = path_value(args.json) if args.json else None
        protected = [
            token_path,
            *[
                Path(settings[k])
                for k in ("read_token_file", "write_token_file")
                if settings.get(k)
            ],
        ]
        if args.config:
            protected.append(path_value(args.config))
        for destination in (keep, report_path):
            if destination is not None and (
                destination.is_symlink()
                or destination.resolve().is_relative_to(root)
                or any(same_file(destination, item) for item in protected)
            ):
                raise ValueError("comparison-output-overlaps-protected-input")
        if keep is not None and any(
            item.resolve().is_relative_to(keep.resolve()) for item in protected
        ):
            raise ValueError("saved-exports-overlap-protected-input")
        command = command_argv(
            args.pandoc_command or settings.get("mirror", {}).get("pandoc_command"),
            ["pandoc"],
        )
        directories = sorted(path for path in root.iterdir() if path.is_dir())
        if args.limit:
            directories = directories[: args.limit]
        if not directories:
            raise ValueError("mirror-has-no-documents")
        token = access_token(token_path)
        records = []
        for directory in directories:
            try:
                record = compare_directory(
                    directory,
                    root,
                    token,
                    command,
                    keep=keep,
                    include_content=args.include_content,
                )
            except (OSError, ValueError, yaml.YAMLError):
                record = {
                    "slug": directory.name,
                    "errors": {"input": "mirror-input-or-export-output-invalid"},
                }
            records.append(record)
            print(
                f"{'FAIL' if record['errors'] else 'OK'} {directory.name}: native vs mirror {record.get('fp_vs_repo', 'unavailable')}"
            )
            if "vs_repo" in record:
                print("  " + record["vs_repo"])
        complete = [
            r
            for r in records
            if not r["errors"]
            and r.get("native") is not None
            and r.get("html_route") is not None
        ]
        failed = sum(bool(record["errors"]) for record in records)
        print(
            f"NOTE {len(complete)} complete comparisons; {failed} documents with export, conversion or input failures"
        )
        if report_path:
            atomic_write(
                report_path, json.dumps(records, indent=2, ensure_ascii=False) + "\n"
            )
        return EXIT_API if failed or not complete else 0
    except OAuthError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_API
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        print("FAIL comparison-configuration-or-output-invalid", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
