import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from prompt_translation import translate as MODULE


def source_text(*timestamps: str) -> str:
    blocks = ["---", "source: test", "---", ""]
    for timestamp in timestamps:
        blocks.extend(
            [
                f"### {timestamp}",
                "",
                "这是一个足够长、用于验证日期选择和状态恢复逻辑的中文测试 prompt。",
                "",
            ]
        )
    return "\n".join(blocks)


class TranslateRawPromptsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source_dir = self.root / "sources" / "prompts"
        self.output_dir = self.root / "learning" / "pairs"
        self.source_dir.mkdir(parents=True)
        self.output_dir.mkdir(parents=True)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "schema": "prompt-translation/v1",
                    "repository_root": str(self.root),
                    "source_directory": "sources/prompts",
                    "output_directory": "learning/pairs",
                    "cheatsheet_path": "learning/cheatsheet.md",
                    "models": {"translate": "test-translator", "classify": "test-classifier"},
                    "api": {
                        "base_url": "https://api.example.invalid",
                        "credential": {"environment": "GSK_API_KEY", "kind": "gsk"},
                        "required": True,
                    },
                }
            )
        )
        self.patches = [
            mock.patch.dict(os.environ, {"PROMPT_TRANSLATION_CONFIG": str(self.config_path)}),
            mock.patch.object(MODULE, "REPO_DIR", self.root),
            mock.patch.object(MODULE, "SOURCE_DIR", self.source_dir),
            mock.patch.object(MODULE, "OUTPUT_DIR", self.output_dir),
            mock.patch.object(MODULE, "STATE_FILE", self.output_dir / ".translate_state.json"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tempdir.cleanup()

    def write_source(self, name: str, *timestamps: str) -> Path:
        path = self.source_dir / "2026-07" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_text(*timestamps), encoding="utf-8")
        return path

    @staticmethod
    def classifier_response(text: str):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    def test_classifier_bisects_batch_on_length_mismatch(self):
        client = mock.Mock()
        client.messages.create.side_effect = [
            self.classifier_response("[1, 0, 1, 0, 1, 0]"),
            self.classifier_response("[1, 0, 1]"),
            self.classifier_response("[0, 1, 0, 1]"),
        ]
        flags = MODULE.classify_substantive(
            client, [f"prompt {index}" for index in range(7)], "test-classifier"
        )
        self.assertEqual(flags, [True, False, True, False, True, False, True])
        self.assertEqual(client.messages.create.call_count, 3)

    def test_classifier_bisects_batch_on_non_json(self):
        client = mock.Mock()
        client.messages.create.side_effect = [
            self.classifier_response("not json"),
            self.classifier_response("[1]"),
            self.classifier_response("[0]"),
        ]
        flags = MODULE.classify_substantive(
            client, ["first prompt", "second prompt"], "test-classifier"
        )
        self.assertEqual(flags, [True, False])
        self.assertEqual(client.messages.create.call_count, 3)

    def test_classifier_tolerates_prose_after_json(self):
        client = mock.Mock()
        client.messages.create.side_effect = [
            self.classifier_response("[1]\n```\n\nThis is substantive. The prompt articulates:"),
            self.classifier_response("[0]\n\nThe prompt is a single-line command/instruction."),
        ]
        flags = MODULE.classify_substantive(client, ["first prompt"], "test-classifier")
        self.assertEqual(flags, [True])
        flags = MODULE.classify_substantive(client, ["second prompt"], "test-classifier")
        self.assertEqual(flags, [False])
        self.assertEqual(client.messages.create.call_count, 2)

    def test_classifier_single_item_malformed_response_is_hard_error(self):
        client = mock.Mock()
        client.messages.create.return_value = self.classifier_response("[]")
        with self.assertRaisesRegex(RuntimeError, "expected array of 1"):
            MODULE.classify_substantive(client, ["only prompt"], "test-classifier")
        client.messages.create.assert_called_once()

    def test_exact_date_slices_cross_day_session(self):
        first = self.write_source(
            "2026-07-18_first.md", "2026-07-18 23:00:00Z", "2026-07-19 01:00:00Z"
        )
        second = self.write_source("2026-07-20_second.md", "2026-07-20 01:00:00Z")
        third = self.write_source("2026-07-19_third.md", "2026-07-19 02:00:00Z")
        units = MODULE.build_work_units([first, second, third], exact_date="2026-07-19")
        self.assertEqual(len(units), 1)
        self.assertIsNone(units[0]["source"])
        self.assertEqual(
            [prompt["timestamp"] for prompt in units[0]["prompts"]],
            ["2026-07-19 01:00:00Z", "2026-07-19 02:00:00Z"],
        )
        self.assertEqual(units[0]["output_path"].name, "2026-07-19.md")

    def test_backlog_through_date_excludes_partial_current_day(self):
        source = self.write_source(
            "2026-07-20_cross_day.md", "2026-07-19 23:00:00Z", "2026-07-20 01:00:00Z"
        )
        units = MODULE.build_work_units(
            [source], since_date="2026-07-19", through_date="2026-07-19", oldest_first=True
        )
        self.assertEqual([unit["prompt_date"] for unit in units], ["2026-07-19"])

    def test_through_date_requires_valid_since_range(self):
        source = self.write_source("2026-07-19_range.md", "2026-07-19 01:00:00Z")
        with self.assertRaisesRegex(ValueError, "requires --since-date"):
            MODULE.build_work_units([source], through_date="2026-07-19")
        with self.assertRaisesRegex(ValueError, "on or after"):
            MODULE.build_work_units([source], since_date="2026-07-20", through_date="2026-07-19")

    def test_rendered_multiline_quotes_have_no_trailing_whitespace(self):
        source = self.write_source("2026-07-19_render.md", "2026-07-19 01:00:00Z")
        stats = {"input": 1, "too_short": 0, "non_chinese": 0, "truncated": 0}
        rendered = MODULE.render_pair_md(
            source,
            "a" * 40,
            [
                {
                    "timestamp": "2026-07-19 01:00:00Z",
                    "body": "第一行\n\n第三行  ",
                    "english": "First line\n\nThird line  ",
                }
            ],
            "test-model",
            stats,
            {"kept": 1, "dropped": 0},
            "2026-07-19",
        )
        self.assertIn("\n>\n", rendered)
        self.assertFalse(any((line.endswith((" ", "\t")) for line in rendered.splitlines())))

    def test_committed_frontmatter_recovers_state(self):
        source = self.write_source("2026-07-19_state.md", "2026-07-19 01:00:00Z")
        text = source.read_text(encoding="utf-8")
        source_hash = MODULE.source_sha1(text)
        stats = {"input": 1, "too_short": 0, "non_chinese": 0, "truncated": 0}
        rendered = MODULE.render_pair_md(source, source_hash, [], "test-model", stats, None)
        (self.output_dir / source.name).write_text(rendered, encoding="utf-8")
        state = MODULE.load_state()
        self.assertEqual(state[MODULE.source_state_key(source)]["sourceSha1"], source_hash)

    def test_output_state_overrides_stale_local_cache(self):
        source = self.write_source("2026-07-19_override.md", "2026-07-19 01:00:00Z")
        source_hash = MODULE.source_sha1(source.read_text(encoding="utf-8"))
        key = MODULE.source_state_key(source)
        MODULE.STATE_FILE.write_text(
            f'''{{"{key}": {{"sourceSha1": "{"0" * 40}"}}}}''', encoding="utf-8"
        )
        stats = {"input": 1, "too_short": 0, "non_chinese": 0, "truncated": 0}
        (self.output_dir / source.name).write_text(
            MODULE.render_pair_md(source, source_hash, [], "test-model", stats, None),
            encoding="utf-8",
        )
        self.assertEqual(MODULE.load_state()[key]["sourceSha1"], source_hash)

    def test_daily_slice_frontmatter_recovers_slice_state(self):
        source = self.write_source(
            "2026-07-18_slice.md", "2026-07-18 23:00:00Z", "2026-07-19 01:00:00Z"
        )
        unit = MODULE.build_work_units([source], exact_date="2026-07-19")[0]
        record = unit["records"][0]
        ledger_record = MODULE.ledger_record_base(record) | {
            "status": "translated",
            "truncated": False,
            "english": "This is an English translation.",
            "model": "test-model",
        }
        rendered = MODULE.render_daily_pair_md(
            "2026-07-19",
            unit["input_hash"],
            [record],
            [ledger_record],
            "test-model",
            "test-classifier",
        )
        unit["output_path"].write_text(rendered, encoding="utf-8")
        state = MODULE.load_state()
        self.assertEqual(state[unit["state_key"]]["dayInputSha256"], unit["input_hash"])
        self.assertEqual(state[unit["state_key"]]["pipelineRevision"], MODULE.PIPELINE_REVISION)
        self.assertEqual(state[unit["state_key"]]["records"], [ledger_record])

    def test_record_identity_is_stable_across_body_edits_and_disambiguates_timestamp(self):
        source = self.write_source(
            "2026-07-19_duplicate.md", "2026-07-19 01:00:00Z", "2026-07-19 01:00:00Z"
        )
        first_records = MODULE.prompt_records([source])
        key = MODULE.conversation_key(source.relative_to(self.root).as_posix())
        self.assertEqual(key, "duplicate")
        expected_first = hashlib.sha256(
            "\x00".join((key, first_records[0]["source_timestamp"], "0")).encode()
        ).hexdigest()
        expected_second = hashlib.sha256(
            "\x00".join((key, first_records[1]["source_timestamp"], "1")).encode()
        ).hexdigest()
        self.assertEqual(
            [record["record_id"] for record in first_records], [expected_first, expected_second]
        )
        text = source.read_text(encoding="utf-8").replace(
            "中文测试 prompt", "中文测试 prompt，内容已经改变"
        )
        source.write_text(text, encoding="utf-8")
        edited_records = MODULE.prompt_records([source])
        self.assertEqual(
            [record["record_id"] for record in edited_records],
            [record["record_id"] for record in first_records],
        )
        self.assertNotEqual(edited_records[0]["input_sha256"], first_records[0]["input_sha256"])

    def test_day_hash_uses_canonical_sorted_projection(self):
        first = self.write_source("2026-07-19_z.md", "2026-07-19 02:00:00Z")
        second = self.write_source("2026-07-19_a.md", "2026-07-19 01:00:00Z")
        records = MODULE.prompt_records([first, second])
        ordered = sorted(records, key=MODULE.record_sort_key)
        projection = [
            {
                "record_id": record["record_id"],
                "source_timestamp": record["source_timestamp"],
                "occurrence": record["occurrence"],
                "input_sha256": record["input_sha256"],
            }
            for record in ordered
        ]
        expected = hashlib.sha256(
            json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(MODULE.day_input_sha256(list(reversed(records))), expected)
        self.assertEqual(
            [record["source_timestamp"] for record in ordered],
            ["2026-07-19 01:00:00Z", "2026-07-19 02:00:00Z"],
        )

    def test_record_identity_and_day_hash_survive_a_day_split_of_the_conversation(self):
        whole = self.write_source(
            "2026-07-18_abcd1234.md", "2026-07-18 23:50:00Z", "2026-07-19 01:00:00Z"
        )
        whole_records = MODULE.prompt_records([whole])
        whole.unlink()
        day_one = self.write_source("2026-07-18_abcd1234.md", "2026-07-18 23:50:00Z")
        day_two = self.write_source("2026-07-19_abcd1234.md", "2026-07-19 01:00:00Z")
        split_records = MODULE.prompt_records([day_one, day_two])
        self.assertEqual(
            [record["record_id"] for record in whole_records],
            [record["record_id"] for record in split_records],
        )
        self.assertEqual(
            MODULE.day_input_sha256(whole_records), MODULE.day_input_sha256(split_records)
        )
        self.assertEqual(
            MODULE.conversation_key("sources/prompts/2026-08/2026-08-01_sbKta2mi-opencode.md"),
            "sbKta2mi-opencode",
        )
        self.assertEqual(
            MODULE.conversation_key("sources/prompts/2026-08/2026-08-21_2a6f2c7f--03eed9b67469.md"),
            "2a6f2c7f--03eed9b67469",
        )

    def test_daily_body_only_contains_translated_records_and_full_chinese(self):
        source = self.write_source("2026-07-19_render_daily.md", "2026-07-19 01:00:00Z")
        record = MODULE.prompt_records([source])[0]
        full_body = "长" * (MODULE.MAX_CHARS + 20)
        record["body"] = full_body
        record["input_sha256"] = MODULE.sha256_text(full_body)
        translated = MODULE.ledger_record_base(record) | {
            "status": "translated",
            "truncated": True,
            "english": "A translated record.",
            "model": "test-model",
        }
        filtered = {
            "record_id": "f" * 64,
            "source": "sources/prompts/2026-07/filtered.md",
            "source_timestamp": "2026-07-19 02:00:00Z",
            "occurrence": 0,
            "input_sha256": "e" * 64,
            "status": "filtered",
            "filter_reason": "too_short",
        }
        rendered = MODULE.render_daily_pair_md(
            "2026-07-19",
            "d" * 64,
            [record],
            [translated, filtered],
            "test-model",
            "test-classifier",
        )
        self.assertEqual(rendered.count("## Record "), 1)
        self.assertNotIn("no substantive prompts", rendered)
        self.assertIn(MODULE.markdown_quote(full_body), rendered)
        self.assertIn("prompt-pair-ledger:v2", rendered)
        self.assertFalse(any((line.endswith((" ", "\t")) for line in rendered.splitlines())))

    def test_fresh_worktree_reuses_all_record_statuses(self):
        source = self.write_source("2026-07-19_reuse.md", "2026-07-19 01:00:00Z")
        record = MODULE.prompt_records([source])[0]
        old = MODULE.ledger_record_base(record) | {
            "status": "translated",
            "truncated": False,
            "english": "Reused English.",
            "model": "test-model",
        }
        previous = {"pipelineRevision": MODULE.PIPELINE_REVISION, "records": [old]}
        client = mock.Mock()
        rows, stats = MODULE.process_daily_records(
            client,
            [record],
            previous,
            model="test-model",
            classify_model="test-classifier",
            batch_size=8,
            classify_batch_size=20,
            no_classify=False,
        )
        self.assertEqual(rows, [old])
        self.assertEqual(stats["reused"], 1)
        client.messages.create.assert_not_called()

    def test_pipeline_revision_is_part_of_whole_day_skip(self):
        source = self.write_source("2026-07-19_revision.md", "2026-07-19 01:00:00Z")
        unit = MODULE.build_work_units([source], exact_date="2026-07-19")[0]
        unit["output_path"].write_text("existing", encoding="utf-8")
        state = {
            unit["state_key"]: {
                "dayInputSha256": unit["input_hash"],
                "pipelineRevision": MODULE.PIPELINE_REVISION - 1,
            }
        }
        selected, skipped, _ = MODULE.select_work_units([unit], state)
        self.assertEqual(selected, [unit])
        self.assertEqual(skipped, 0)

    def test_cleanup_removes_only_legacy_slices_for_day(self):
        legacy = self.output_dir / "session--2026-07-19.md"
        other = self.output_dir / "session--2026-07-18.md"
        daily = self.output_dir / "2026-07-19.md"
        for path in (legacy, other, daily):
            path.write_text("test", encoding="utf-8")
        self.assertEqual(MODULE.cleanup_legacy_daily_slices("2026-07-19"), 1)
        self.assertFalse(legacy.exists())
        self.assertTrue(other.exists())
        self.assertTrue(daily.exists())

    def test_daily_llm_error_does_not_replace_existing_day(self):
        self.write_source("2026-07-19_atomic.md", "2026-07-19 01:00:00Z")
        output = self.output_dir / "2026-07-19.md"
        output.write_text("sentinel existing day\n", encoding="utf-8")
        with (
            mock.patch.object(sys, "argv", ["translate", "--date", "2026-07-19", "--strict"]),
            mock.patch.object(MODULE, "make_client", return_value=(object(), "test")),
            mock.patch.object(
                MODULE, "classify_substantive", side_effect=RuntimeError("test failure")
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = MODULE.main()
        self.assertEqual(result, 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "sentinel existing day\n")

    def test_limit_applies_after_unchanged_filter(self):
        newest = self.write_source("2026-07-20_newest.md", "2026-07-20 01:00:00Z")
        older = self.write_source("2026-07-19_older.md", "2026-07-19 01:00:00Z")
        newest_hash = MODULE.source_sha1(newest.read_text(encoding="utf-8"))
        (self.output_dir / newest.name).write_text("already translated", encoding="utf-8")
        state = {MODULE.source_state_key(newest): {"sourceSha1": newest_hash}}
        selected, skipped = MODULE.select_work_sources([newest, older], state, limit=1)
        self.assertEqual(selected, [older])
        self.assertEqual(skipped, 1)

    def test_configured_kind_never_uses_unselected_environment(self):
        calls = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        MODULE.configure(MODULE.config.load(self.config_path))
        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with (
            mock.patch.dict(sys.modules, {"anthropic": fake_module}),
            mock.patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "UNSELECTED_PLACEHOLDER",
                    "GSK_API_KEY": "SELECTED_PLACEHOLDER",
                },
                clear=True,
            ),
        ):
            _client, label = MODULE.make_client("gsk")
        self.assertEqual(label, "gsk")
        self.assertEqual(calls[0]["api_key"], "SELECTED_PLACEHOLDER")
        self.assertEqual(calls[0]["base_url"], "https://api.example.invalid")
        self.assertFalse(calls[0]["http_client"].follow_redirects)
        calls[0]["http_client"].close()


if __name__ == "__main__":
    unittest.main()
