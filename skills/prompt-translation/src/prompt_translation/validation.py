"""Validate selected prompt-pair outputs against their configured sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

REPO_DIR = SOURCE_ROOT = OUTPUT_ROOT = None
GENERATED_MARKER = config.DEFAULT_MARKER


def configure(settings):
    global REPO_DIR, SOURCE_ROOT, OUTPUT_ROOT, GENERATED_MARKER
    global PIPELINE_REVISION, MIN_CHARS, MAX_CHARS, MIN_CHINESE_RATIO
    REPO_DIR = settings["repository_root"]
    SOURCE_ROOT = settings["source_directory"]
    OUTPUT_ROOT = settings["output_directory"]
    GENERATED_MARKER = settings["generated_marker"]
    PIPELINE_REVISION = settings["pipeline_revision"]
    MIN_CHARS = settings["filters"]["min_chars"]
    MAX_CHARS = settings["filters"]["max_chars"]
    MIN_CHINESE_RATIO = settings["filters"]["min_chinese_ratio"]


def allowed_source(source):
    prefix = SOURCE_ROOT.relative_to(REPO_DIR).as_posix() + "/"
    return (
        isinstance(source, str)
        and source.startswith(prefix)
        and ".." not in source.split("/")
        and source.endswith(".md")
    )


PROMPT_HEADING_RE = re.compile(
    "^### (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}Z)\\s*$", re.MULTILINE
)
DAILY_FILENAME_RE = re.compile("^(\\d{4}-\\d{2}-\\d{2})\\.md$")
LEDGER_RE = re.compile("<!-- prompt-pair-ledger:v2\\n(?P<payload>\\{.*?\\})\\n-->", re.DOTALL)
RECORD_HEADING_RE = re.compile(
    "^## Record (\\d+) — (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}Z)\\s*$", re.MULTILINE
)
LOWER_SHA256_RE = re.compile("[0-9a-f]{64}")
PIPELINE_REVISION = 2
ALLOWED_STATUSES = {"translated", "filtered", "classified_trivial"}
ALLOWED_FILTER_REASONS = {"too_short", "non_chinese"}
MIN_CHARS = 30
MAX_CHARS = 2000
MIN_CHINESE_RATIO = 0.1


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    return dict(re.findall("^([A-Za-z0-9_]+):\\s*(.*?)\\s*$", text[4:end], re.MULTILINE))


def parse_prompts(text: str) -> list[dict[str, str]]:
    prompts: list[dict[str, str]] = []
    matches = list(PROMPT_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            prompts.append({"timestamp": match.group(1), "body": body})
    return prompts


def prompt_slice_sha1(prompts: list[dict[str, str]]) -> str:
    canonical = json.dumps(prompts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


CONVERSATION_DATE_PREFIX_RE = re.compile("^\\d{4}-\\d{2}-\\d{2}_")


def conversation_key(source: str) -> str:
    name = source.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return CONVERSATION_DATE_PREFIX_RE.sub("", name, count=1)


def stable_record_id(conversation: str, source_timestamp: str, occurrence: int) -> str:
    payload = f"{conversation}\x00{source_timestamp}\x00{occurrence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_sort_key(record: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        record["source_timestamp"],
        conversation_key(record["source"]),
        record["occurrence"],
        record["record_id"],
    )


def canonical_day_input_sha256(records: list[dict[str, Any]]) -> str:
    ordered = sorted(records, key=record_sort_key)
    projection = [
        {
            "record_id": record["record_id"],
            "source_timestamp": record["source_timestamp"],
            "occurrence": record["occurrence"],
            "input_sha256": record["input_sha256"],
        }
        for record in ordered
    ]
    canonical = json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quoted_block(section: str, marker: str, end_marker: str | None = None) -> str:
    if marker not in section:
        return ""
    block = section.split(marker, 1)[1]
    if end_marker and end_marker in block:
        block = block.split(end_marker, 1)[0]
    lines = []
    for line in block.splitlines():
        if line.startswith("> "):
            lines.append(line[2:])
        elif line == ">":
            lines.append("")
    return "\n".join(lines).strip()


def _valid_utc_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _valid_prompt_timestamp(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%SZ")
    except ValueError:
        return False
    return True


def _parse_nonnegative_counts(
    fields: dict[str, str], total_field: str, translated_field: str, errors: list[str]
) -> tuple[int, int]:
    try:
        total = int(fields.get(total_field, ""))
        translated = int(fields.get(translated_field, ""))
        if total < 0 or translated < 0 or translated > total:
            raise ValueError
    except ValueError:
        errors.append(
            f"{total_field}/{translated_field} must be nonnegative integers with translated <= total"
        )
        return (-1, -1)
    return (total, translated)


def _parse_nonnegative_count(fields: dict[str, str], field: str, errors: list[str]) -> int:
    try:
        value = int(fields.get(field, ""))
        if value < 0:
            raise ValueError
    except ValueError:
        errors.append(f"{field} must be a nonnegative integer")
        return -1
    return value


def _chinese_ratio(text: str) -> float:
    if not text:
        return 0.0
    chinese = sum((1 for character in text if "一" <= character <= "鿿"))
    return chinese / len(text)


def _daily_source_record_index() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    records_by_date: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    try:
        source_root = SOURCE_ROOT.resolve(strict=True)
    except OSError:
        return ({}, [f"source root is missing: {SOURCE_ROOT}"])
    sources = sorted(
        (
            path
            for path in SOURCE_ROOT.rglob("*.md")
            if path.name != "README.md" and re.match("^\\d{4}-\\d{2}-\\d{2}_", path.name)
        )
    )
    for source_path in sources:
        try:
            resolved = source_path.resolve(strict=True)
            resolved.relative_to(source_root)
            source = resolved.relative_to(REPO_DIR.resolve()).as_posix()
            prompts = parse_prompts(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"could not read raw prompt source {source_path}: {exc}")
            continue
        occurrences: Counter[str] = Counter()
        for prompt in prompts:
            timestamp = prompt["timestamp"]
            occurrence = occurrences[timestamp]
            occurrences[timestamp] += 1
            body = prompt["body"].strip()
            records_by_date.setdefault(timestamp[:10], []).append(
                {
                    "record_id": stable_record_id(conversation_key(source), timestamp, occurrence),
                    "source": source,
                    "source_timestamp": timestamp,
                    "occurrence": occurrence,
                    "input_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "body": body,
                }
            )
    for records in records_by_date.values():
        records.sort(key=record_sort_key)
    return (records_by_date, errors)


def _expected_daily_records(prompt_date: str) -> tuple[list[dict[str, Any]], list[str]]:
    records_by_date, errors = _daily_source_record_index()
    return (records_by_date.get(prompt_date, []), errors)


def _parse_ledger(text: str, errors: list[str]) -> dict[str, Any] | None:
    matches = list(LEDGER_RE.finditer(text))
    if len(matches) != 1:
        errors.append(f"expected exactly one prompt-pair-ledger:v2 marker, found {len(matches)}")
        return None
    try:
        ledger = json.loads(matches[0].group("payload"))
    except json.JSONDecodeError as exc:
        errors.append(f"prompt-pair ledger is not valid JSON: {exc}")
        return None
    if not isinstance(ledger, dict):
        errors.append("prompt-pair ledger must be a JSON object")
        return None
    return ledger


def _single_metadata(
    section: str, label: str, pattern: str, block_index: int, errors: list[str]
) -> str:
    values = re.findall(pattern, section, re.MULTILINE)
    if len(values) != 1:
        errors.append(f"record block {block_index} must contain exactly one {label} line")
        return ""
    return values[0]


def _parse_record_blocks(text: str, errors: list[str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    matches = list(RECORD_HEADING_RE.finditer(text))
    for index, match in enumerate(matches, start=1):
        end = matches[index].start() if index < len(matches) else len(text)
        section = text[match.end() : end]
        if int(match.group(1)) != index:
            errors.append(
                f"record block numbering must be contiguous from 1; found {match.group(1)} at block {index}"
            )
        source = _single_metadata(section, "Source", "^- Source: `([^`]+)`\\s*$", index, errors)
        record_id = _single_metadata(
            section, "Record ID", "^- Record ID: `([0-9a-f]{64})`\\s*$", index, errors
        )
        input_sha256 = _single_metadata(
            section, "Input SHA-256", "^- Input SHA-256: `([0-9a-f]{64})`\\s*$", index, errors
        )
        blocks.append(
            {
                "number": match.group(1),
                "source_timestamp": match.group(2),
                "source": source,
                "record_id": record_id,
                "input_sha256": input_sha256,
                "chinese": quoted_block(section, "**中文:**", "**English:**"),
                "english": quoted_block(section, "**English:**", "\n---"),
            }
        )
    return blocks


def _normalized_markdown_source(text: str) -> str:
    return "\n".join((line.rstrip() for line in text.strip().splitlines())).strip()


def validate_daily(
    path: Path,
    text: str,
    fields: dict[str, str],
    source_record_index: dict[str, list[dict[str, Any]]] | None = None,
    source_index_errors: list[str] | None = None,
    *,
    allow_source_ahead: bool = False,
) -> list[str]:
    errors: list[str] = []
    filename_match = DAILY_FILENAME_RE.fullmatch(path.name)
    filename_date = filename_match.group(1) if filename_match else ""
    for field in (
        "title",
        "schema_version",
        "pipeline_revision",
        "prompt_date",
        "day_input_sha256",
        "record_count_total",
        "record_count_translated",
        "record_count_filtered",
        "record_count_classified_trivial",
        "filter_dropped_short",
        "filter_dropped_non_chinese",
        "filter_truncated",
        "translated_at",
        "model",
        "classify_model",
    ):
        if not fields.get(field):
            errors.append(f"frontmatter missing {field}")
    if fields.get("schema_version") != "2":
        errors.append("schema_version must be 2")
    if fields.get("pipeline_revision") != str(PIPELINE_REVISION):
        errors.append(f"pipeline_revision must be {PIPELINE_REVISION}")
    prompt_date = fields.get("prompt_date", "")
    try:
        parsed_date = datetime.strptime(prompt_date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        parsed_date = ""
        errors.append("prompt_date must be a real YYYY-MM-DD date")
    if not filename_match:
        errors.append("schema v2 output filename must be YYYY-MM-DD.md")
    elif parsed_date and filename_date != parsed_date:
        errors.append(f"prompt_date {prompt_date} does not match filename date {filename_date}")
    declared_day_hash = fields.get("day_input_sha256", "")
    if not LOWER_SHA256_RE.fullmatch(declared_day_hash):
        errors.append("day_input_sha256 must be 64 lowercase hex characters")
    total_count, translated_count = _parse_nonnegative_counts(
        fields, "record_count_total", "record_count_translated", errors
    )
    filtered_count = _parse_nonnegative_count(fields, "record_count_filtered", errors)
    classified_trivial_count = _parse_nonnegative_count(
        fields, "record_count_classified_trivial", errors
    )
    dropped_short_count = _parse_nonnegative_count(fields, "filter_dropped_short", errors)
    dropped_non_chinese_count = _parse_nonnegative_count(
        fields, "filter_dropped_non_chinese", errors
    )
    truncated_count = _parse_nonnegative_count(fields, "filter_truncated", errors)
    if not _valid_utc_timestamp(fields.get("translated_at", "")):
        errors.append("translated_at must be a real UTC ISO timestamp")
    ledger = _parse_ledger(text, errors)
    if ledger is None:
        return errors
    if ledger.get("schema_version") != 2:
        errors.append("ledger schema_version must be 2")
    if ledger.get("pipeline_revision") != PIPELINE_REVISION:
        errors.append(f"ledger pipeline_revision must be {PIPELINE_REVISION}")
    if ledger.get("prompt_date") != prompt_date:
        errors.append("ledger prompt_date does not match frontmatter")
    if ledger.get("day_input_sha256") != declared_day_hash:
        errors.append("ledger day_input_sha256 does not match frontmatter")
    raw_records = ledger.get("records")
    if not isinstance(raw_records, list):
        errors.append("ledger records must be a JSON array")
        return errors
    records: list[dict[str, Any]] = []
    record_ids: list[str] = []
    source_keys: list[tuple[str, str, int]] = []
    for index, raw_record in enumerate(raw_records, start=1):
        label = f"ledger record {index}"
        if not isinstance(raw_record, dict):
            errors.append(f"{label} must be a JSON object")
            continue
        record = raw_record
        records.append(record)
        record_id = record.get("record_id")
        source = record.get("source")
        timestamp = record.get("source_timestamp")
        occurrence = record.get("occurrence")
        input_sha256 = record.get("input_sha256")
        status = record.get("status")
        if not isinstance(record_id, str) or not LOWER_SHA256_RE.fullmatch(record_id):
            errors.append(f"{label} record_id must be 64 lowercase hex characters")
        else:
            record_ids.append(record_id)
        if (
            not isinstance(source, str)
            or not allowed_source(source)
            or ".." in source.split("/")
            or (not source.endswith(".md"))
        ):
            errors.append(f"{label} source must be under the configured source directory")
        if not isinstance(timestamp, str) or not _valid_prompt_timestamp(timestamp):
            errors.append(f"{label} source_timestamp must be a real UTC prompt timestamp")
        elif parsed_date and (not timestamp.startswith(f"{prompt_date} ")):
            errors.append(f"{label} source_timestamp is outside prompt_date")
        if type(occurrence) is not int or occurrence < 0:
            errors.append(f"{label} occurrence must be a nonnegative integer")
        if not isinstance(input_sha256, str) or not LOWER_SHA256_RE.fullmatch(input_sha256):
            errors.append(f"{label} input_sha256 must be 64 lowercase hex characters")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{label} status must be one of {sorted(ALLOWED_STATUSES)}")
        elif status == "filtered":
            if record.get("filter_reason") not in ALLOWED_FILTER_REASONS:
                errors.append(
                    f"{label} filtered record needs filter_reason in {sorted(ALLOWED_FILTER_REASONS)}"
                )
        elif status == "translated":
            if not isinstance(record.get("english"), str) or not record["english"].strip():
                errors.append(f"{label} translated record needs non-empty english")
        if (
            status in {"translated", "classified_trivial"}
            and type(record.get("truncated")) is not bool
        ):
            errors.append(f"{label} {status} record needs boolean truncated")
        elif "truncated" in record and type(record["truncated"]) is not bool:
            errors.append(f"{label} truncated must be boolean when present")
        if isinstance(source, str) and isinstance(timestamp, str) and (type(occurrence) is int):
            source_keys.append((source, timestamp, occurrence))
            expected_id = stable_record_id(conversation_key(source), timestamp, occurrence)
            if isinstance(record_id, str) and record_id != expected_id:
                errors.append(
                    f"{label} record_id does not match source/timestamp/occurrence: expected {expected_id}"
                )
    duplicate_ids = sorted(
        (record_id for record_id, count in Counter(record_ids).items() if count > 1)
    )
    if duplicate_ids:
        errors.append(f"ledger record_id values are not unique: {', '.join(duplicate_ids)}")
    duplicate_keys = sorted((key for key, count in Counter(source_keys).items() if count > 1))
    if duplicate_keys:
        errors.append(f"ledger source/timestamp/occurrence keys are not unique: {duplicate_keys}")
    if parsed_date and source_record_index is None:
        expected_records, source_errors = _expected_daily_records(prompt_date)
    elif parsed_date:
        expected_records = source_record_index.get(prompt_date, [])
        source_errors = source_index_errors or []
    else:
        expected_records, source_errors = ([], [])
    errors.extend(source_errors)

    def identity_key(record: dict[str, Any]) -> tuple[str, str, int] | None:
        if not (
            isinstance(record.get("source"), str)
            and isinstance(record.get("source_timestamp"), str)
            and (type(record.get("occurrence")) is int)
        ):
            return None
        return (
            conversation_key(record["source"]),
            record["source_timestamp"],
            record["occurrence"],
        )

    expected_by_key = {identity_key(record): record for record in expected_records}
    actual_by_key = {key: record for record in records if (key := identity_key(record)) is not None}
    missing = sorted(set(expected_by_key) - set(actual_by_key))
    unexpected = sorted(set(actual_by_key) - set(expected_by_key), key=repr)
    if missing and (not allow_source_ahead):
        errors.append(f"ledger is missing {len(missing)} raw prompt record(s): {missing[:3]}")
    if unexpected and (not allow_source_ahead):
        errors.append(
            f"ledger has {len(unexpected)} record(s) not present in the raw day slice: {unexpected[:3]}"
        )
    for key in sorted(set(expected_by_key) & set(actual_by_key)):
        expected = expected_by_key[key]
        actual = actual_by_key[key]
        if actual.get("record_id") != expected["record_id"]:
            errors.append(f"ledger record {key} has the wrong record_id")
        if actual.get("input_sha256") != expected["input_sha256"]:
            if allow_source_ahead:
                continue
            errors.append(
                f"ledger record {key} input_sha256 does not match raw body: expected {expected['input_sha256']}"
            )
        body = expected["body"]
        expected_filter_reason = None
        if len(body) < MIN_CHARS:
            expected_filter_reason = "too_short"
        elif _chinese_ratio(body) < MIN_CHINESE_RATIO:
            expected_filter_reason = "non_chinese"
        if expected_filter_reason is not None:
            if actual.get("status") != "filtered":
                errors.append(
                    f"ledger record {key} must be filtered by the deterministic {expected_filter_reason} rule"
                )
            elif actual.get("filter_reason") != expected_filter_reason:
                errors.append(f"ledger record {key} filter_reason must be {expected_filter_reason}")
        elif actual.get("status") == "filtered":
            errors.append(
                f"ledger record {key} passed deterministic filters but is marked filtered"
            )
        if actual.get("status") in {"translated", "classified_trivial"}:
            expected_truncated = len(body) > MAX_CHARS
            if actual.get("truncated") is not expected_truncated:
                errors.append(
                    f"ledger record {key} truncated must be {str(expected_truncated).lower()} for the raw body length"
                )
    if total_count >= 0 and total_count != len(records):
        errors.append(
            f"ledger record count {len(records)} does not match record_count_total {total_count}"
        )
    actual_translated_count = sum(
        (
            1
            for record in records
            if isinstance(record, dict) and record.get("status") == "translated"
        )
    )
    if translated_count >= 0 and translated_count != actual_translated_count:
        errors.append(
            f"translated ledger count {actual_translated_count} does not match record_count_translated {translated_count}"
        )
    actual_filtered_count = sum((record.get("status") == "filtered" for record in records))
    actual_classified_trivial_count = sum(
        (record.get("status") == "classified_trivial" for record in records)
    )
    actual_dropped_short_count = sum(
        (
            record.get("status") == "filtered" and record.get("filter_reason") == "too_short"
            for record in records
        )
    )
    actual_dropped_non_chinese_count = sum(
        (
            record.get("status") == "filtered" and record.get("filter_reason") == "non_chinese"
            for record in records
        )
    )
    actual_truncated_count = sum((record.get("truncated") is True for record in records))
    count_checks = (
        ("record_count_filtered", filtered_count, actual_filtered_count),
        (
            "record_count_classified_trivial",
            classified_trivial_count,
            actual_classified_trivial_count,
        ),
        ("filter_dropped_short", dropped_short_count, actual_dropped_short_count),
        ("filter_dropped_non_chinese", dropped_non_chinese_count, actual_dropped_non_chinese_count),
        ("filter_truncated", truncated_count, actual_truncated_count),
    )
    for field, declared, actual in count_checks:
        if declared >= 0 and declared != actual:
            errors.append(f"{field} {declared} does not match ledger count {actual}")
    if all(
        (
            isinstance(record.get("source"), str)
            and isinstance(record.get("source_timestamp"), str)
            and (type(record.get("occurrence")) is int)
            and isinstance(record.get("record_id"), str)
            and LOWER_SHA256_RE.fullmatch(record["record_id"])
            and isinstance(record.get("input_sha256"), str)
            and LOWER_SHA256_RE.fullmatch(record["input_sha256"])
            for record in records
        )
    ):
        actual_day_hash = canonical_day_input_sha256(records)
        if declared_day_hash != actual_day_hash:
            errors.append(
                f"day_input_sha256 does not match ledger projection: expected {actual_day_hash}"
            )
    blocks = _parse_record_blocks(text, errors)
    if translated_count >= 0 and len(blocks) != translated_count:
        errors.append(
            f"record block count {len(blocks)} does not match record_count_translated {translated_count}"
        )
    translated_by_id = {
        record.get("record_id"): record
        for record in records
        if record.get("status") == "translated" and isinstance(record.get("record_id"), str)
    }
    block_ids = [block["record_id"] for block in blocks if block["record_id"]]
    duplicate_block_ids = sorted(
        (record_id for record_id, count in Counter(block_ids).items() if count > 1)
    )
    if duplicate_block_ids:
        errors.append(f"translated record blocks are not unique: {', '.join(duplicate_block_ids)}")
    missing_blocks = sorted(set(translated_by_id) - set(block_ids))
    extra_blocks = sorted(set(block_ids) - set(translated_by_id))
    if missing_blocks:
        errors.append(f"translated ledger records missing body blocks: {', '.join(missing_blocks)}")
    if extra_blocks:
        errors.append(f"body blocks without translated ledger records: {', '.join(extra_blocks)}")
    for index, block in enumerate(blocks, start=1):
        record = translated_by_id.get(block["record_id"])
        if record is None:
            continue
        for field in ("source", "source_timestamp", "input_sha256"):
            if block[field] != record.get(field):
                errors.append(f"record block {index} {field} does not match its ledger record")
        if not block["chinese"]:
            errors.append(f"record block {index} has an empty Chinese block")
        if not block["english"]:
            errors.append(f"record block {index} has an empty English block")
        elif _normalized_markdown_source(block["english"]) != _normalized_markdown_source(
            str(record.get("english", ""))
        ):
            errors.append(f"record block {index} English does not match its ledger record")
        key = identity_key(record)
        expected = expected_by_key.get(key)
        if (
            expected
            and block["chinese"]
            and (
                not allow_source_ahead or record.get("input_sha256") == expected.get("input_sha256")
            )
        ):
            displayed = _normalized_markdown_source(block["chinese"])
            original = _normalized_markdown_source(expected["body"])
            if not record.get("truncated") and displayed != original:
                errors.append(f"record block {index} Chinese does not match the raw prompt body")
            elif record.get("truncated") and (
                not original.startswith(displayed.replace("\n\n[…truncated]", ""))
            ):
                errors.append(
                    f"record block {index} truncated Chinese is not a prefix of the raw prompt body"
                )
    return errors


def validate_legacy(path: Path, text: str, fields: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for field in (
        "title",
        "source",
        "prompt_count_total",
        "prompt_count_translated",
        "translated_at",
        "model",
    ):
        if not fields.get(field):
            errors.append(f"frontmatter missing {field}")
    _total_count, translated_count = _parse_nonnegative_counts(
        fields, "prompt_count_total", "prompt_count_translated", errors
    )
    if not _valid_utc_timestamp(fields.get("translated_at", "")):
        errors.append("translated_at must be a real UTC ISO timestamp")
    source_rel = fields.get("source", "")
    source = REPO_DIR / source_rel
    if not allowed_source(source_rel) or ".." in source_rel.split("/"):
        errors.append(f"source is outside the configured source directory: {source_rel}")
    elif not source.is_file():
        pass
    else:
        slice_date = fields.get("prompt_slice_date")
        slice_hash = fields.get("prompt_slice_sha1")
        source_hash = fields.get("source_sha1")
        if slice_date or slice_hash:
            try:
                datetime.strptime(slice_date or "", "%Y-%m-%d")
            except ValueError:
                errors.append("prompt_slice_date must be YYYY-MM-DD")
            if not re.fullmatch("[0-9a-f]{40}", slice_hash or ""):
                errors.append("prompt_slice_sha1 must be 40 lowercase hex characters")
            else:
                prompts = [
                    prompt
                    for prompt in parse_prompts(source.read_text(encoding="utf-8"))
                    if prompt["timestamp"].startswith(f"{slice_date} ")
                ]
                actual_hash = prompt_slice_sha1(prompts)
                if not prompts:
                    errors.append("source has no prompts on prompt_slice_date")
                elif slice_hash != actual_hash:
                    errors.append(
                        f"prompt_slice_sha1 does not match source slice: expected {actual_hash}"
                    )
        elif not re.fullmatch("[0-9a-f]{40}", source_hash or ""):
            errors.append("source_sha1 must be 40 lowercase hex characters")
        else:
            actual_hash = hashlib.sha1(source.read_bytes()).hexdigest()
            if source_hash != actual_hash:
                errors.append(f"source_sha1 does not match source: expected {actual_hash}")
    sections = text.split("\n## Prompt ")[1:]
    if translated_count >= 0 and len(sections) != translated_count:
        errors.append(
            f"prompt block count {len(sections)} does not match prompt_count_translated {translated_count}"
        )
    for index, section in enumerate(sections, start=1):
        chinese = quoted_block(section, "**中文:**", "**English:**")
        english = quoted_block(section, "**English:**", "\n---")
        if not chinese:
            errors.append(f"prompt {index} has an empty Chinese block")
        if not english:
            errors.append(f"prompt {index} has an empty English block")
    return errors


def output_path_errors(path: Path) -> list[str]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(OUTPUT_ROOT.resolve())
    except (OSError, ValueError):
        return ["path is missing or outside the configured output directory"]
    if path.is_symlink() or not path.is_file():
        return ["output must be a regular file, not a symlink"]
    return []


def validate(
    path: Path,
    source_record_index: dict[str, list[dict[str, Any]]] | None = None,
    source_index_errors: list[str] | None = None,
    *,
    allow_source_ahead: bool = False,
    legacy_source_only: bool = False,
) -> list[str]:
    errors = output_path_errors(path)
    if errors:
        return errors
    text = path.read_text(encoding="utf-8")
    fields = parse_frontmatter(text)
    daily = (
        fields.get("schema_version") == "2" or DAILY_FILENAME_RE.fullmatch(path.name) is not None
    )
    if legacy_source_only:
        if not text.startswith("---\n"):
            return ["missing YAML frontmatter"]
        if not daily:
            source = fields.get("source", "")
            if not source:
                return ["frontmatter missing source"]
            if not allowed_source(source):
                return ["source is outside the configured source directory"]
            return []
    if re.search("[ \\t]+$", text, re.MULTILINE):
        errors.append("output contains trailing whitespace")
    if GENERATED_MARKER not in text:
        errors.append("missing generated-file marker")
    errors.extend(
        validate_daily(
            path,
            text,
            fields,
            source_record_index=source_record_index,
            source_index_errors=source_index_errors,
            allow_source_ahead=allow_source_ahead,
        )
        if daily
        else validate_legacy(path, text, fields)
    )
    return errors


def arguments(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=config.default_config())
    parser.add_argument("--root")
    parser.add_argument(
        "--allow-source-ahead",
        action="store_true",
        help="allow new raw prompt records that the asynchronous derived ledger has not processed yet",
    )
    parser.add_argument(
        "--scan-output",
        action="store_true",
        help="validate immediate Markdown files in the configured output directory, excluding README.md",
    )
    parser.add_argument(
        "--legacy-source-only",
        action="store_true",
        help="inspect only provenance references in legacy files; daily ledgers remain fully validated",
    )
    parser.add_argument("--format", choices=["text", "tsv"], default="text")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.scan_output == bool(args.paths):
        parser.error("select either --scan-output or explicit output paths")
    return args


def report_error(message: str, output_format: str, prefix: str = "[ERROR]") -> None:
    if output_format == "tsv":
        print("ERROR\t" + message.replace("\n", " ").replace("\r", " ").replace("\t", " "))
    else:
        print(f"{prefix} {message}", file=sys.stderr)


def run_validation(args) -> int:
    configure(config.load(args.config, root=args.root))
    if args.scan_output and not OUTPUT_ROOT.is_dir():
        raise config.ConfigurationError("output-directory-missing-or-not-directory")
    paths = (
        sorted(path for path in OUTPUT_ROOT.glob("*.md") if path.name != "README.md")
        if args.scan_output
        else args.paths
    )
    daily_paths = []
    for path in paths:
        if output_path_errors(path):
            continue
        try:
            fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            fields = {}
        if fields.get("schema_version") == "2" or DAILY_FILENAME_RE.fullmatch(path.name):
            daily_paths.append(path)
    source_record_index, source_index_errors = (
        _daily_source_record_index() if daily_paths else (None, None)
    )
    failed = False
    for path in paths:
        errors = validate(
            path,
            source_record_index=source_record_index,
            source_index_errors=source_index_errors,
            allow_source_ahead=args.allow_source_ahead,
            legacy_source_only=args.legacy_source_only,
        )
        for error in errors:
            report_error(f"{path}: {error}", args.format)
        failed = failed or bool(errors)
    return 1 if failed else 0


def main(argv=None):
    args = arguments(argv)
    try:
        return run_validation(args)
    except config.ConfigurationError as error:
        report_error(str(error), args.format, "FAIL")
        return 1
    except Exception:
        report_error("prompt-pair validation could not complete", args.format, "FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
