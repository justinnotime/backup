import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "github_archive.py"
SPEC = importlib.util.spec_from_file_location("github_archive_for_tests", SCRIPT)
GH_SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = GH_SYNC
SPEC.loader.exec_module(GH_SYNC)


def issue(number: int, *, body: str = "", merged: str | None = None) -> dict:
    payload = {
        "number": number,
        "title": f"Item {number}",
        "state": "closed" if merged else "open",
        "user": {"login": "tester"},
        "created_at": "2026-07-19T00:00:00Z",
        "updated_at": "2026-07-19T01:00:00Z",
        "closed_at": merged,
        "labels": [],
        "assignees": [],
        "milestone": None,
        "html_url": f"https://github.com/acme/project/issues/{number}",
        "body": body,
        "_comments": [],
    }
    if merged:
        payload["pull_request"] = {"merged_at": merged}
        payload["html_url"] = f"https://github.com/acme/project/pull/{number}"
    return payload


class GhSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.config = self.workspace / "config.yaml"

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, *, depth: int = 0, maximum: int = 300,
                     config: Path | None = None) -> None:
        config = config or self.config
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "\n".join(
                [
                    "repos:",
                    "  - owner: acme",
                    "    name: project",
                    "    seeds:",
                    "      all: true",
                    "    closure:",
                    f"      depth: {depth}",
                    f"      max_total: {maximum}",
                    "state_file: state/sync.json",
                    "output_dir: archive",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def run_main(self, *args: str, config: Path | None = None) -> int:
        argv = ["sync", "--config", str(config or self.config), *map(str, args)]
        with mock.patch.object(GH_SYNC.sys, "argv", argv):
            return GH_SYNC.main()

    def state_path(self) -> Path:
        return self.workspace / "state" / "sync.json"

    def test_all_seed_uses_paginated_issues_endpoint_and_keeps_prs(self):
        items = [
            {"number": 1, "updated_at": "2026-07-19T00:00:00Z"},
            {
                "number": 2,
                "updated_at": "2026-07-19T01:00:00Z",
                "pull_request": {"merged_at": None},
            },
        ]
        config = GH_SYNC.RepoConfig("acme", "project", seed_all=True)
        with mock.patch.object(GH_SYNC, "gh_api", return_value=items) as api:
            seeds = GH_SYNC.collect_seeds(config)
        self.assertEqual(seeds, {1: "2026-07-19T00:00:00Z", 2: "2026-07-19T01:00:00Z"})
        api.assert_called_once_with(
            "/repos/acme/project/issues?state=all&per_page=100", paginate=True
        )

    def test_render_pull_request_records_merged_timestamp(self):
        merged = "2026-07-19T09:14:00Z"
        rendered = GH_SYNC.render_issue_md(issue(92, merged=merged), "acme/project", [])
        self.assertIn("type: gh-pull-request", rendered)
        self.assertIn(f"merged: '{merged}'", rendered)

    def test_crossrefs_reject_zero_and_implausibly_large_numbers(self):
        refs = GH_SYNC.extract_crossrefs(
            "valid #1; invalid #0 and #1000000000", "acme/project"
        )
        self.assertEqual(refs, {("acme/project", 1)})

    def test_incremental_unchanged_item_skips_before_fetching_comments(self):
        self.write_config()
        repo_dir = self.workspace / "archive" / "acme_project"
        repo_dir.mkdir(parents=True)
        (repo_dir / "1.md").write_text("existing\n", encoding="utf-8")
        self.state_path().parent.mkdir(parents=True)
        self.state_path().write_text(
            json.dumps(
                {
                    "acme/project": {
                        "last_sync": "2026-07-20T00:00:00Z",
                        "target_numbers": [1],
                    }
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            GH_SYNC, "collect_seeds", return_value={1: "2026-07-19T01:00:00Z"}
        ), mock.patch.object(GH_SYNC, "fetch_issue") as fetch:
            self.assertEqual(self.run_main(), 0)
        fetch.assert_not_called()
        state = json.loads(self.state_path().read_text(encoding="utf-8"))
        self.assertGreater(
            state["acme/project"]["last_sync"], "2026-07-20T00:00:00Z"
        )

    def test_closure_fetch_is_cached_and_new_targets_obey_total_cap(self):
        self.write_config(depth=2, maximum=3)
        issues = {
            1: issue(1, body="#2 #3 #4"),
            2: issue(2),
            3: issue(3),
            4: issue(4),
        }
        with mock.patch.object(
            GH_SYNC, "collect_seeds", return_value={1: "2026-07-19T01:00:00Z"}
        ), mock.patch.object(
            GH_SYNC, "fetch_issue", side_effect=lambda _repo, number: issues[number]
        ) as fetch:
            self.assertEqual(self.run_main(), 0)

        self.assertEqual([call.args[1] for call in fetch.call_args_list], [1, 2, 3])
        state = json.loads(self.state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["acme/project"]["target_numbers"], [1, 2, 3])
        self.assertFalse(
            (self.workspace / "archive" / "acme_project" / "4.md").exists()
        )

    def test_fetch_failure_does_not_advance_repository_cursor(self):
        self.write_config()
        repo_dir = self.workspace / "archive" / "acme_project"
        repo_dir.mkdir(parents=True)
        (repo_dir / "1.md").write_text("existing\n", encoding="utf-8")
        old_cursor = "2026-07-18T00:00:00Z"
        self.state_path().parent.mkdir(parents=True)
        self.state_path().write_text(
            json.dumps(
                {
                    "acme/project": {
                        "last_sync": old_cursor,
                        "target_numbers": [1],
                    }
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            GH_SYNC, "collect_seeds", return_value={1: "2026-07-19T01:00:00Z"}
        ), mock.patch.object(GH_SYNC, "fetch_issue", return_value=None):
            self.assertEqual(self.run_main(), 1)

        state = json.loads(self.state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["acme/project"]["last_sync"], old_cursor)

    def test_custom_layout_recovers_targets_without_state(self):
        self.write_config()
        config = GH_SYNC.yaml.safe_load(self.config.read_text())
        config["filename_template"] = "ticket-{number}.md"
        config["repos"][0]["directory"] = "tickets"
        self.config.write_text(GH_SYNC.yaml.safe_dump(config))
        directory = self.workspace / "archive" / "tickets"
        directory.mkdir(parents=True)
        (directory / "ticket-8.md").write_text("previous archive\n")
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}), mock.patch.object(
            GH_SYNC, "fetch_issue", side_effect=lambda _repo, number: issue(number)
        ) as fetch:
            self.assertEqual(self.run_main(), 0)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [1, 8])
        self.assertEqual(sorted(path.name for path in directory.iterdir()),
                         ["ticket-1.md", "ticket-8.md"])
        state = json.loads(self.state_path().read_text())
        self.assertEqual(state["acme/project"]["target_numbers"], [1, 8])
        self.assertFalse((self.workspace / "archive" / "acme_project").exists())

    def test_missing_archive_is_refetched_despite_unchanged_timestamp(self):
        self.write_config()
        self.state_path().parent.mkdir(parents=True)
        self.state_path().write_text(json.dumps({"acme/project": {
            "last_sync": "2026-07-20T00:00:00Z", "target_numbers": [1],
        }}))
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={}), mock.patch.object(
            GH_SYNC, "fetch_issue", return_value=issue(1)
        ) as fetch:
            self.assertEqual(self.run_main(), 0)
        fetch.assert_called_once_with("acme/project", 1)
        self.assertTrue((self.workspace / "archive" / "acme_project" / "1.md").is_file())

    def test_full_rerun_preserves_unchanged_archive_bytes_and_mtime(self):
        self.write_config()
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}), mock.patch.object(
            GH_SYNC, "fetch_issue", return_value=issue(1, body="Stable body")
        ):
            self.assertEqual(self.run_main(), 0)
            target = self.workspace / "archive" / "acme_project" / "1.md"
            before = (target.read_bytes(), target.stat().st_mtime_ns)
            self.assertEqual(self.run_main("--full"), 0)
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)

    def test_dry_run_reads_crossrefs_without_writing_any_files(self):
        self.write_config(depth=1)
        before = {p.relative_to(self.workspace): p.read_bytes()
                  for p in self.workspace.rglob("*") if p.is_file()}
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}), mock.patch.object(
            GH_SYNC, "fetch_issue", return_value=issue(1, body="#2")
        ) as fetch:
            self.assertEqual(self.run_main("--dry-run"), 0)
        fetch.assert_called_once_with("acme/project", 1)
        after = {p.relative_to(self.workspace): p.read_bytes()
                 for p in self.workspace.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.workspace / "archive").exists())
        self.assertFalse(self.state_path().parent.exists())

    def test_two_configs_resolve_relative_paths_independently(self):
        for label, number in (("first", 1), ("second", 2)):
            config = self.workspace / label / "config.yaml"
            self.write_config(config=config)
            with mock.patch.object(GH_SYNC, "collect_seeds", return_value={number: None}), mock.patch.object(
                GH_SYNC, "fetch_issue", return_value=issue(number)
            ):
                self.assertEqual(self.run_main(config=config), 0)
        for label, number in (("first", 1), ("second", 2)):
            directory = self.workspace / label
            files = list((directory / "archive" / "acme_project").glob("*.md"))
            self.assertEqual([path.name for path in files], [f"{number}.md"])
            state = json.loads((directory / "state" / "sync.json").read_text())
            self.assertEqual(state["acme/project"]["target_numbers"], [number])
        self.assertFalse((self.workspace / "archive").exists())

    def test_base_directory_and_explicit_paths_override_config(self):
        self.write_config()
        base = self.workspace / "base"
        explicit_output = self.workspace / "explicit-output"
        explicit_state = self.workspace / "explicit-state.json"
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}), mock.patch.object(
            GH_SYNC, "fetch_issue", return_value=issue(1)
        ):
            self.assertEqual(self.run_main("--base-dir", str(base)), 0)
            self.assertTrue((base / "archive" / "acme_project" / "1.md").is_file())
            self.assertTrue((base / "state" / "sync.json").is_file())
            self.assertEqual(self.run_main("--base-dir", str(base), "--output-dir", str(explicit_output),
                                           "--state-file", str(explicit_state)), 0)
        self.assertTrue((explicit_output / "acme_project" / "1.md").is_file())
        self.assertTrue(explicit_state.is_file())
        self.assertFalse((self.workspace / "archive").exists())
        self.assertFalse(self.state_path().exists())

    def test_unsafe_directory_and_filename_are_rejected_before_fetch(self):
        for field, value in (("directory", "../escape"), ("directory", "/absolute"),
                             ("directory", "."), ("directory", "nested/path"),
                             ("filename_template", "../{number}.md"),
                             ("filename_template", "{number}/{number}.md"),
                             ("filename_template", "all.md"),
                             ("filename_template", "{title}.md")):
            with self.subTest(field=field, value=value):
                self.write_config()
                config = GH_SYNC.yaml.safe_load(self.config.read_text())
                destination = config["repos"][0] if field == "directory" else config
                destination[field] = value
                self.config.write_text(GH_SYNC.yaml.safe_dump(config))
                with mock.patch.object(GH_SYNC, "collect_seeds") as fetch:
                    self.assertEqual(self.run_main(), 2)
                fetch.assert_not_called()
                self.assertFalse((self.workspace / "archive").exists())
                self.assertFalse(self.state_path().exists())

    def test_conflicting_repository_directories_are_rejected_before_fetch(self):
        self.write_config()
        config = GH_SYNC.yaml.safe_load(self.config.read_text())
        config["repos"][0]["directory"] = "shared"
        config["repos"].append({"owner": "acme", "name": "other", "directory": "shared"})
        self.config.write_text(GH_SYNC.yaml.safe_dump(config))
        with mock.patch.object(GH_SYNC, "collect_seeds") as fetch:
            self.assertEqual(self.run_main(), 2)
        fetch.assert_not_called()
        self.assertFalse((self.workspace / "archive").exists())

    def test_repository_symlink_cannot_escape_output_directory(self):
        self.write_config()
        outside = self.workspace / "outside"
        outside.mkdir()
        archive = self.workspace / "archive"
        archive.mkdir()
        (archive / "acme_project").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(GH_SYNC, "collect_seeds") as fetch:
            self.assertEqual(self.run_main(), 2)
        fetch.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.state_path().exists())

    def test_repository_symlink_cannot_alias_another_repository(self):
        self.write_config()
        config = GH_SYNC.yaml.safe_load(self.config.read_text())
        config["repos"][0]["directory"] = "alias"
        config["repos"].append({"owner": "acme", "name": "other", "directory": "real",
                                "seeds": {"all": True}})
        self.config.write_text(GH_SYNC.yaml.safe_dump(config))
        archive = self.workspace / "archive"
        target = archive / "real"
        target.mkdir(parents=True)
        existing = target / "1.md"
        existing.write_text("existing archive content\n")
        (archive / "alias").symlink_to("real", target_is_directory=True)
        with mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}) as seeds, \
                mock.patch.object(GH_SYNC, "fetch_issue", return_value=issue(1)):
            self.assertEqual(self.run_main(), 2)
        seeds.assert_not_called()
        self.assertEqual(existing.read_text(), "existing archive content\n")
        self.assertFalse(self.state_path().exists())

    def test_repository_directory_cannot_reenter_skill_from_parent_output(self):
        self.write_config()
        package = self.workspace / "skills" / "github-archive"
        source = package / "src" / "github_archive.py"
        source.parent.mkdir(parents=True)
        source.write_text("# synthetic package source\n")
        (package / "1.md").write_text("existing package content\n")
        before = {path.relative_to(package): path.read_bytes()
                  for path in package.rglob("*") if path.is_file()}
        config = GH_SYNC.yaml.safe_load(self.config.read_text())
        config["output_dir"] = str(package.parent)
        config["repos"][0]["directory"] = package.name
        self.config.write_text(GH_SYNC.yaml.safe_dump(config))
        with mock.patch.object(GH_SYNC, "__file__", str(source)), \
                mock.patch.object(GH_SYNC, "collect_seeds", return_value={1: None}) as seeds, \
                mock.patch.object(GH_SYNC, "fetch_issue", return_value=issue(1)):
            self.assertEqual(self.run_main(), 2)
        seeds.assert_not_called()
        after = {path.relative_to(package): path.read_bytes()
                 for path in package.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(self.state_path().exists())

    def test_raw_gh_stderr_is_not_repeated_in_logs(self):
        sentinel = "SYNTHETIC_PRIVATE_ERROR_SENTINEL"
        for seed_all in (True, False):
            with self.subTest(seed_all=seed_all):
                self.write_config()
                if not seed_all:
                    config = GH_SYNC.yaml.safe_load(self.config.read_text())
                    config["repos"][0]["seeds"] = {"labels": ["example"]}
                    self.config.write_text(GH_SYNC.yaml.safe_dump(config))
                error = subprocess.CalledProcessError(1, ["gh"], output=sentinel, stderr=sentinel)
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(GH_SYNC.subprocess, "run", side_effect=error), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(self.run_main(), 1)
                self.assertNotIn(sentinel, stdout.getvalue())
                self.assertNotIn(sentinel, stderr.getvalue())
                self.assertIn("failed", stderr.getvalue().lower())
                self.assertEqual(json.loads(self.state_path().read_text()), {})


if __name__ == "__main__":
    unittest.main()
