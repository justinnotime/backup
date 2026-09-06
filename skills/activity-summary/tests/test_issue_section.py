import tempfile
import unittest
from pathlib import Path

from activity_summary import issue_section as MODULE


class DailySummaryIssueSectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        mirror = self.root / "sources" / "issues" / "example-org_storage"
        mirror.mkdir(parents=True)
        (mirror / "2.md").write_text(
            "---\nrepo: example-org/storage\nnumber: 2\nstate: closed\nmerged: '2026-07-17T01:02:03Z'\ntitle: Fix the reader\ntype: gh-pull-request\nurl: https://github.com/example-org/storage/pull/2\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def facts(self):
        return {
            "gh_touched_today": {
                "example-org/storage#2": {
                    "repo": "example-org/storage",
                    "number": 2,
                    "state": "closed",
                    "merged_at": "2026-07-17T01:02:03Z",
                    "title": "Fix the reader",
                    "url": "https://github.com/example-org/storage/pull/2",
                    "file": "sources/issues/example-org_storage/2.md",
                    "activity_on_target": ["frontmatter:merged"],
                }
            },
            "commits": [
                {
                    "kind": "content",
                    "issue_refs": ["example-org/storage#1", "example-org/storage#2"],
                }
            ],
            "session_clusters": [
                {
                    "kind": "human",
                    "time": "09:00–09:30Z",
                    "n_sessions": 2,
                    "n_real_prompts": 3,
                    "messages": 12,
                },
                {"kind": "machine", "time": "10:00–10:01Z"},
            ],
        }

    def test_render_uses_exact_touched_set_and_excludes_content_refs(self):
        section = MODULE.render_issue_section(self.facts(), self.root)
        self.assertNotIn("storage#1", section)
        self.assertIn("[storage#2](https://github.com/example-org/storage/pull/2) MERGED", section)
        self.assertIn("([src](sources/issues/example-org_storage/2.md))", section)

    def test_render_labels_target_activity_not_historical_merged_state(self):
        facts = self.facts()
        item = facts["gh_touched_today"]["example-org/storage#2"]
        item["activity_on_target"] = ["frontmatter:updated", "comment:created"]
        section = MODULE.render_issue_section(facts, self.root)
        self.assertIn(") commented — Fix the reader", section)
        self.assertNotIn(") MERGED —", section)
        item["activity_on_target"] = ["frontmatter:updated", "frontmatter:closed"]
        self.assertIn(") closed — Fix the reader", MODULE.render_issue_section(facts, self.root))
        item["activity_on_target"] = ["frontmatter:updated"]
        self.assertIn(") updated — Fix the reader", MODULE.render_issue_section(facts, self.root))
        item["activity_on_target"] = ["frontmatter:created", "body:created"]
        self.assertIn(") created — Fix the reader", MODULE.render_issue_section(facts, self.root))

    def test_install_replaces_model_section_and_normalizes_other_labels(self):
        section = MODULE.render_issue_section(self.facts(), self.root)
        generated = "---\ntitle: x\n---\n# x\n## Facts\n### PRs / Issues\n<!-- DAILY_SUMMARY_PR_ISSUES -->\n\n### Docs\n- docs\n\n### Agent work\n- [#2](https://github.com/example-org/storage/pull/2)\n"
        installed = MODULE.install_issue_section(generated, section)
        self.assertNotIn("DAILY_SUMMARY_PR_ISSUES", installed)
        self.assertEqual(
            installed.count("[storage#2](https://github.com/example-org/storage/pull/2)"), 2
        )
        self.assertIn("### Docs\n- docs", installed)

    def test_install_can_insert_missing_subsection(self):
        installed = MODULE.install_issue_section(
            "## Facts\n\n### Docs\n- docs\n", "### PRs / Issues\n- item\n"
        )
        self.assertIn("## Facts\n\n### PRs / Issues\n- item", installed)
        self.assertLess(installed.index("### PRs / Issues"), installed.index("### Docs"))

    def test_sanitizer_removes_external_urls_and_qualified_tokens_outside_section(self):
        section = "### PRs / Issues\n- [storage#2](https://github.com/example-org/storage/pull/2) MERGED — title mentions storage#34 and #99\n"
        markdown = f"# sample\n\n{section}\n### Docs\n\n- strict duplicate [storage#2](https://github.com/example-org/storage/pull/2)\n- external [alpha#428](https://github.com/example-org/alpha/pull/428)\n- raw https://github.com/example-org/alpha/issues/437#issuecomment-1\n- bracket token [alpha#428]\n- full token example-org/alpha#428\n- short token storage#1\n- ambiguous bare #777 remains\n"
        sanitized = MODULE.sanitize_external_github_references(markdown, self.facts())
        self.assertEqual(
            MODULE.ISSUE_SECTION_RE.search(sanitized).group(0).rstrip(), section.rstrip()
        )
        self.assertIn("title mentions storage#34 and #99", sanitized)
        self.assertIn("[storage#2](https://github.com/example-org/storage/pull/2)", sanitized)
        self.assertIn("ambiguous bare #777 remains", sanitized)
        self.assertNotIn("428", sanitized)
        self.assertNotIn("437", sanitized)
        self.assertNotIn("storage#1", sanitized)
        self.assertNotIn("[related earlier work]", sanitized)
        self.assertGreaterEqual(sanitized.count("related earlier work"), 5)
        self.assertEqual(
            MODULE.sanitize_external_github_references(sanitized, self.facts()), sanitized
        )

    def test_agent_work_heading_is_canonicalized_without_replacing_content(self):
        generated = "## Facts\n\n### Agent work  ★ HIGH-VALUE SECTION\n\n09:00 保留模型写出的真实内容。\n\n## Projects\n"
        installed = MODULE.install_agent_work_section(generated, self.facts())
        self.assertIn("### Agent work\n\n09:00 保留模型写出的真实内容。", installed)
        self.assertNotIn("HIGH-VALUE", installed)
        self.assertNotIn("Extractor facts fallback", installed)

    def test_existing_agent_work_gets_only_missing_cluster_coverage(self):
        facts = self.facts()
        facts["session_clusters"].insert(
            1,
            {
                "kind": "human",
                "time": "00:01–23:59Z",
                "n_sessions": 7,
                "n_real_prompts": 11,
                "messages": 42,
            },
        )
        generated = "## Facts\n\n### Agent work  ★ HIGH-VALUE SECTION\n\n09:00 模型写出的真实内容必须原样保留。\n\n#### 已有簇的深入说明\n\n这段正文也必须保留。\n\n## Projects\n\n项目内容。\n"
        installed = MODULE.install_agent_work_section(generated, facts)
        self.assertIn("### Agent work", installed)
        self.assertIn("09:00 模型写出的真实内容必须原样保留。", installed)
        self.assertIn("#### 已有簇的深入说明\n\n这段正文也必须保留。", installed)
        self.assertEqual(installed.count("09:00–09:30Z"), 0)
        self.assertEqual(installed.count("00:01–23:59Z — 7 sessions · 11 prompts · 42 msgs"), 1)
        self.assertLess(installed.index("00:01–23:59Z"), installed.index("## Projects"))

    def test_missing_agent_work_gets_facts_only_skeleton_before_projects(self):
        generated = "## Facts\n\n### Docs\n- docs\n\n## Projects\n\n项目内容。\n"
        installed = MODULE.install_agent_work_section(generated, self.facts())
        self.assertIn("### Agent work", installed)
        self.assertIn("09:00–09:30Z — 2 sessions · 3 prompts · 12 msgs", installed)
        self.assertIn("1 automated clusters excluded", installed)
        self.assertLess(installed.index("### Agent work"), installed.index("## Projects"))

    def test_missing_agent_work_with_no_human_cluster_stays_truthful(self):
        facts = {"session_clusters": [{"kind": "machine", "time": "10:00Z"}]}
        installed = MODULE.install_agent_work_section("## Facts\n\n## Projects\n", facts)
        self.assertIn("No human prompts recorded", installed)
        self.assertIn("1 automated clusters excluded", installed)


if __name__ == "__main__":
    unittest.main()
