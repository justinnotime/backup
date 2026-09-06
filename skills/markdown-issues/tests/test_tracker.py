from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from markdown_issues import tracker as issues


def issue_text(
    issue_id: str,
    *,
    updated: str = "2026-07-12T00:00:00Z",
    review_after: str | None = None,
    note: str = "- 2026-07-12T00:00:00Z [writer] opened.",
    checked: bool = False,
) -> str:
    review_line = f"review_after: {review_after}\n" if review_after else ""
    checkbox = "x" if checked else " "
    return f"---\nid: {issue_id}\ntitle: Test issue\ncreated: 2026-07-12T00:00:00Z\nupdated: {updated}\nstate: open\nassignee: writer\npriority: P1\nkind: action\nproject: example\n{review_line}labels: [test]\nsources: []\nrelated: []\nexternal_refs: []\nblocks: []\nblocked_by: []\n---\n\n# Test issue\n\n## Context\n\nTest.\n\n## Acceptance\n\n- [{checkbox}] Done.\n\n## Notes\n\n{note}\n"


class IssuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        (self.repo / "records" / "active").mkdir(parents=True)
        (self.repo / "records" / "resolved").mkdir(parents=True)
        cfg = json.loads((ROOT / "references/example.json").read_text())
        cfg["repository_root"] = str(self.repo)
        issues.configure(cfg)
        self.config = self.repo / "config.json"
        self.config.write_text(json.dumps(cfg))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_issue(self, issue_id: str, text: str) -> Path:
        path = self.repo / "records" / "active" / f"{issue_id}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def init_git(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "writer@example.test"], cwd=self.repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)

    def test_future_review_is_scheduled_not_stale(self) -> None:
        issue_id = "2026-07-12_future-review_12345678"
        self.write_issue(
            issue_id,
            issue_text(issue_id, updated="2026-05-01T00:00:00Z", review_after="2026-07-29").replace(
                "kind: action", "kind: watch"
            ),
        )
        loaded = issues.load_issues(self.repo)
        findings = issues.audit_issues(self.repo, loaded, date(2026, 7, 12))
        messages = [finding.message for finding in findings]
        self.assertFalse(any("stale:" in message for message in messages))
        brief = issues.brief_lines(loaded, date(2026, 7, 12), 8)
        self.assertIn("1 scheduled", brief[0])
        self.assertIn("2026-07-29", brief[-1])

    def test_checked_acceptance_is_close_candidate(self) -> None:
        issue_id = "2026-07-12_close-candidate_12345678"
        self.write_issue(issue_id, issue_text(issue_id, checked=True))
        loaded = issues.load_issues(self.repo)
        findings = issues.audit_issues(self.repo, loaded, date(2026, 7, 12))
        self.assertTrue(any("close-candidate" in finding.message for finding in findings))
        self.assertIn("CLOSE?", "\n".join(issues.brief_lines(loaded, date(2026, 7, 12), 8)))

    def test_existing_notes_are_append_only_across_base_ref(self) -> None:
        issue_id = "2026-07-12_append-only_12345678"
        path = self.write_issue(issue_id, issue_text(issue_id))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        path.write_text(issue_text(issue_id).replace("opened.", "rewritten."), encoding="utf-8")
        findings = issues.audit_issues(
            self.repo, issues.load_issues(self.repo), date(2026, 7, 12), "HEAD"
        )
        self.assertTrue(any("changed or were reordered" in finding.message for finding in findings))

    def test_deleting_an_issue_from_the_base_is_an_error(self) -> None:
        issue_id = "2026-07-12_deleted_12345678"
        path = self.write_issue(issue_id, issue_text(issue_id))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        path.unlink()
        findings = issues.audit_issues(
            self.repo, issues.load_issues(self.repo), date(2026, 7, 12), "HEAD"
        )
        self.assertTrue(any("Issue disappeared" in finding.message for finding in findings))

    def test_action_review_date_does_not_hide_actionable_work(self) -> None:
        issue_id = "2026-07-12_action-review_12345678"
        self.write_issue(issue_id, issue_text(issue_id, review_after="2026-07-29"))
        loaded = issues.load_issues(self.repo)
        findings = issues.audit_issues(self.repo, loaded, date(2026, 7, 12))
        self.assertTrue(any("only schedules" in finding.message for finding in findings))
        self.assertIn(
            "1 actionable; 0 scheduled", issues.brief_lines(loaded, date(2026, 7, 12), 8)[0]
        )

    def test_required_body_sections_are_present_ordered_and_nonempty(self) -> None:
        issue_id = "2026-07-12_bad-body_12345678"
        broken = (
            issue_text(issue_id).replace("## Context\n\nTest.\n\n", "").replace("- [ ] Done.", "")
        )
        self.write_issue(issue_id, broken)
        findings = issues.audit_issues(self.repo, issues.load_issues(self.repo), date(2026, 7, 12))
        messages = "\n".join(finding.message for finding in findings)
        self.assertIn("missing ## Context", messages)
        self.assertIn("Acceptance section is empty", messages)
        self.assertIn("Acceptance needs a checkbox", messages)

    def test_state_transition_requires_updated_and_appended_note(self) -> None:
        issue_id = "2026-07-12_transition_12345678"
        path = self.write_issue(issue_id, issue_text(issue_id))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        closed = self.repo / "records" / "resolved" / path.name
        path.rename(closed)
        closed.write_text(
            issue_text(issue_id).replace("state: open", "state: closed"), encoding="utf-8"
        )
        findings = issues.audit_issues(
            self.repo, issues.load_issues(self.repo), date(2026, 7, 12), "HEAD"
        )
        messages = "\n".join(finding.message for finding in findings)
        self.assertIn("advance updated", messages)
        self.assertIn("append a Notes entry", messages)

    def test_hard_wrapped_note_continuation_is_append_only(self) -> None:
        issue_id = "2026-07-12_wrapped_12345678"
        wrapped = issue_text(issue_id).replace("opened.", "opened.\n  continued context.")
        path = self.write_issue(issue_id, wrapped)
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        path.write_text(wrapped.replace("continued context", "rewritten context"), encoding="utf-8")
        findings = issues.audit_issues(
            self.repo, issues.load_issues(self.repo), date(2026, 7, 12), "HEAD"
        )
        self.assertTrue(any("changed or were reordered" in finding.message for finding in findings))

    def test_watch_path_change_reenters_actionable_queue(self) -> None:
        issue_id = "2026-07-12_watch-signal_12345678"
        watched = self.repo / "signal.txt"
        watched.write_text("old\n", encoding="utf-8")
        text = issue_text(issue_id, review_after="2026-07-29").replace(
            "kind: action\nproject: example",
            "kind: watch\nproject: example\nwatch_paths: [signal.txt]",
        )
        self.write_issue(issue_id, text)
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        watched.write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "add", "signal.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "signal"], cwd=self.repo, check=True)
        loaded = issues.load_issues(self.repo)
        brief = issues.brief_lines(loaded, date(2026, 7, 12), 8, repo=self.repo)
        self.assertIn("1 actionable; 0 scheduled", brief[0])

    def test_default_cli_baseline_grandfathers_legacy_note(self) -> None:
        issue_id = "2026-07-12_legacy-note_12345678"
        self.write_issue(issue_id, issue_text(issue_id, note="- malformed legacy note"))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        result = subprocess.run(
            [
                "/bin/sh",
                str(ROOT / "scripts" / "issues"),
                "--config",
                str(self.config),
                "--repo",
                str(self.repo),
                "lint",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN", result.stdout)

    REVERSED = ("changed or were reordered", "must advance updated", "must append a Notes entry")

    def stale_checkout_with_upstream_ahead(self, issue_id: str, note: str | None = None) -> str:
        """Commit a legal append on `upstream`, then step the tree back behind it."""
        first_note = note or "- 2026-07-12T00:00:00Z [writer] opened."
        path = self.write_issue(issue_id, issue_text(issue_id, note=first_note))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        path.write_text(
            issue_text(
                issue_id,
                updated="2026-07-30T00:00:00Z",
                note=f"{first_note}\n- 2026-07-30T00:00:00Z [reviewer] took over.",
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "upstream append"], cwd=self.repo, check=True)
        subprocess.run(["git", "branch", "upstream"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "--detach", first], cwd=self.repo, check=True)
        return first

    def test_stale_checkout_refuses_instead_of_reporting_reversed_findings(self) -> None:
        first = self.stale_checkout_with_upstream_ahead("2026-07-12_stale-base_12345678")
        messages = [
            finding.message
            for finding in issues.audit_issues(
                self.repo, issues.load_issues(self.repo), date(2026, 7, 30), "upstream"
            )
        ]
        joined = "\n".join(messages)
        self.assertIn("cannot verify the issue append-only contract", joined)
        self.assertIn("git pull --rebase", joined)
        for reversed_finding in self.REVERSED:
            self.assertNotIn(reversed_finding, joined)
        against_ancestor = [
            finding.message
            for finding in issues.audit_issues(
                self.repo, issues.load_issues(self.repo), date(2026, 7, 30), first
            )
        ]
        self.assertNotIn("cannot verify", "\n".join(against_ancestor))

    def test_stale_checkout_does_not_promote_grandfathered_warnings(self) -> None:
        """Refusing must not itself become a false alarm.

        Dropping the base entirely would make every pre-existing note look new
        and turn a dozen legacy WARNs into ERRORs, so the guard falls back to the
        divergence point instead of to nothing.
        """
        self.stale_checkout_with_upstream_ahead(
            "2026-07-12_stale-legacy_12345678", note="- malformed legacy note"
        )
        findings = issues.audit_issues(
            self.repo, issues.load_issues(self.repo), date(2026, 7, 30), "upstream"
        )
        malformed = [f for f in findings if "malformed Notes entry" in f.message]
        self.assertTrue(malformed)
        self.assertEqual([f.level for f in malformed], ["WARN"] * len(malformed))

    def test_stale_checkout_lint_still_exits_nonzero(self) -> None:
        self.stale_checkout_with_upstream_ahead("2026-07-12_stale-exit_12345678")
        result = subprocess.run(
            [
                "/bin/sh",
                str(ROOT / "scripts" / "issues"),
                "--config",
                str(self.config),
                "--repo",
                str(self.repo),
                "lint",
                "--base-ref",
                "upstream",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("git pull --rebase", result.stdout)

    def test_guard_leaves_a_real_violation_reportable(self) -> None:
        issue_id = "2026-07-12_real-violation_12345678"
        path = self.write_issue(issue_id, issue_text(issue_id))
        self.init_git()
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        (self.repo / "unrelated.txt").write_text("ahead\n", encoding="utf-8")
        subprocess.run(["git", "add", "unrelated.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=self.repo, check=True)
        subprocess.run(["git", "branch", "upstream"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD~1"], cwd=self.repo, check=True)
        path.write_text(issue_text(issue_id).replace("opened.", "rewritten."), encoding="utf-8")
        joined = "\n".join(
            finding.message
            for finding in issues.audit_issues(
                self.repo, issues.load_issues(self.repo), date(2026, 7, 30), "upstream"
            )
        )
        self.assertNotIn("cannot verify", joined)
        self.assertIn("changed or were reordered", joined)

    def test_watch_signals_rejects_unknown_ref(self) -> None:
        result = subprocess.run(
            [
                "/bin/sh",
                str(ROOT / "scripts" / "issues"),
                "--config",
                str(self.config),
                "--repo",
                str(self.repo),
                "watch-signals",
                "--ref",
                "missing-ref",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not resolve", result.stderr)


if __name__ == "__main__":
    unittest.main()
