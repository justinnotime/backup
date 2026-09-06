"""Check repository structure using explicit, caller-owned rules."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    level: str
    message: str
    check: str
    path: str = ""


def frontmatter(text: str) -> tuple[str | None, dict[str, str]]:
    """Read top-level field lines; this is not a general YAML schema validator."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, {}
    end = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), len(lines))
    body = "\n".join(lines[1:end])
    fields = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            fields.setdefault(match[1], match[2].strip())
    return body, fields


def section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        return None
    depth = len(heading) - len(heading.lstrip("#"))
    result = []
    for line in lines[start + 1 :]:
        if re.match(r"^#{1," + str(depth) + r"}\s", line):
            break
        result.append(line)
    return "\n".join(result)


class Checker:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("repository root is not a directory")
        self.findings: list[Finding] = []

    def report(self, rule: dict, message: str, path: Path | str = "", level: str | None = None):
        severity = (level or rule.get("severity", "error")).upper()
        if severity not in {"ERROR", "WARN"}:
            raise ValueError("severity must be error or warn")
        relative = str(path.relative_to(self.root)) if isinstance(path, Path) else path
        self.findings.append(Finding(severity, message, rule.get("id", rule["type"]), relative))

    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def path(self, value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("configured repository paths must be relative and cannot contain '..'")
        return self.root / candidate

    def select(self, rule: dict, key: str = "include", *, directories: bool = False) -> list[Path]:
        patterns = rule.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{key} must be a list of relative glob patterns")
        paths = set()
        for pattern in patterns:
            self.path(pattern)
            paths.update(self.root.glob(pattern))
        selected = []
        for path in sorted(paths):
            relative = path.relative_to(self.root).as_posix()
            if any(fnmatch.fnmatchcase(relative, p) for p in rule.get("exclude", [])):
                continue
            if any(re.search(p, relative) for p in rule.get("exclude_regex", [])):
                continue
            if path.is_dir() if directories else path.is_file():
                selected.append(path)
        return selected

    def layout(self, rule: dict):
        document = self.path(rule["document"])
        text = self.read(document)
        body = section(text, rule["section"])
        if body is None:
            self.report(rule, f"{rule['document']} — missing section {rule['section']!r}", document)
            return
        pattern = rule.get("pattern", r"`([A-Za-z][A-Za-z0-9._-]*/)`")
        declared = set(re.findall(pattern, body))
        if not declared:
            self.report(
                rule, f"{rule['document']} — directory table has no parseable entries", document
            )
        for name in sorted(declared):
            if not self.path(name).is_dir():
                self.report(
                    rule, f"{rule['document']} — declared directory {name!r} does not exist", name
                )
        if rule.get("undocumented", True):
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or path.name in rule.get("ignore_directories", []):
                    continue
                if path.name.startswith(".") and not rule.get("include_hidden", False):
                    continue
                if f"`{path.name}/`" not in text:
                    self.report(
                        rule,
                        f"Directory '{path.name}/' exists but is not documented in {rule['document']}",
                        path,
                        rule.get("undocumented_severity", "warn"),
                    )

    def forbidden_text(self, rule: dict):
        pattern = re.compile(rule["pattern"], re.IGNORECASE if rule.get("ignore_case") else 0)
        skip = (
            re.compile(rule["skip_first_line_pattern"])
            if rule.get("skip_first_line_pattern")
            else None
        )
        for path in self.select(rule):
            with path.open(encoding="utf-8") as stream:
                for i, line in enumerate(stream):
                    if i == 0 and skip and skip.search(line):
                        break
                    if rule.get("first_lines") is not None and i >= rule["first_lines"]:
                        break
                    if pattern.search(line):
                        self.report(
                            rule,
                            f"{path.relative_to(self.root)} — {rule.get('message', 'matches a prohibited text pattern')}: {line.strip()}",
                            path,
                        )
                        break

    def forbidden_paths(self, rule: dict):
        for path in self.select(rule):
            self.report(
                rule,
                f"{path.relative_to(self.root)} — {rule.get('message', 'file is in a prohibited location')}",
                path,
            )

    def metadata(self, rule: dict):
        for path in self.select(rule):
            relative = path.relative_to(self.root)
            body, fields = frontmatter(self.read(path))
            if body is None:
                self.report(
                    rule, f"{relative} — missing YAML frontmatter (first line is not '---')", path
                )
                continue
            if not body.strip() and rule.get("empty_frontmatter", True):
                self.report(rule, f"{relative} — empty YAML frontmatter", path)
                continue
            for field in rule.get("fields", []):
                if field not in fields or (rule.get("nonempty") and not fields[field]):
                    self.report(
                        rule, f"{relative} — frontmatter missing required field: {field}", path
                    )
            for field, allowed in rule.get("values", {}).items():
                if fields.get(field) and fields[field] not in allowed:
                    self.report(
                        rule,
                        f"{relative} — {field} {fields[field]!r} not in allowed values {allowed}",
                        path,
                    )

    def source_references(self, rule: dict):
        for path in self.select(rule):
            body, _ = frontmatter(self.read(path))
            active = False
            for line in (body or "").splitlines():
                if line.startswith(rule.get("field", "sources") + ":"):
                    active = True
                    continue
                if line and not line.startswith(" "):
                    active = False
                if not active or not re.match(r"^\s*-\s+", line):
                    continue
                value = re.sub(r"^\s*-\s+", "", line).strip()
                if rule.get("strip_annotations", False):
                    value = re.split(r"\s*[#(]", value, maxsplit=1)[0].strip()
                value = value.strip("\"'")
                if rule.get("prefixes") and not any(value.startswith(p) for p in rule["prefixes"]):
                    continue
                target = self.path(value)
                if not target.exists() and not (
                    rule.get("allow_parent", False) and target.parent.is_dir()
                ):
                    self.report(
                        rule,
                        f"{path.relative_to(self.root)} — source reference {value!r} not found",
                        path,
                    )

    def inline_paths(self, rule: dict):
        for path in self.select(rule):
            for value in sorted(set(re.findall(rule["pattern"], self.read(path)))):
                if value.rstrip("/").endswith(tuple(rule.get("ignore_suffixes", []))):
                    continue
                if not self.path(value).exists():
                    self.report(
                        rule,
                        f"{path.relative_to(self.root)} — references {value!r} which does not exist",
                        path,
                    )

    def navigation(self, rule: dict):
        if rule.get("when_exists") and not self.path(rule["when_exists"]).exists():
            return
        corpus = "\n".join(
            self.read(path) for path in self.select({"include": rule["indexes"]})
        ).casefold()
        for path in self.select(rule):
            if path.stem.casefold() not in corpus:
                self.report(
                    rule,
                    f"{path.relative_to(self.root)} — not reachable from configured navigation",
                    path,
                )

    def taxonomy(self, rule: dict):
        field = rule.get("field", "kind")
        field_pattern = re.compile(
            r"^\s*-\s+\*\*" + re.escape(field) + r":\*\*\s*(.*?)\s*$", re.MULTILINE
        )
        for provenance in self.select(rule):
            body = section(self.read(provenance), rule["section"])
            if body is None:
                continue
            allowed = re.findall(rule.get("pattern", r"(?m)^\|\s*`([a-z-]+)`"), body)
            if not allowed:
                self.report(
                    rule,
                    f"{provenance.relative_to(self.root)} — taxonomy section has no parseable values",
                    provenance,
                    rule.get("missing_severity", "warn"),
                )
                continue
            for path in sorted(provenance.parent.glob(rule.get("members", "*.md"))):
                if not path.is_file() or path.name in rule.get("exclude_names", []):
                    continue
                text = self.read(path)
                match = field_pattern.search(text)
                if not match:
                    self.report(
                        rule,
                        f"{path.relative_to(self.root)} — missing '- **{field}:**' bullet (allowed values in {provenance.relative_to(self.root)})",
                        path,
                        rule.get("missing_severity", "warn"),
                    )
                    continue
                value = match[1]
                if value not in allowed:
                    self.report(
                        rule,
                        f"{path.relative_to(self.root)} — {field} {value!r} not in allowed values {allowed}",
                        path,
                    )
                for extra in rule.get("required_when", []):
                    directory = path.parent.relative_to(self.root).as_posix()
                    if value == extra["value"] and fnmatch.fnmatchcase(
                        directory, extra.get("directory", "*")
                    ):
                        for name in extra["fields"]:
                            if not re.search(
                                r"(?m)^\s*-\s+\*\*" + re.escape(name) + r":\*\*", text
                            ):
                                self.report(
                                    rule,
                                    f"{path.relative_to(self.root)} — {field}={value} missing '- **{name}:**' bullet",
                                    path,
                                )

    def required_files(self, rule: dict):
        for directory in self.select(rule, directories=True):
            for name in rule["files"]:
                self.path(name)
                if not (directory / name).is_file():
                    self.report(
                        rule, f"{directory.relative_to(self.root)}/ — missing {name}", directory
                    )

    def external(self, rule: dict):
        argv = rule["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ValueError("external argv must be a nonempty string array")
        argv = [v.replace("@root@", str(self.root)) for v in argv]
        result = subprocess.run(
            argv,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=rule.get("timeout", 120),
        )
        errors_before = sum(f.level == "ERROR" for f in self.findings)
        for line in result.stdout.splitlines():
            if not line:
                continue
            level, separator, message = line.partition("\t")
            if separator and level in {"ERROR", "WARN"}:
                self.report(rule, message, level=level)
            else:
                self.report(rule, f"external checker emitted invalid TSV: {line}", level="ERROR")
        if result.stderr.strip():
            self.report(
                rule,
                f"external checker wrote stderr: {result.stderr.strip().splitlines()[-1]}",
                level="ERROR",
            )
        if result.returncode and errors_before == sum(f.level == "ERROR" for f in self.findings):
            self.report(
                rule,
                f"external checker exited {result.returncode} without a structured error",
                level="ERROR",
            )

    def git_freshness(self, rule: dict):
        if rule.get("skip_environment") and os.environ.get(rule["skip_environment"]) == "1":
            return
        remote, branch = rule.get("remote", "origin"), rule.get("branch", "main")
        if remote.startswith("-") or branch.startswith("-"):
            raise ValueError("remote and branch cannot begin with '-'")

        def git(*args):
            command = ["git", "-C", str(self.root), *args]
            try:
                return subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=rule.get("timeout", 30),
                )
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(command, 124, "", "")

        if git("remote", "get-url", remote).returncode:
            return
        if (
            rule.get("fetch", False)
            and git("fetch", "--quiet", "--no-tags", remote, branch).returncode
        ):
            return
        result = git("rev-list", "--count", f"HEAD..{remote}/{branch}")
        if result.returncode == 0 and int(result.stdout.strip()) > 0:
            self.report(
                rule, f"local HEAD is {result.stdout.strip()} commit(s) behind {remote}/{branch}"
            )

    def run(self, config: dict) -> list[Finding]:
        if config.get("schema") != "structure-lint/v1" or not isinstance(
            config.get("checks"), list
        ):
            raise ValueError("configuration requires schema=structure-lint/v1 and a checks array")
        if not config["checks"]:
            raise ValueError("configuration has no checks")
        types = {
            "layout",
            "forbidden_text",
            "forbidden_paths",
            "metadata",
            "source_references",
            "inline_paths",
            "navigation",
            "taxonomy",
            "required_files",
            "external",
            "git_freshness",
        }
        for rule in config["checks"]:
            if not isinstance(rule, dict) or rule.get("type") not in types:
                raise ValueError("unknown structure check type")
            getattr(self, rule["type"])(rule)
        return self.findings


def render(findings: list[Finding], output_format: str, root: Path):
    errors = sum(f.level == "ERROR" for f in findings)
    warnings = sum(f.level == "WARN" for f in findings)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "root": str(root),
                    "errors": errors,
                    "warnings": warnings,
                    "findings": [asdict(f) for f in findings],
                },
                ensure_ascii=False,
            )
        )
    elif output_format == "tsv":
        for finding in findings:
            print(f"{finding.level}\t{finding.message.replace(chr(10), ' ').replace(chr(9), ' ')}")
    else:
        print(f"=== Structure Lint: {root} ===")
        for finding in findings:
            print(f"[{finding.level}] {finding.message}")
        print(f"=== Summary: {errors} errors, {warnings} warnings ===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=os.environ.get("STRUCTURE_LINT_CONFIG"))
    parser.add_argument("--format", choices=["text", "json", "tsv"], default="text")
    args = parser.parse_args(argv)
    findings = []
    exit_code = 0
    try:
        if args.config is None:
            raise ValueError("an explicit --config or STRUCTURE_LINT_CONFIG is required")
        config: Any = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise TypeError("configuration must be an object")
        findings = Checker(args.root).run(config)
        exit_code = int(any(f.level == "ERROR" for f in findings))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        findings.append(Finding("ERROR", f"structure check could not complete: {exc}", "runtime"))
        exit_code = 2
    render(findings, args.format, args.root.resolve())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
