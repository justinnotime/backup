import unittest

from activity_summary import evaluation as MODULE


class DailySummaryEvalTest(unittest.TestCase):
    def test_agent_work_keeps_level_four_clusters(self):
        summary = "### Agent work\n\n#### 09:00–09:30 翻译工作\n\n用户要求实现 daily prompt translation runner。\n\n#### 11:00–11:30 总结工作\n\n用户要求补跑 daily summary。\n\n### Projects\n\n这里不应被包含。\n"
        body = MODULE.agent_work_body(summary)
        self.assertIn("daily prompt translation", body)
        self.assertIn("daily summary", body)
        self.assertNotIn("这里不应被包含", body)

    def test_agent_work_stops_at_level_two_section(self):
        summary = "### Agent work\n需要包含。\n## Commentary\n不应包含。\n"
        self.assertEqual(MODULE.agent_work_body(summary).strip(), "需要包含。")

    def test_issue_links_preserve_repo_identity_and_two_digit_numbers(self):
        text = "\n[alpha#90](https://github.com/example-org/alpha/issues/90)\n[storage#90](https://github.com/example-org/storage/pull/90)\n[storage-bench#11](https://github.com/example-org/storage-bench/issues/11)\nbare #90 is ambiguous and must not add another identity\n"
        self.assertEqual(
            MODULE.refs_in(text),
            {"example-org/alpha#90", "example-org/storage#90", "example-org/storage-bench#11"},
        )

    def test_issue_ground_truth_is_exact_touched_set_and_context_stays_separate(self):
        facts = {
            "gh_touched_today": {
                "example-org/alpha#90": {"repo": "example-org/alpha", "number": "90"},
                "example-org/storage#90": {"repo": "example-org/storage", "number": "90"},
            },
            "commits": [
                {"kind": "content", "issue_refs": ["example-org/storage#92"]},
                {
                    "kind": "sync",
                    "issue_refs": ["example-org/storage-bench#11"],
                    "issue_refs_in_body": ["example-org/alpha#7"],
                },
            ],
        }
        ground_truth, window_all = MODULE.issue_reference_sets(facts)
        self.assertEqual(ground_truth, {"example-org/alpha#90", "example-org/storage#90"})
        self.assertIn("example-org/storage#92", window_all)
        self.assertIn("example-org/storage-bench#11", window_all)
        self.assertIn("example-org/alpha#7", window_all)
        legacy_gt, legacy_window = MODULE.issue_reference_sets(
            {
                "gh_touched_today": {"1234": {"title": "legacy"}},
                "commits": [{"kind": "content", "issues": ["2345"]}],
            }
        )
        self.assertEqual(legacy_gt, {"example-org/alpha#1234"})
        self.assertEqual(legacy_window, {"example-org/alpha#1234", "example-org/alpha#2345"})


if __name__ == "__main__":
    unittest.main()
