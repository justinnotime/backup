import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prompt_translation import translate as TRANSLATE
from prompt_translation import validation as VALIDATOR

SOURCE_TEXT = "---\nsource: test\n---\n\n### 2026-07-19 01:00:00Z\n\n这是一个足够长、可以进入英语学习翻译流程的中文复杂 prompt。\n\n### 2026-07-19 01:00:00Z\n\n继续\n\n### 2026-07-20 01:00:00Z\n\n这是另一天的 prompt，不应该进入七月十九日的 ledger。\n"


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def markdown_quote(value: str) -> str:
    return "\n".join(
        (">" if not line else f"> {line.rstrip()}" for line in value.strip().splitlines())
    )


class PromptPairValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_root = self.root / "sources" / "prompts"
        self.output_root = self.root / "learning" / "pairs"
        self.source = self.source_root / "2026-07" / "2026-07-19_test.md"
        self.source.parent.mkdir(parents=True)
        self.output_root.mkdir(parents=True)
        self.source.write_text(SOURCE_TEXT, encoding="utf-8")
        self.source_rel = self.source.relative_to(self.root).as_posix()
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
                        "credential": {"environment": "SYNTHETIC_TRANSLATION_KEY"},
                        "required": False,
                    },
                }
            )
        )
        self.patches = [
            mock.patch.dict(os.environ, {"PROMPT_TRANSLATION_CONFIG": str(self.config_path)}),
            mock.patch.object(TRANSLATE, "REPO_DIR", self.root),
            mock.patch.object(TRANSLATE, "SOURCE_DIR", self.source_root),
            mock.patch.object(TRANSLATE, "OUTPUT_DIR", self.output_root),
            mock.patch.object(VALIDATOR, "REPO_DIR", self.root),
            mock.patch.object(VALIDATOR, "SOURCE_ROOT", self.source_root),
            mock.patch.object(VALIDATOR, "OUTPUT_ROOT", self.output_root),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def records(self):
        timestamp = "2026-07-19 01:00:00Z"
        chinese = "这是一个足够长、可以进入英语学习翻译流程的中文复杂 prompt。"
        first = {
            "record_id": sha256(
                f"{VALIDATOR.conversation_key(self.source_rel)}\x00{timestamp}\x00{0}"
            ),
            "source": self.source_rel,
            "source_timestamp": timestamp,
            "occurrence": 0,
            "input_sha256": sha256(chinese),
            "status": "translated",
            "english": "This is a sufficiently complex Chinese prompt for the English-learning workflow.",
            "truncated": False,
        }
        second = {
            "record_id": sha256(
                f"{VALIDATOR.conversation_key(self.source_rel)}\x00{timestamp}\x00{1}"
            ),
            "source": self.source_rel,
            "source_timestamp": timestamp,
            "occurrence": 1,
            "input_sha256": sha256("继续"),
            "status": "filtered",
            "filter_reason": "too_short",
        }
        return [first, second]

    def day_hash(self, records):
        ordered = sorted(
            records,
            key=lambda record: (
                record["source_timestamp"],
                VALIDATOR.conversation_key(record["source"]),
                record["occurrence"],
                record["record_id"],
            ),
        )
        projection = [
            {
                "record_id": record["record_id"],
                "source_timestamp": record["source_timestamp"],
                "occurrence": record["occurrence"],
                "input_sha256": record["input_sha256"],
            }
            for record in ordered
        ]
        return sha256(json.dumps(projection, ensure_ascii=False, separators=(",", ":")))

    def render_daily(self, records=None, *, filename="2026-07-19.md", blocks=None, day_hash=None):
        records = copy.deepcopy(self.records() if records is None else records)
        day_hash = day_hash or self.day_hash(records)
        translated = [record for record in records if record.get("status") == "translated"]
        filtered = [record for record in records if record.get("status") == "filtered"]
        classified_trivial = [
            record for record in records if record.get("status") == "classified_trivial"
        ]
        ledger = {
            "schema_version": 2,
            "prompt_date": "2026-07-19",
            "day_input_sha256": day_hash,
            "pipeline_revision": 2,
            "records": records,
        }
        if blocks is None:
            rendered = []
            for number, record in enumerate(translated, start=1):
                rendered.extend(
                    [
                        f"## Record {number} — {record['source_timestamp']}",
                        "",
                        f"- Source: `{record['source']}`",
                        f"- Record ID: `{record['record_id']}`",
                        f"- Input SHA-256: `{record['input_sha256']}`",
                        "",
                        "**中文:**",
                        "",
                        markdown_quote(
                            "这是一个足够长、可以进入英语学习翻译流程的中文复杂 prompt。"
                        ),
                        "",
                        "**English:**",
                        "",
                        markdown_quote(record["english"]),
                        "",
                        "---",
                        "",
                    ]
                )
            blocks = "\n".join(rendered).rstrip()
        text = (
            "\n".join(
                [
                    "---",
                    "title: Prompt translations for 2026-07-19",
                    "schema_version: 2",
                    "prompt_date: 2026-07-19",
                    f"day_input_sha256: {day_hash}",
                    "pipeline_revision: 2",
                    f"record_count_total: {len(records)}",
                    f"record_count_translated: {len(translated)}",
                    f"record_count_filtered: {len(filtered)}",
                    f"record_count_classified_trivial: {len(classified_trivial)}",
                    f"filter_dropped_short: {sum((record.get('filter_reason') == 'too_short' for record in filtered))}",
                    f"filter_dropped_non_chinese: {sum((record.get('filter_reason') == 'non_chinese' for record in filtered))}",
                    f"filter_truncated: {sum((record.get('truncated') is True for record in records))}",
                    "translated_at: 2026-07-21T01:02:03Z",
                    "model: test-model",
                    "classify_model: test-classifier",
                    "---",
                    "",
                    "<!-- Generated by prompt-translation/scripts/translate. Do not hand-edit; -->",
                    "",
                    "# Prompt pairs for 2026-07-19 UTC",
                    "",
                    "<!-- prompt-pair-ledger:v2",
                    json.dumps(ledger, ensure_ascii=False, separators=(",", ":")),
                    "-->",
                    "",
                    blocks,
                ]
            ).rstrip()
            + "\n"
        )
        output = self.output_root / filename
        output.write_text(text, encoding="utf-8")
        return output

    def test_valid_daily_record_file_passes(self):
        self.assertEqual(VALIDATOR.validate(self.render_daily()), [])

    def test_translator_daily_renderer_passes_validator(self):
        source_records = [
            record
            for record in TRANSLATE.prompt_records([self.source])
            if record["source_timestamp"].startswith("2026-07-19 ")
        ]
        translated = TRANSLATE.ledger_record_base(source_records[0]) | {
            "status": "translated",
            "english": "A renderer-to-validator integration translation.",
            "truncated": False,
            "model": "test-model",
        }
        filtered = TRANSLATE.ledger_record_base(source_records[1]) | {
            "status": "filtered",
            "filter_reason": "too_short",
        }
        day_hash = TRANSLATE.day_input_sha256(source_records)
        rendered = TRANSLATE.render_daily_pair_md(
            "2026-07-19",
            day_hash,
            source_records,
            [translated, filtered],
            "test-model",
            "test-classifier",
        )
        output = self.output_root / "2026-07-19.md"
        output.write_text(rendered, encoding="utf-8")
        self.assertEqual(VALIDATOR.validate(output), [])

    def test_cli_batch_scans_raw_sources_once_for_multiple_daily_paths(self):
        output = self.render_daily()
        with (
            mock.patch.object(
                VALIDATOR, "_daily_source_record_index", wraps=VALIDATOR._daily_source_record_index
            ) as scan,
            mock.patch.object(sys, "argv", ["validate-prompt-pairs.py", str(output), str(output)]),
        ):
            self.assertEqual(VALIDATOR.main(), 0)
        scan.assert_called_once_with()

    def test_daily_hash_and_input_hash_detect_source_change(self):
        output = self.render_daily()
        self.source.write_text(SOURCE_TEXT.replace("中文复杂", "中文而且复杂"), encoding="utf-8")
        errors = VALIDATOR.validate(output)
        self.assertTrue(any(("input_sha256 does not match raw body" in error for error in errors)))

    def test_stable_id_excludes_body_but_checks_occurrence(self):
        records = self.records()
        records[1]["occurrence"] = 0
        output = self.render_daily(records)
        errors = VALIDATOR.validate(output)
        self.assertTrue(any(("not unique" in error for error in errors)))
        self.assertTrue(any(("record_id does not match" in error for error in errors)))

    def test_ledger_requires_exact_raw_day_coverage(self):
        records = self.records()[:1]
        errors = VALIDATOR.validate(self.render_daily(records))
        self.assertTrue(any(("ledger is missing 1 raw prompt record" in error for error in errors)))

    def test_source_ahead_mode_allows_only_unprocessed_new_records(self):
        output = self.render_daily()
        self.source.write_text(
            SOURCE_TEXT
            + "\n### 2026-07-19 02:00:00Z\n\n"
            + "这是稍后到达、尚未进入异步学习记录的新提示。\n",
            encoding="utf-8",
        )
        strict_errors = VALIDATOR.validate(output)
        self.assertTrue(
            any(("ledger is missing 1 raw prompt record" in error for error in strict_errors))
        )
        self.assertEqual(VALIDATOR.validate(output, allow_source_ahead=True), [])
        self.source.write_text(
            self.source.read_text(encoding="utf-8").replace("中文复杂", "中文已改变"),
            encoding="utf-8",
        )
        self.assertTrue(
            any(
                (
                    "input_sha256 does not match raw body" in error
                    for error in VALIDATOR.validate(output)
                )
            )
        )
        self.assertEqual(VALIDATOR.validate(output, allow_source_ahead=True), [])
        self.source.unlink()
        self.assertTrue(
            any(
                (
                    "not present in the raw day slice" in error
                    for error in VALIDATOR.validate(output)
                )
            )
        )
        self.assertEqual(VALIDATOR.validate(output, allow_source_ahead=True), [])

    def test_ledger_survives_the_prompt_file_being_renamed_or_day_split(self):
        output = self.render_daily()
        self.assertEqual(VALIDATOR.validate(output), [])
        renamed = self.source.parent / "2026-07-18_test.md"
        self.source.rename(renamed)
        self.assertEqual(VALIDATOR.validate(output), [])
        text = renamed.read_text(encoding="utf-8")
        head, tail = text.split("### 2026-07-20 01:00:00Z")
        renamed.write_text(head.rstrip() + "\n", encoding="utf-8")
        (self.source.parent / "2026-07-20_test.md").write_text(
            "---\nsource: test\n---\n### 2026-07-20 01:00:00Z" + tail, encoding="utf-8"
        )
        self.assertEqual(VALIDATOR.validate(output), [])

    def test_shared_prompt_layout_is_read_by_the_work_parser(self):
        shared_output = "# Prompts — synthetic\n\n- Managed-By: agent-session-extraction/v1\n- Schema: agent-session/v1\n- View: prompts\n- Tool: codex\n- Host: synthetic-node\n- Session: synthetic-session\n- Source: source/session.jsonl\n- Project: synthetic-project\n\n---\n\n### 2026-07-19 01:00:00Z\n\n这是一条用于验证共享渲染格式的合成提示。\n\n---\n"
        parsed = TRANSLATE.parse_prompts(shared_output)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["timestamp"], "2026-07-19 01:00:00Z")
        self.assertTrue(parsed[0]["body"].startswith("这是一条"))

    def test_ledger_rejects_source_outside_raw_root(self):
        records = self.records()
        records[0]["source"] = "Wiki/not-a-raw-source.md"
        errors = VALIDATOR.validate(self.render_daily(records))
        self.assertTrue(
            any(
                (
                    "source must be under the configured source directory" in error
                    for error in errors
                )
            )
        )

    def test_prompt_date_must_match_daily_filename(self):
        errors = VALIDATOR.validate(self.render_daily(filename="2026-07-18.md"))
        self.assertTrue(any(("does not match filename date" in error for error in errors)))

    def test_translated_ledger_record_requires_exactly_one_block(self):
        errors = VALIDATOR.validate(self.render_daily(blocks=""))
        self.assertTrue(any(("record block count 0" in error for error in errors)))
        self.assertTrue(any(("missing body blocks" in error for error in errors)))

    def test_block_metadata_and_english_must_match_ledger(self):
        output = self.render_daily()
        text = output.read_text(encoding="utf-8").replace(
            "> This is a sufficiently complex Chinese prompt for the English-learning workflow.",
            "> A different translation.",
        )
        output.write_text(text, encoding="utf-8")
        self.assertTrue(
            any(("English does not match" in error for error in VALIDATOR.validate(output)))
        )

    def test_all_status_reason_and_truncation_counts_are_reconciled(self):
        output = self.render_daily()
        output.write_text(
            output.read_text(encoding="utf-8").replace(
                "record_count_filtered: 1", "record_count_filtered: 0"
            ),
            encoding="utf-8",
        )
        self.assertIn(
            "record_count_filtered 0 does not match ledger count 1", VALIDATOR.validate(output)
        )

    def test_deterministic_filter_decision_is_audited_against_raw_body(self):
        records = self.records()
        records[1]["filter_reason"] = "non_chinese"
        errors = VALIDATOR.validate(self.render_daily(records))
        self.assertTrue(any(("filter_reason must be too_short" in error for error in errors)))

    def test_substantive_raw_body_cannot_be_marked_deterministically_filtered(self):
        records = self.records()
        records[0] = {
            key: value
            for key, value in records[0].items()
            if key not in {"english", "truncated", "model"}
        }
        records[0].update({"status": "filtered", "filter_reason": "too_short"})
        errors = VALIDATOR.validate(self.render_daily(records))
        self.assertTrue(
            any(
                ("passed deterministic filters but is marked filtered" in error for error in errors)
            )
        )

    def test_trailing_whitespace_fails(self):
        output = self.render_daily()
        output.write_text(
            output.read_text(encoding="utf-8").replace(
                "> This is a sufficiently complex Chinese prompt for the English-learning workflow.",
                "> This is a sufficiently complex Chinese prompt for the English-learning workflow. ",
            ),
            encoding="utf-8",
        )
        self.assertIn("output contains trailing whitespace", VALIDATOR.validate(output))

    def test_legacy_daily_slice_schema_still_passes(self):
        prompts = [
            prompt
            for prompt in VALIDATOR.parse_prompts(self.source.read_text(encoding="utf-8"))
            if prompt["timestamp"].startswith("2026-07-19 ")
        ]
        slice_hash = VALIDATOR.prompt_slice_sha1(prompts)
        text = (
            "\n".join(
                [
                    "---",
                    "title: legacy prompt translations",
                    f"source: {self.source_rel}",
                    "prompt_slice_date: 2026-07-19",
                    f"prompt_slice_sha1: {slice_hash}",
                    "prompt_count_total: 2",
                    "prompt_count_translated: 1",
                    "translated_at: 2026-07-21T01:02:03Z",
                    "model: test-model",
                    "---",
                    "",
                    "<!-- Generated by prompt-translation/scripts/translate. Do not hand-edit; -->",
                    "",
                    "## Prompt 1 — 2026-07-19 01:00:00Z",
                    "",
                    "**中文:**",
                    "",
                    "> 这是一个测试 prompt。",
                    "",
                    "**English:**",
                    "",
                    "> This is a test prompt.",
                    "",
                    "---",
                ]
            )
            + "\n"
        )
        output = self.output_root / "2026-07-19_session--2026-07-19.md"
        output.write_text(text, encoding="utf-8")
        self.assertEqual(VALIDATOR.validate(output), [])


if __name__ == "__main__":
    unittest.main()
