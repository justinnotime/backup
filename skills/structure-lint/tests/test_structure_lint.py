from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from structure_lint import Checker, frontmatter, main


def write(root, name, text=""):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def check(root, *rules):
    return Checker(root).run({"schema": "structure-lint/v1", "checks": list(rules)})


def test_layout_declarations_and_undocumented_directories(tmp_path):
    write(tmp_path, "POLICY.md", "## Layout\n| `Docs/` | documents |\n## Other\n`Tools/`\n")
    (tmp_path / "Extra").mkdir()
    rule = {"type": "layout", "document": "POLICY.md", "section": "## Layout"}
    findings = check(tmp_path, rule)
    assert [(f.level, f.path) for f in findings] == [("ERROR", "Docs/"), ("WARN", "Extra")]
    (tmp_path / "Docs").mkdir()
    rule["ignore_directories"] = ["Extra"]
    assert check(tmp_path, rule) == []


@pytest.mark.parametrize("text", ["## Other\n", "## Layout\nNo directory entries\n"])
def test_layout_cannot_pass_a_missing_or_empty_declaration_table(tmp_path, text):
    write(tmp_path, "POLICY.md", text)
    assert (
        check(tmp_path, {"type": "layout", "document": "POLICY.md", "section": "## Layout"})[
            0
        ].level
        == "ERROR"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("# Page\n", 1),
        ("---\n---\n", 1),
        ("---\nstatus: odd\n---\n", 2),
        ("---\ntitle: Page\nstatus: ready\n---\n", 0),
    ],
)
def test_metadata_required_fields_and_allowed_values(tmp_path, text, expected):
    write(tmp_path, "Docs/page.md", text)
    rule = {
        "type": "metadata",
        "include": ["Docs/**/*.md"],
        "fields": ["title"],
        "values": {"status": ["ready"]},
        "severity": "warn",
    }
    findings = check(tmp_path, rule)
    assert len(findings) == expected
    assert all(f.level == "WARN" for f in findings)


def test_metadata_exclusions_are_explicit(tmp_path):
    write(tmp_path, "Docs/imports/source.md", "Original material")
    write(tmp_path, "Docs/index.md", "Index")
    write(tmp_path, "Docs/page.md", "---\ntitle: Page\n---\n")
    assert (
        check(
            tmp_path,
            {
                "type": "metadata",
                "include": ["Docs/**/*.md"],
                "exclude": ["Docs/index.md"],
                "exclude_regex": ["^Docs/imports/"],
                "fields": ["title"],
            },
        )
        == []
    )


def test_source_reference_strictness_and_parent_compatibility(tmp_path):
    write(
        tmp_path,
        "Docs/page.md",
        "---\nsources:\n  - 'Inputs/group/missing.md' # explanation\n  - Elsewhere/unselected.md\n---\n",
    )
    (tmp_path / "Inputs/group").mkdir(parents=True)
    rule = {
        "type": "source_references",
        "include": ["Docs/*.md"],
        "prefixes": ["Inputs/"],
        "strip_annotations": True,
    }
    assert len(check(tmp_path, rule)) == 1
    rule["allow_parent"] = True
    assert check(tmp_path, rule) == []
    (tmp_path / "Inputs/group").rmdir()
    assert len(check(tmp_path, rule)) == 1


def test_navigation_treats_filenames_literally(tmp_path):
    write(tmp_path, "Docs/a[1].md", "Document")
    write(tmp_path, "Docs/index.md", "a1")
    rule = {
        "type": "navigation",
        "include": ["Docs/*.md"],
        "exclude": ["Docs/index.md"],
        "indexes": ["Docs/index.md"],
    }
    assert len(check(tmp_path, rule)) == 1
    write(tmp_path, "Docs/index.md", "[Page](a[1].md)")
    assert check(tmp_path, rule) == []


def test_taxonomy_uses_documented_values_and_conditional_fields(tmp_path):
    write(tmp_path, "Inputs/PROVENANCE.md", "## Kinds\n| `memo` | note |\n")
    write(tmp_path, "Inputs/item.md", "- **kind:** wrong\n")
    rule = {
        "type": "taxonomy",
        "include": ["Inputs/PROVENANCE.md"],
        "section": "## Kinds",
        "exclude_names": ["PROVENANCE.md"],
        "required_when": [{"value": "memo", "fields": ["Source URL"]}],
    }
    assert len(check(tmp_path, rule)) == 1
    write(tmp_path, "Inputs/item.md", "- **kind:** memo\n")
    assert "Source URL" in check(tmp_path, rule)[0].message
    write(tmp_path, "Inputs/item.md", "- **kind:** memo\n- **Source URL:** local\n")
    assert check(tmp_path, rule) == []
    write(tmp_path, "Inputs/item.md", "Unclassified\n")
    assert check(tmp_path, rule)[0].level == "WARN"


def test_taxonomy_empty_table_is_not_a_pass(tmp_path):
    write(tmp_path, "Inputs/PROVENANCE.md", "## Kinds\nNothing declared\n")
    findings = check(
        tmp_path, {"type": "taxonomy", "include": ["Inputs/PROVENANCE.md"], "section": "## Kinds"}
    )
    assert len(findings) == 1


def test_required_files_and_inline_paths(tmp_path):
    (tmp_path / "Topics/one").mkdir(parents=True)
    write(
        tmp_path, "instructions.md", "Use `Topics/one/` and `Missing/`. Example: `Missing/file.py`."
    )
    rules = [
        {"type": "required_files", "include": ["Topics/*"], "files": ["README.md"]},
        {"type": "inline_paths", "include": ["instructions.md"], "pattern": r"`([^`]+/)`"},
    ]
    assert len(check(tmp_path, *rules)) == 2
    write(tmp_path, "Topics/one/README.md", "Topic")
    (tmp_path / "Missing").mkdir()
    assert check(tmp_path, *rules) == []


def test_heading_policy_is_only_applied_when_configured(tmp_path):
    write(tmp_path, "Inputs/original.md", "# Summary\nOriginal source\n")
    metadata = {"type": "metadata", "include": ["Docs/*.md"], "fields": ["title"]}
    assert check(tmp_path, metadata) == []
    rule = {
        "type": "forbidden_text",
        "include": ["Inputs/*.md"],
        "pattern": "^# Summary",
        "first_lines": 2,
        "skip_first_line_pattern": "^EXPORTED",
    }
    assert len(check(tmp_path, rule)) == 1
    write(tmp_path, "Inputs/original.md", "EXPORTED source\n# Summary\n")
    assert check(tmp_path, rule) == []
    write(tmp_path, "Inputs/original.md", "First\nSecond\n# Summary\n")
    assert check(tmp_path, rule) == []


def test_forbidden_paths_respect_directory_depth(tmp_path):
    write(tmp_path, "Archive/2030-01-01_item.md")
    write(tmp_path, "Archive/2030-01/2030-01-01_item.md")
    findings = check(tmp_path, {"type": "forbidden_paths", "include": ["Archive/????-??-??_*.md"]})
    assert len(findings) == 1
    assert findings[0].path == "Archive/2030-01-01_item.md"


@pytest.mark.parametrize(
    "program, expected",
    [
        ("print('WARN\\tlegacy finding')", ["WARN"]),
        ("print('ERROR\\tpolicy failed'); raise SystemExit(1)", ["ERROR"]),
        ("raise SystemExit(7)", ["ERROR"]),
        ("print('unexpected text')", ["ERROR"]),
        ("import sys; print('diagnostic', file=sys.stderr)", ["ERROR"]),
    ],
)
def test_external_checkers_cannot_lose_failures(tmp_path, program, expected):
    findings = check(tmp_path, {"type": "external", "argv": [sys.executable, "-c", program]})
    assert [f.level for f in findings] == expected


def test_external_checkers_receive_exact_root_and_cwd(tmp_path):
    findings = check(
        tmp_path,
        {
            "type": "external",
            "argv": [
                sys.executable,
                "-c",
                "import os,sys; assert os.getcwd()==sys.argv[1]",
                "@root@",
            ],
        },
    )
    assert findings == []


def test_external_defaults_expand_once_and_preserve_selected_environment(tmp_path, monkeypatch):
    root = tmp_path / "checkout ${UNDEFINED_ROOT_TEXT}"
    root.mkdir()
    home = tmp_path / "different home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VALIDATOR_ROOT", "selected $UNCHANGED")
    monkeypatch.setenv("VALIDATOR_CONFIG", "")
    monkeypatch.delenv("VALIDATOR_CACHE", raising=False)
    write(
        root,
        "capture.py",
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "Path('captured.json').write_text(json.dumps({'argv':sys.argv[1:],"
        "'cache':os.environ['VALIDATOR_CACHE']}))\n",
    )
    findings = check(
        root,
        {
            "type": "external",
            "environment_defaults": {
                "VALIDATOR_ROOT": "$MISSING_BUT_UNUSED",
                "VALIDATOR_CONFIG": "${HOME}/settings.json",
                "VALIDATOR_CACHE": "$VALIDATOR_CONFIG.cache",
            },
            "expand_environment": True,
            "argv": [
                sys.executable,
                "@root@/capture.py",
                "$VALIDATOR_ROOT",
                "${VALIDATOR_CONFIG}",
                "~/data",
                "@root@",
                "$$literal",
            ],
        },
    )
    assert findings == []
    assert json.loads((root / "captured.json").read_text()) == {
        "argv": [
            "selected $UNCHANGED",
            str(home / "settings.json"),
            str(home / "data"),
            str(root),
            "$literal",
        ],
        "cache": str(home / "settings.json.cache"),
    }
    assert os.environ["VALIDATOR_CONFIG"] == ""
    assert "VALIDATOR_CACHE" not in os.environ


def test_external_environment_expansion_is_opt_in(tmp_path):
    assert (
        check(
            tmp_path,
            {
                "type": "external",
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; assert sys.argv[1:] == "
                        "['$UNDEFINED_LITERAL', '~', '${UNDEFINED_LITERAL}']"
                    ),
                    "$UNDEFINED_LITERAL",
                    "~",
                    "${UNDEFINED_LITERAL}",
                ],
            },
        )
        == []
    )


@pytest.mark.parametrize("include,exclude", [(["Absent/*.md"], []), (["*.md"], ["*.md"])])
def test_external_empty_selection_does_not_resolve_or_execute(tmp_path, include, exclude):
    write(tmp_path, "source.md")
    assert (
        check(
            tmp_path,
            {
                "type": "external",
                "include": include,
                "exclude": exclude,
                "expand_environment": True,
                "argv": ["$MISSING_VALIDATOR"],
            },
        )
        == []
    )
    findings = check(
        tmp_path,
        {
            "type": "external",
            "include": ["source.md"],
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
        },
    )
    assert [finding.level for finding in findings] == ["ERROR"]


@pytest.mark.parametrize("kind", ["directory", "broken_symlink"])
def test_external_invalid_selected_paths_are_left_to_the_validator(tmp_path, kind):
    candidate = tmp_path / "invalid.md"
    if kind == "directory":
        candidate.mkdir()
    else:
        candidate.symlink_to(tmp_path / "missing-target")
    findings = check(
        tmp_path,
        {
            "type": "external",
            "include": ["*.md"],
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
        },
    )
    assert [finding.level for finding in findings] == ["ERROR"]


@pytest.mark.parametrize(
    "rule",
    [
        {"expand_environment": True, "argv": ["$MISSING_VALIDATOR"]},
        {"environment_defaults": {"FIRST": "$MISSING_VALIDATOR"}},
        {"environment_defaults": {"FIRST": "$SECOND", "SECOND": "later"}},
        {"expand_environment": True, "argv": ["$(touch must-not-exist)"]},
        {"environment_defaults": {"INVALID-NAME": "value"}},
        {"environment_defaults": {"VALIDATOR_ROOT": 1}},
        {"expand_environment": "true"},
        {"argv": ["/missing-synthetic-validator"]},
    ],
)
def test_external_invalid_configuration_fails_before_a_success_report(
    tmp_path, monkeypatch, capsys, rule
):
    for name in ("MISSING_VALIDATOR", "FIRST", "SECOND"):
        monkeypatch.delenv(name, raising=False)
    rule = {"type": "external", "argv": [sys.executable, "-c", "pass"], **rule}
    config = write(
        tmp_path,
        "rules.json",
        json.dumps(
            {
                "schema": "structure-lint/v1",
                "checks": [rule],
            }
        ),
    )
    assert main(["--root", str(tmp_path), "--config", str(config), "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["errors"] == 1
    assert not (tmp_path / "must-not-exist").exists()


def test_summary_formats_and_missing_configuration_fail_closed(tmp_path, capsys):
    config = write(
        tmp_path,
        "rules.json",
        json.dumps(
            {
                "schema": "structure-lint/v1",
                "checks": [{"type": "forbidden_paths", "include": ["*.bad"]}],
            }
        ),
    )
    write(tmp_path, "example.bad")
    assert main(["--root", str(tmp_path), "--config", str(config)]) == 1
    assert capsys.readouterr().out.endswith("=== Summary: 1 errors, 0 warnings ===\n")
    assert main(["--root", str(tmp_path), "--config", str(config), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == 1
    assert main(["--root", str(tmp_path), "--config", str(config), "--format", "tsv"]) == 1
    assert capsys.readouterr().out.startswith("ERROR\t")
    assert main(["--root", str(tmp_path), "--config", str(tmp_path / "missing")]) == 2
    assert "1 errors" in capsys.readouterr().out


@pytest.mark.parametrize(
    "checks", [[], [{"type": "unknown"}], [{"type": "metadata", "include": ["../*.md"]}]]
)
def test_invalid_rules_are_rejected(tmp_path, checks):
    with pytest.raises(ValueError):
        Checker(tmp_path).run({"schema": "structure-lint/v1", "checks": checks})


def test_copied_package_runs_with_an_unrelated_home_and_no_other_skills(tmp_path):
    package = Path(__file__).resolve().parents[1]
    copied = tmp_path / "only-package"
    shutil.copytree(
        package,
        copied,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache", ".venv"),
    )
    root = tmp_path / "project"
    root.mkdir()
    config = write(
        root,
        "rules.json",
        json.dumps(
            {
                "schema": "structure-lint/v1",
                "checks": [
                    {"type": "required_files", "include": ["Docs/*"], "files": ["README.md"]}
                ],
            }
        ),
    )
    home = tmp_path / "different-user"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(copied / "scripts/check"), "--root", str(root), "--config", str(config)],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "0 errors, 0 warnings" in result.stdout


def test_git_freshness_reads_local_tracking_refs_and_respects_opt_out(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "next")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("reset", "--hard", base)
    git("remote", "add", "origin", str(tmp_path / "absent-remote"))
    rule = {"type": "git_freshness", "severity": "warn", "skip_environment": "SKIP_TEST_FETCH"}
    assert len(check(tmp_path, rule)) == 1
    monkeypatch.setenv("SKIP_TEST_FETCH", "1")
    assert check(tmp_path, rule) == []


def test_frontmatter_only_reads_the_initial_top_level_field_lines():
    body, fields = frontmatter(
        "---\ntitle: Example\n  nested: ignored\n---\nstatus: not metadata\n"
    )
    assert body is not None
    assert fields == {"title": "Example"}
