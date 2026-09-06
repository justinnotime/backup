import tempfile
import unittest
from pathlib import Path

from activity_summary import validation as MODULE

FACTS_HASH = "a" * 64
COVERAGE_FACTS = {
    "gh_touched_today": {"1234": {"title": "today"}, "2345": {"title": "also source-active today"}},
    "commits": [{"kind": "content", "issues": ["3456"]}],
    "session_clusters": [
        {"kind": "human", "time": "09:00–09:30Z", "n_real_prompts": 2},
        {"kind": "machine", "time": "10:00–10:01Z"},
    ],
}
MULTIREPO_FACTS = {
    "gh_touched_today": {
        "example-org/alpha#90": {"repo": "example-org/alpha", "number": "90"},
        "example-org/storage#90": {"repo": "example-org/storage", "number": "90"},
        "example-org/storage-bench#11": {"repo": "example-org/storage-bench", "number": "11"},
    },
    "commits": [{"kind": "content", "issue_refs": ["example-org/storage#92"], "issues": ["92"]}],
    "session_clusters": [],
}


def valid_summary(
    overall: str = "Open item:正式使用前仍缺最终验证；今天把技术工作整理成了可继续推进的路线。",
) -> str:
    filler = "这是可追溯的事实说明，用于确保样例不是空壳。" * 40
    return f"---\ntitle: Daily summary 2026-07-19\ntype: summary\ncreated: 2026-07-19\ndate: 2026-07-19\nupdated: 2026-07-20T01:20:00Z\nwindow: 2026-07-17..2026-07-19\ngenerator: daily-summary\nfacts_sha256: {FACTS_HASH}\nsources: deterministic activity facts and referenced local mirrors\n---\n\n# 2026-07-19\n\n## Facts\n\n### PRs / Issues\n- [alpha#1234](https://github.com/example-org/alpha/issues/1234) open — today\n- [alpha#2345](https://github.com/example-org/alpha/pull/2345) merged — continuity\n\n{filler}\n\n### Agent work\n\n09:00 用户推动了多条工作线，并明确了约束和验收标准。\n\n## Projects\n\n项目按长期主题归并，保留了下一步。\n\n## Commentary\n\n{overall}\n\n## Next\n\n- 继续验证尚未收口的工作。\n"


def multirepo_summary() -> str:
    old = "- [alpha#1234](https://github.com/example-org/alpha/issues/1234) open — today\n- [alpha#2345](https://github.com/example-org/alpha/pull/2345) merged — continuity"
    new = "- [alpha#90](https://github.com/example-org/alpha/issues/90) open — same number, different repo\n- [storage#90](https://github.com/example-org/storage/pull/90) merged — overlay index\n- [storage-bench#11](https://github.com/example-org/storage-bench/issues/11) open — benchmark result"
    return valid_summary().replace(old, new)


class DailySummaryValidationTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        with handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def test_valid_summary_passes(self):
        self.assertEqual(MODULE.validate(self.write(valid_summary()), "2026-07-19", FACTS_HASH), [])

    def test_valid_summary_passes_deterministic_coverage_gate(self):
        self.assertEqual(
            MODULE.validate(self.write(valid_summary()), "2026-07-19", FACTS_HASH, COVERAGE_FACTS),
            [],
        )

    def test_rejects_missing_or_misordered_issue_closed_set(self):
        missing = valid_summary().replace(
            "- [alpha#2345](https://github.com/example-org/alpha/pull/2345) merged — continuity\n",
            "",
        )
        errors = MODULE.validate(self.write(missing), "2026-07-19", FACTS_HASH, COVERAGE_FACTS)
        self.assertTrue(any(("deterministic closed set" in error for error in errors)))
        misordered = valid_summary().replace(
            "- [alpha#1234](https://github.com/example-org/alpha/issues/1234) open — today\n- [alpha#2345](https://github.com/example-org/alpha/pull/2345) merged — continuity",
            "- [alpha#2345](https://github.com/example-org/alpha/pull/2345) merged — continuity\n- [alpha#1234](https://github.com/example-org/alpha/issues/1234) open — today",
        )
        errors = MODULE.validate(self.write(misordered), "2026-07-19", FACTS_HASH, COVERAGE_FACTS)
        self.assertTrue(any(("deterministic closed set" in error for error in errors)))

    def test_multirepo_identity_keeps_collisions_and_two_digit_numbers(self):
        self.assertEqual(
            MODULE.validate(
                self.write(multirepo_summary()), "2026-07-19", FACTS_HASH, MULTIREPO_FACTS
            ),
            [],
        )

    def test_multirepo_rejects_wrong_repo_and_ambiguous_visible_label(self):
        misordered = multirepo_summary().replace(
            "- [storage#90](https://github.com/example-org/storage/pull/90) merged — overlay index\n- [storage-bench#11](https://github.com/example-org/storage-bench/issues/11) open — benchmark result",
            "- [storage-bench#11](https://github.com/example-org/storage-bench/issues/11) open — benchmark result\n- [storage#90](https://github.com/example-org/storage/pull/90) merged — overlay index",
        )
        errors = MODULE.validate(self.write(misordered), "2026-07-19", FACTS_HASH, MULTIREPO_FACTS)
        self.assertTrue(any(("deterministic closed set" in error for error in errors)))
        content_only = multirepo_summary().replace(
            "- [storage-bench#11]",
            "- [storage#92](https://github.com/example-org/storage/pull/92) merged — context only\n- [storage-bench#11]",
        )
        errors = MODULE.validate(
            self.write(content_only), "2026-07-19", FACTS_HASH, MULTIREPO_FACTS
        )
        self.assertTrue(any(("deterministic closed set" in error for error in errors)))
        wrong_repo = multirepo_summary().replace(
            "https://github.com/example-org/storage/pull/90",
            "https://github.com/example-org/storage-bench/pull/90",
        )
        errors = MODULE.validate(self.write(wrong_repo), "2026-07-19", FACTS_HASH, MULTIREPO_FACTS)
        self.assertTrue(any(("deterministic closed set" in error for error in errors)))
        bare_label = multirepo_summary().replace("[storage#90]", "[#90]")
        errors = MODULE.validate(self.write(bare_label), "2026-07-19", FACTS_HASH, MULTIREPO_FACTS)
        self.assertTrue(any(("repository-qualified" in error for error in errors)))

    def test_global_gate_rejects_external_markdown_and_bare_github_urls(self):
        external_markdown = valid_summary().replace(
            "项目按长期主题归并，保留了下一步。",
            "项目提到 [alpha#3456](https://github.com/example-org/alpha/pull/3456)。",
        )
        errors = MODULE.validate(
            self.write(external_markdown), "2026-07-19", FACTS_HASH, COVERAGE_FACTS
        )
        self.assertTrue(any(("URLs in the summary" in error for error in errors)))
        bare_url = valid_summary().replace(
            "项目按长期主题归并，保留了下一步。",
            "项目提到 https://github.com/example-org/storage/issues/92#issuecomment-1。",
        )
        errors = MODULE.validate(self.write(bare_url), "2026-07-19", FACTS_HASH, COVERAGE_FACTS)
        self.assertTrue(any(("URLs in the summary" in error for error in errors)))

    def test_global_gate_rejects_external_qualified_tokens_but_not_bare_numbers(self):
        external_tokens = valid_summary().replace(
            "项目按长期主题归并，保留了下一步。",
            "项目承接 [alpha#3456]、example-org/storage#92 与 storage#92；裸 #777 保留。",
        )
        errors = MODULE.validate(
            self.write(external_tokens), "2026-07-19", FACTS_HASH, COVERAGE_FACTS
        )
        self.assertTrue(any(("qualified GitHub" in error for error in errors)))
        bare_only = (
            valid_summary()
            .replace("项目按长期主题归并，保留了下一步。", "项目按长期主题归并，保留普通裸 #777。")
            .replace("open — today", "open — today; mirror title mentions storage#92")
        )
        self.assertEqual(
            MODULE.validate(self.write(bare_only), "2026-07-19", FACTS_HASH, COVERAGE_FACTS), []
        )

    def test_global_gate_allows_strict_refs_to_repeat_in_narrative(self):
        repeated = valid_summary().replace(
            "项目按长期主题归并，保留了下一步。",
            "项目重复 [alpha#1234](https://github.com/example-org/alpha/issues/1234)，并写作 example-org/alpha#2345 与 alpha#1234。",
        )
        self.assertEqual(
            MODULE.validate(self.write(repeated), "2026-07-19", FACTS_HASH, COVERAGE_FACTS), []
        )

    def test_rejects_missing_human_cluster_and_markdown_wrapper(self):
        text = valid_summary().replace("09:00 ", "").rstrip() + "\n</markdown>\n"
        errors = MODULE.validate(self.write(text), "2026-07-19", FACTS_HASH, COVERAGE_FACTS)
        self.assertTrue(any(("missing human cluster" in error for error in errors)))
        self.assertIn("summary must not contain markdown wrapper tags", errors)

    def test_rejects_issue_number_in_overall_commentary(self):
        errors = MODULE.validate(
            self.write(valid_summary("今天围绕 #12345 完成了关键转折。")), "2026-07-19", FACTS_HASH
        )
        self.assertIn("commentary must not contain issue/PR numbers", errors)

    def test_overall_commentary_must_lead_with_biggest_unfinished_outcome(self):
        errors = MODULE.validate(
            self.write(valid_summary("今天完成了很多工作，但没有说明最终结果还缺什么。")),
            "2026-07-19",
            FACTS_HASH,
        )
        self.assertIn("commentary opening does not match the configured pattern", errors)
        self.assertEqual(
            MODULE.validate(
                self.write(valid_summary("No open items:当天范围内的目标均有完成证据。")),
                "2026-07-19",
                FACTS_HASH,
            ),
            [],
        )

    def test_rejects_wrong_facts_hash(self):
        errors = MODULE.validate(self.write(valid_summary()), "2026-07-19", "b" * 64)
        self.assertTrue(any(("facts_sha256" in error for error in errors)))

    def test_rejects_inexact_provenance_and_invalid_timestamp(self):
        text = (
            valid_summary()
            .replace("title: Daily summary 2026-07-19", "title: wrong title")
            .replace(
                "sources: deterministic activity facts and referenced local mirrors",
                "sources: invented",
            )
            .replace("updated: 2026-07-20T01:20:00Z", "updated: 2026-99-99T99:99:99Z")
        )
        errors = MODULE.validate(self.write(text), "2026-07-19", FACTS_HASH)
        self.assertTrue(any(("title" in error for error in errors)))
        self.assertTrue(any(("sources" in error for error in errors)))
        self.assertIn("frontmatter updated must be a UTC ISO timestamp", errors)


if __name__ == "__main__":
    unittest.main()
