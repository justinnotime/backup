"""LLM calls > 0. Incremental prompt translation with reusable daily ledgers."""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config

REPO_DIR = SOURCE_DIR = OUTPUT_DIR = CHEATSHEET_PATH = STATE_FILE = None
DEFAULT_TRANSLATE_MODEL = DEFAULT_CLASSIFY_MODEL = None
TRANSLATE_SYSTEM_PROMPT = config.DEFAULT_PROMPTS["translate"]
CLASSIFY_SYSTEM_PROMPT = config.DEFAULT_PROMPTS["classify"]
CHEATSHEET_SYSTEM_PROMPT = config.DEFAULT_PROMPTS["cheatsheet"]
GENERATED_MARKER = config.DEFAULT_MARKER
PRICING = config.ZERO_PRICES
SETTINGS = None


def configure(settings):
    global SETTINGS, REPO_DIR, SOURCE_DIR, OUTPUT_DIR, CHEATSHEET_PATH, STATE_FILE
    global TRANSLATE_SYSTEM_PROMPT, CLASSIFY_SYSTEM_PROMPT, CHEATSHEET_SYSTEM_PROMPT
    global DEFAULT_TRANSLATE_MODEL, DEFAULT_CLASSIFY_MODEL, GENERATED_MARKER, PRICING
    global PIPELINE_REVISION, MIN_CHARS, MAX_CHARS, MIN_CHINESE_RATIO
    SETTINGS = settings
    REPO_DIR = settings["repository_root"]
    SOURCE_DIR = settings["source_directory"]
    OUTPUT_DIR = settings["output_directory"]
    CHEATSHEET_PATH = settings["cheatsheet_path"]
    STATE_FILE = settings["state_file"]
    DEFAULT_TRANSLATE_MODEL = settings["models"]["translate"]
    DEFAULT_CLASSIFY_MODEL = settings["models"]["classify"]
    TRANSLATE_SYSTEM_PROMPT = settings["prompts"]["translate"]
    CLASSIFY_SYSTEM_PROMPT = settings["prompts"]["classify"]
    CHEATSHEET_SYSTEM_PROMPT = settings["prompts"]["cheatsheet"]
    GENERATED_MARKER = settings["generated_marker"]
    PRICING = settings.get("pricing") or config.ZERO_PRICES
    PIPELINE_REVISION = settings["pipeline_revision"]
    MIN_CHARS = settings["filters"]["min_chars"]
    MAX_CHARS = settings["filters"]["max_chars"]
    MIN_CHINESE_RATIO = settings["filters"]["min_chinese_ratio"]


def make_client(credential_source="auto"):
    if SETTINGS is None:
        raise config.ConfigurationError("translation-config-required")
    kind = SETTINGS["api"]["credential"].get("kind", "configured")
    selected = "gsk" if credential_source == "gsk-proxy" else credential_source
    configured = "gsk" if kind == "gsk-proxy" else kind
    if selected not in {"auto", "configured", configured}:
        raise config.ConfigurationError("credential-source-does-not-match-config")
    key = config.credential_value(SETTINGS)
    if not key:
        return None, "configured credential is missing"
    import httpx
    from anthropic import Anthropic

    client = Anthropic(
        api_key=key,
        base_url=SETTINGS["api"]["base_url"],
        timeout=SETTINGS["api"]["timeout"],
        max_retries=SETTINGS["api"]["max_retries"],
        http_client=httpx.Client(follow_redirects=False),
    )
    return client, kind


def doctor():
    import importlib.util

    if importlib.util.find_spec("anthropic") is None:
        raise config.ConfigurationError("anthropic-sdk-missing")
    if not config.credential_value(SETTINGS) and SETTINGS["api"]["required"]:
        raise config.ConfigurationError("configured-credential-missing")
    print("OK prompt translation configuration and local dependencies")
    return 0


def safe_output(path):
    if path.is_symlink():
        raise config.ConfigurationError("generated-file-symlink-refused")
    target = path.resolve()
    if target not in {STATE_FILE, CHEATSHEET_PATH} and not target.is_relative_to(
        OUTPUT_DIR.resolve()
    ):
        raise config.ConfigurationError("generated-path-outside-output")
    return path


DEFAULT_BATCH_SIZE = 8
DEFAULT_CLASSIFY_BATCH_SIZE = 20
DAILY_SCHEMA_VERSION = 2
PIPELINE_REVISION = 2
DAILY_LEDGER_PREFIX = "<!-- prompt-pair-ledger:v2\n"
DAILY_LEDGER_SUFFIX = "\n-->"
MIN_CHARS = 30
MAX_CHARS = 2000
MIN_CHINESE_RATIO = 0.1
PROMPT_HEADING_RE = re.compile(
    "^### (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}Z)\\s*$", re.MULTILINE
)
SOURCE_DATE_RE = re.compile("^(\\d{4}-\\d{2}-\\d{2})_")


def discover_sources(source_dir: Path) -> list[Path]:
    """Select timestamped source files without following paths outside the selection."""
    result = []
    for path in source_dir.rglob("*.md"):
        if path.name == "README.md" or not SOURCE_DATE_RE.match(path.name):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(source_dir.resolve()):
            raise config.ConfigurationError("source-path-outside-selected-directory")
        result.append(path)
    return sorted(result)


def parse_prompts(source_text: str) -> list[dict]:
    out: list[dict] = []
    matches = list(PROMPT_HEADING_RE.finditer(source_text))
    for i, m in enumerate(matches):
        ts = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source_text)
        body = source_text[start:end].strip()
        if body:
            out.append({"timestamp": ts, "body": body})
    return out


def parse_date_from_filename(name: str) -> datetime | None:
    m = SOURCE_DATE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def source_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chinese_ratio(text: str) -> float:
    if not text:
        return 0.0
    cn = sum((1 for c in text if "一" <= c <= "鿿"))
    return cn / len(text)


def filter_prompts(prompts: list[dict]) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    stats = {"input": len(prompts), "too_short": 0, "non_chinese": 0, "truncated": 0, "kept": 0}
    for p in prompts:
        body = p["body"]
        if len(body) < MIN_CHARS:
            stats["too_short"] += 1
            continue
        if chinese_ratio(body) < MIN_CHINESE_RATIO:
            stats["non_chinese"] += 1
            continue
        truncated = False
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] + "\n\n[…truncated]"
            truncated = True
            stats["truncated"] += 1
        kept.append(
            {**p, "raw_body": p.get("raw_body", p["body"]), "body": body, "truncated": truncated}
        )
    stats["kept"] = len(kept)
    return (kept, stats)


def classify_substantive(client, prompts: list[str], model: str) -> list[bool]:
    if len(prompts) == 0:
        return []
    try:
        return _classify_substantive_inner(client, prompts, model)
    except RuntimeError as e:
        msg = str(e)
        if len(prompts) <= 1 or ("non-JSON" not in msg and "expected array" not in msg):
            raise
        mid = len(prompts) // 2
        print(
            f"    [retry] classifier batch of {len(prompts)} returned malformed JSON; halving (left {mid}, right {len(prompts) - mid})"
        )
        return classify_substantive(client, prompts[:mid], model) + classify_substantive(
            client, prompts[mid:], model
        )


def _classify_substantive_inner(client, prompts: list[str], model: str) -> list[bool]:
    user_msg = "\n\n".join((f"{i + 1}. {p}" for i, p in enumerate(prompts)))
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=[
            {"type": "text", "text": CLASSIFY_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub("^```(?:json)?\\s*", "", raw)
        raw = re.sub("\\s*```\\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed, end = json.JSONDecoder().raw_decode(raw.lstrip())
            trailer = raw.lstrip()[end:].strip()
            if trailer:
                print(
                    f"    [lenient] classifier appended {len(trailer)} chars of prose after the JSON; ignored"
                )
        except json.JSONDecodeError:
            raise RuntimeError("Classifier returned non-JSON") from None
    if not isinstance(parsed, list) or len(parsed) != len(prompts):
        raise RuntimeError(
            f"Classifier expected array of {len(prompts)}, got len={(len(parsed) if hasattr(parsed, '__len__') else '?')}"
        )
    if any(type(value) is not int or value not in {0, 1} for value in parsed):
        raise RuntimeError("Classifier expected array of 0 or 1 integers")
    return [bool(value) for value in parsed]


def translate_batch(client, prompts: list[str], model: str) -> list[str]:
    if len(prompts) == 0:
        return []
    try:
        return _translate_batch_inner(client, prompts, model)
    except RuntimeError as e:
        msg = str(e)
        if len(prompts) <= 1 or ("non-JSON" not in msg and "expected array" not in msg):
            raise
        mid = len(prompts) // 2
        print(
            f"    [retry] batch of {len(prompts)} failed JSON parse; halving (left {mid}, right {len(prompts) - mid})"
        )
        return translate_batch(client, prompts[:mid], model) + translate_batch(
            client, prompts[mid:], model
        )


def _translate_batch_inner(client, prompts: list[str], model: str) -> list[str]:
    user_msg = "\n\n".join((f"{i + 1}. {p}" for i, p in enumerate(prompts)))
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": TRANSLATE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub("^```(?:json)?\\s*", "", raw)
        raw = re.sub("\\s*```\\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Translator returned non-JSON") from None
    if not isinstance(parsed, list) or len(parsed) != len(prompts):
        raise RuntimeError(
            f"Translator expected array of {len(prompts)}, got len={(len(parsed) if hasattr(parsed, '__len__') else '?')}"
        )
    out: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            for key in ("en", "english", "translation", "value"):
                if key in item:
                    value = item[key]
                    break
            else:
                raise RuntimeError("Translation object has no recognized English field")
        else:
            raise RuntimeError("Translation item must be a string")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Translation item must be a non-empty string")
        out.append(value.strip())
    return out


FRONTMATTER_FIELD_RE = re.compile("^([A-Za-z0-9_]+):\\s*(.*?)\\s*$", re.MULTILINE)
CONVERSATION_DATE_PREFIX_RE = re.compile("^\\d{4}-\\d{2}-\\d{2}_")


def conversation_key(source: str) -> str:
    name = source.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return CONVERSATION_DATE_PREFIX_RE.sub("", name, count=1)


def record_id_for(conversation: str, source_timestamp: str, occurrence: int) -> str:
    return sha256_text(f"{conversation}\x00{source_timestamp}\x00{occurrence}")


def prompt_records(sources: list[Path]) -> list[dict]:
    records: list[dict] = []
    for source_path in sources:
        source = source_path.relative_to(REPO_DIR).as_posix()
        prompts = parse_prompts(source_path.read_text(encoding="utf-8"))
        occurrences: dict[str, int] = {}
        for prompt in prompts:
            source_timestamp = prompt["timestamp"]
            occurrence = occurrences.get(source_timestamp, 0)
            occurrences[source_timestamp] = occurrence + 1
            body = prompt["body"].strip()
            records.append(
                {
                    "record_id": record_id_for(
                        conversation_key(source), source_timestamp, occurrence
                    ),
                    "source": source,
                    "source_timestamp": source_timestamp,
                    "occurrence": occurrence,
                    "input_sha256": sha256_text(body),
                    "timestamp": source_timestamp,
                    "body": body,
                }
            )
    return records


def record_sort_key(record: dict) -> tuple[str, str, int, str]:
    return (
        record["source_timestamp"],
        conversation_key(record["source"]),
        int(record["occurrence"]),
        record["record_id"],
    )


def day_input_sha256(records: list[dict]) -> str:
    projection = [
        {
            "record_id": record["record_id"],
            "source_timestamp": record["source_timestamp"],
            "occurrence": int(record["occurrence"]),
            "input_sha256": record["input_sha256"],
        }
        for record in sorted(records, key=record_sort_key)
    ]
    canonical = json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(canonical)


def daily_state_key(prompt_date: str) -> str:
    return f"daily:{prompt_date}"


def daily_output_path(prompt_date: str) -> Path:
    return OUTPUT_DIR / f"{prompt_date}.md"


def source_state_key(source_path: Path, prompt_date: str | None = None) -> str:
    key = source_path.relative_to(SOURCE_DIR).as_posix()
    return f"{key}@{prompt_date}" if prompt_date else key


def prompt_slice_sha1(prompts: list[dict]) -> str:
    payload = [{"timestamp": prompt["timestamp"], "body": prompt["body"]} for prompt in prompts]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def output_path_for(source_path: Path, prompt_date: str | None = None) -> Path:
    if prompt_date:
        return OUTPUT_DIR / f"{source_path.stem}--{prompt_date}.md"
    return OUTPUT_DIR / source_path.name


def read_pair_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    return dict(FRONTMATTER_FIELD_RE.findall(text[4:end]))


def read_daily_ledger(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    start = text.find(DAILY_LEDGER_PREFIX)
    if start < 0:
        return None
    start += len(DAILY_LEDGER_PREFIX)
    end = text.find(DAILY_LEDGER_SUFFIX, start)
    if end < 0:
        return None
    try:
        ledger = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(ledger, dict) or not isinstance(ledger.get("records"), list):
        return None
    return ledger


def load_state() -> dict:
    state: dict = {}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    state = {key: value for key, value in state.items() if not key.startswith("daily:")}
    for output_path in OUTPUT_DIR.glob("*.md"):
        safe_output(output_path)
        fields = read_pair_frontmatter(output_path)
        prompt_date = fields.get("prompt_date")
        if (
            fields.get("schema_version") == str(DAILY_SCHEMA_VERSION)
            and prompt_date
            and re.fullmatch("\\d{4}-\\d{2}-\\d{2}", prompt_date)
        ):
            ledger = read_daily_ledger(output_path)
            if ledger is None:
                continue
            try:
                pipeline_revision = int(fields["pipeline_revision"])
            except (KeyError, ValueError):
                continue
            day_hash = fields.get("day_input_sha256", "")
            if (
                ledger.get("schema_version") != DAILY_SCHEMA_VERSION
                or ledger.get("prompt_date") != prompt_date
                or ledger.get("day_input_sha256") != day_hash
                or (ledger.get("pipeline_revision") != pipeline_revision)
                or (not re.fullmatch("[0-9a-f]{64}", day_hash))
            ):
                continue
            state[daily_state_key(prompt_date)] = {
                "dayInputSha256": day_hash,
                "pipelineRevision": pipeline_revision,
                "records": ledger["records"],
                "translatedAt": fields.get("translated_at", ""),
                "model": fields.get("model", ""),
            }
            continue
        rel_source = fields.get("source")
        source_hash = fields.get("source_sha1")
        slice_date = fields.get("prompt_slice_date")
        slice_hash = fields.get("prompt_slice_sha1")
        if not rel_source:
            continue
        try:
            source_path = REPO_DIR / rel_source
            if (
                slice_date
                and slice_hash
                and re.fullmatch("\\d{4}-\\d{2}-\\d{2}", slice_date)
                and re.fullmatch("[0-9a-f]{40}", slice_hash)
            ):
                key = source_state_key(source_path, slice_date)
                input_state = {"inputSha1": slice_hash, "promptSliceDate": slice_date}
            elif source_hash and re.fullmatch("[0-9a-f]{40}", source_hash):
                key = source_state_key(source_path)
                input_state = {"sourceSha1": source_hash}
            else:
                continue
        except ValueError:
            continue
        try:
            prompt_count_total = int(fields.get("prompt_count_total", "0"))
            prompt_count_translated = int(fields.get("prompt_count_translated", "0"))
        except ValueError:
            continue
        state[key] = input_state | {
            "promptCountTotal": prompt_count_total,
            "promptCountTranslated": prompt_count_translated,
            "translatedAt": fields.get("translated_at", ""),
            "model": fields.get("model", ""),
        }
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(STATE_FILE, json.dumps(state, indent=2, sort_keys=True) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    safe_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".translate-tmp-{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def markdown_quote(text: str) -> str:
    lines = (line.rstrip() for line in text.strip().splitlines())
    return "\n".join((">" if not line else f"> {line}" for line in lines))


def render_pair_md(
    source_path: Path,
    input_hash: str,
    pairs: list[dict],
    model: str,
    filter_stats: dict,
    classify_stats: dict | None,
    prompt_date: str | None = None,
) -> str:
    rel_source = source_path.relative_to(REPO_DIR)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f"title: prompt translations from {source_path.stem}"
        + (f" on {prompt_date}" if prompt_date else ""),
        f"source: {rel_source}",
    ]
    if prompt_date:
        lines += [f"prompt_slice_date: {prompt_date}", f"prompt_slice_sha1: {input_hash}"]
    else:
        lines.append(f"source_sha1: {input_hash}")
    lines += [
        f"prompt_count_total: {filter_stats['input']}",
        f"prompt_count_translated: {len(pairs)}",
        f"filter_dropped_short: {filter_stats['too_short']}",
        f"filter_dropped_non_chinese: {filter_stats['non_chinese']}",
        f"filter_truncated: {filter_stats['truncated']}",
    ]
    if classify_stats is not None:
        lines += [
            f"classifier_kept_substantive: {classify_stats['kept']}",
            f"classifier_dropped_trivial: {classify_stats['dropped']}",
        ]
    lines += [
        f"translated_at: {now}",
        f"model: {model}",
        "---",
        "",
        GENERATED_MARKER + " Do not hand-edit;",
        "     edit the source raw-prompts file and re-run translation. -->",
        "",
        f"# Prompt pairs from `{rel_source}`" + (f" on {prompt_date} UTC" if prompt_date else ""),
        "",
    ]
    if not pairs:
        lines.append(
            "_(no substantive prompts found in this source after filtering + classification)_"
        )
        return "\n".join(lines).rstrip() + "\n"
    for i, pair in enumerate(pairs, start=1):
        flag = " (truncated)" if pair.get("truncated") else ""
        lines += [
            f"## Prompt {i} — {pair['timestamp']}{flag}",
            "",
            "**中文:**",
            "",
            markdown_quote(pair["body"]),
            "",
            "**English:**",
            "",
            markdown_quote(pair["english"]),
            "",
            "---",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def ledger_record_base(record: dict) -> dict:
    return {
        "record_id": record["record_id"],
        "source": record["source"],
        "source_timestamp": record["source_timestamp"],
        "occurrence": int(record["occurrence"]),
        "input_sha256": record["input_sha256"],
    }


def render_daily_pair_md(
    prompt_date: str,
    input_hash: str,
    source_records: list[dict],
    ledger_records: list[dict],
    model: str,
    classify_model: str,
) -> str:
    by_id = {record["record_id"]: record for record in source_records}
    translated = [record for record in ledger_records if record.get("status") == "translated"]
    filtered = [record for record in ledger_records if record.get("status") == "filtered"]
    classified_trivial = [
        record for record in ledger_records if record.get("status") == "classified_trivial"
    ]
    filtered_short = sum((record.get("filter_reason") == "too_short" for record in filtered))
    filtered_non_chinese = sum(
        (record.get("filter_reason") == "non_chinese" for record in filtered)
    )
    truncated = sum((bool(record.get("truncated")) for record in ledger_records))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger = {
        "schema_version": DAILY_SCHEMA_VERSION,
        "prompt_date": prompt_date,
        "day_input_sha256": input_hash,
        "pipeline_revision": PIPELINE_REVISION,
        "records": sorted(ledger_records, key=record_sort_key),
    }
    ledger_json = json.dumps(ledger, ensure_ascii=False, separators=(",", ":"))
    ledger_json = ledger_json.replace("--", "\\u002d\\u002d")
    lines = [
        "---",
        f"title: prompt translations for {prompt_date}",
        "type: learning",
        f"created: {prompt_date}",
        f"schema_version: {DAILY_SCHEMA_VERSION}",
        f"prompt_date: {prompt_date}",
        f"day_input_sha256: {input_hash}",
        f"pipeline_revision: {PIPELINE_REVISION}",
        f"record_count_total: {len(ledger_records)}",
        f"record_count_translated: {len(translated)}",
        f"record_count_filtered: {len(filtered)}",
        f"record_count_classified_trivial: {len(classified_trivial)}",
        f"filter_dropped_short: {filtered_short}",
        f"filter_dropped_non_chinese: {filtered_non_chinese}",
        f"filter_truncated: {truncated}",
        f"translated_at: {now}",
        f"model: {model}",
        f"classify_model: {classify_model}",
        "---",
        "",
        GENERATED_MARKER + " Do not hand-edit. -->",
        DAILY_LEDGER_PREFIX.rstrip("\n"),
        ledger_json,
        DAILY_LEDGER_SUFFIX.lstrip("\n"),
        "",
        f"# Prompt translations — {prompt_date}",
        "",
        f"{len(translated)} of {len(ledger_records)} records selected for learning; {len(filtered)} filtered deterministically and {len(classified_trivial)} classified as trivial.",
        "",
    ]
    visible = sorted(
        translated,
        key=lambda record: (
            record["source_timestamp"],
            record["source"],
            int(record["occurrence"]),
            record["record_id"],
        ),
    )
    for index, ledger_record in enumerate(visible, start=1):
        source_record = by_id[ledger_record["record_id"]]
        lines += [
            f"## Record {index} — {ledger_record['source_timestamp']}",
            "",
            f"- Source: `{ledger_record['source']}`",
            f"- Record ID: `{ledger_record['record_id']}`",
            f"- Input SHA-256: `{ledger_record['input_sha256']}`",
            "",
            "**中文:**",
            "",
            markdown_quote(source_record["body"]),
            "",
            "**English:**",
            "",
            markdown_quote(ledger_record["english"]),
            "",
            "---",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


PAIR_BLOCK_RE = re.compile(
    "\\*\\*中文:\\*\\*\\s*\\n\\s*\\n((?:>.*\\n)+)\\s*\\n\\*\\*English:\\*\\*\\s*\\n\\s*\\n((?:>.*\\n)+)",
    re.MULTILINE,
)


def load_existing_pairs(sample_size: int) -> tuple[list[dict], datetime | None, datetime | None]:
    pairs: list[dict] = []
    dates: list[datetime] = []
    for f in sorted(OUTPUT_DIR.glob("*.md")):
        safe_output(f)
        if f.name == "README.md":
            continue
        text = f.read_text(encoding="utf-8")
        d = parse_date_from_filename(f.name)
        if d is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f.name):
            d = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if d:
            dates.append(d)
        for m in PAIR_BLOCK_RE.finditer(text):
            zh = "\n".join(
                (line[2:] if line.startswith("> ") else line for line in m.group(1).splitlines())
            ).strip()
            en = "\n".join(
                (line[2:] if line.startswith("> ") else line for line in m.group(2).splitlines())
            ).strip()
            if zh and en:
                pairs.append({"zh": zh, "en": en, "source_file": f.name})
    if not pairs:
        return ([], None, None)
    if len(pairs) > sample_size:
        random.seed(42)
        pairs = random.sample(pairs, sample_size)
    return (pairs, min(dates) if dates else None, max(dates) if dates else None)


def generate_cheatsheet(client, model: str, sample_size: int = 200) -> int:
    pairs, dmin, dmax = load_existing_pairs(sample_size)
    if not pairs:
        print("[cheatsheet] no translated pairs found yet; skipping")
        return 0
    user_msg_lines = [f"Sample of {len(pairs)} (zh, en) prompt pairs from this user's corpus:"]
    for i, p in enumerate(pairs, start=1):
        zh_short = p["zh"][:300] + ("…" if len(p["zh"]) > 300 else "")
        en_short = p["en"][:300] + ("…" if len(p["en"]) > 300 else "")
        user_msg_lines.append(f"\n--- Pair {i} ---")
        user_msg_lines.append(f"zh: {zh_short}")
        user_msg_lines.append(f"en: {en_short}")
    if dmin and dmax:
        user_msg_lines.append(
            f"\n(Date range: {dmin.strftime('%Y-%m-%d')} to {dmax.strftime('%Y-%m-%d')})"
        )
    user_msg = "\n".join(user_msg_lines)
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": CHEATSHEET_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    md = resp.content[0].text.strip()
    if md.startswith("```"):
        md = re.sub("^```(?:markdown)?\\s*", "", md)
        md = re.sub("\\s*```\\s*$", "", md)
    CHEATSHEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = "<!-- Generated by `prompt-translation/scripts/translate --cheatsheet`.\n     Re-run after adding new translated pairs to refresh.\n     The script samples pairs deterministically (random.seed(42)). -->\n\n"
    write_text_atomic(CHEATSHEET_PATH, header + md + "\n")
    print(
        f"[cheatsheet] wrote {CHEATSHEET_PATH.relative_to(REPO_DIR)} from {len(pairs)} sampled pairs"
    )
    return len(pairs)


def model_pricing(model: str) -> dict:
    if model in PRICING:
        return PRICING[model]
    for family in ("opus", "haiku", "sonnet"):
        if family in model.lower() and family in PRICING:
            return PRICING[family]
    return PRICING.get("default", config.ZERO_PRICES["sonnet"])


def rough_token_count(text: str) -> int:
    cn = sum((1 for c in text if "一" <= c <= "鿿"))
    other = len(text) - cn
    return cn + max(1, other // 4)


def reusable_daily_records(
    source_records: list[dict], previous: dict, *, force: bool = False
) -> dict[str, dict]:
    if force or previous.get("pipelineRevision") != PIPELINE_REVISION:
        return {}
    old_rows = previous.get("records")
    if not isinstance(old_rows, list):
        return {}
    old_by_id = {
        row.get("record_id"): row
        for row in old_rows
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    }
    reusable: dict[str, dict] = {}
    for record in source_records:
        old = old_by_id.get(record["record_id"])
        if old is None:
            continue
        base = ledger_record_base(record)
        if any(old.get(key) != value for key, value in base.items() if key != "source"):
            continue
        status = old.get("status")
        if status == "translated":
            if not isinstance(old.get("english"), str) or not old["english"].strip():
                continue
        elif status == "filtered":
            if old.get("filter_reason") not in {"too_short", "non_chinese"}:
                continue
        elif status != "classified_trivial":
            continue
        reusable[record["record_id"]] = {**old, **base}
    return reusable


def process_daily_records(
    client,
    records: list[dict],
    previous: dict,
    *,
    model: str,
    classify_model: str,
    batch_size: int,
    classify_batch_size: int,
    no_classify: bool,
    force: bool = False,
) -> tuple[list[dict], dict]:
    reusable = reusable_daily_records(records, previous, force=force)
    resolved: dict[str, dict] = dict(reusable)
    classify_candidates: list[dict] = []
    for record in records:
        if record["record_id"] in resolved:
            continue
        body = record["body"]
        base = ledger_record_base(record)
        if len(body) < MIN_CHARS:
            resolved[record["record_id"]] = base | {
                "status": "filtered",
                "filter_reason": "too_short",
            }
            continue
        if chinese_ratio(body) < MIN_CHINESE_RATIO:
            resolved[record["record_id"]] = base | {
                "status": "filtered",
                "filter_reason": "non_chinese",
            }
            continue
        llm_body = body
        truncated = False
        if len(llm_body) > MAX_CHARS:
            llm_body = llm_body[:MAX_CHARS] + "\n\n[…truncated]"
            truncated = True
        classify_candidates.append({**record, "llm_body": llm_body, "truncated": truncated})
    substantive: list[dict]
    if no_classify:
        substantive = classify_candidates
    else:
        kept_flags: list[bool] = []
        for index in range(0, len(classify_candidates), classify_batch_size):
            batch = classify_candidates[index : index + classify_batch_size]
            kept_flags.extend(
                classify_substantive(
                    client, [record["llm_body"] for record in batch], classify_model
                )
            )
            time.sleep(0.3)
        substantive = []
        for record, keep in zip(classify_candidates, kept_flags):
            if keep:
                substantive.append(record)
                continue
            resolved[record["record_id"]] = ledger_record_base(record) | {
                "status": "classified_trivial",
                "truncated": record["truncated"],
            }
    for index in range(0, len(substantive), batch_size):
        batch = substantive[index : index + batch_size]
        translated = translate_batch(client, [record["llm_body"] for record in batch], model)
        for record, english in zip(batch, translated):
            resolved[record["record_id"]] = ledger_record_base(record) | {
                "status": "translated",
                "truncated": record["truncated"],
                "english": english,
                "model": model,
            }
        time.sleep(0.3)
    if len(resolved) != len(records):
        raise RuntimeError(
            f"daily ledger coverage mismatch: resolved {len(resolved)} of {len(records)} records"
        )
    rows = [resolved[record["record_id"]] for record in records]
    stats = {
        "reused": len(reusable),
        "processed": len(records) - len(reusable),
        "translated": sum((row["status"] == "translated" for row in rows)),
        "filtered": sum((row["status"] == "filtered" for row in rows)),
        "classified_trivial": sum((row["status"] == "classified_trivial" for row in rows)),
    }
    return (rows, stats)


def cleanup_legacy_daily_slices(prompt_date: str) -> int:
    removed = 0
    for path in OUTPUT_DIR.glob(f"*--{prompt_date}.md"):
        path.unlink()
        removed += 1
    return removed


def filter_sources_by_date(sources: list[Path], days: int | None) -> list[Path]:
    if days is None or days <= 0:
        return sources
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for s in sources:
        dates = prompt_dates(s)
        d = max(dates) if dates else parse_date_from_filename(s.name)
        if d is not None and d >= cutoff:
            out.append(s)
    return out


def prompt_dates(source_path: Path) -> list[datetime]:
    try:
        prompts = parse_prompts(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return []
    dates: list[datetime] = []
    for prompt in prompts:
        try:
            dates.append(
                datetime.strptime(prompt["timestamp"], "%Y-%m-%d %H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            )
        except (KeyError, ValueError):
            continue
    return dates


def parse_utc_date(value: str, option: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {option} {value!r}; expected YYYY-MM-DD") from exc


def prompt_utc_date(prompt: dict) -> date | None:
    try:
        return datetime.strptime(prompt["timestamp"], "%Y-%m-%d %H:%M:%SZ").date()
    except (KeyError, ValueError):
        return None


def build_work_units(
    sources: list[Path],
    *,
    exact_date: str | None = None,
    since_date: str | None = None,
    through_date: str | None = None,
    oldest_first: bool = False,
) -> list[dict]:
    wanted = parse_utc_date(exact_date, "--date") if exact_date else None
    since = parse_utc_date(since_date, "--since-date") if since_date else None
    through = parse_utc_date(through_date, "--through-date") if through_date else None
    if through is not None and since is None:
        raise ValueError("--through-date requires --since-date")
    if since is not None and through is not None and (through < since):
        raise ValueError("--through-date must be on or after --since-date")
    daily_mode = wanted is not None or since is not None
    units: list[dict] = []
    if not daily_mode:
        for source in sources:
            text = source.read_text(encoding="utf-8")
            prompts = parse_prompts(text)
            units.append(
                {
                    "source": source,
                    "prompts": prompts,
                    "prompt_date": None,
                    "input_hash": source_sha1(text),
                    "state_key": source_state_key(source),
                    "state_hash_field": "sourceSha1",
                    "output_path": output_path_for(source),
                }
            )
        return sorted(units, key=lambda unit: unit["source"].name, reverse=not oldest_first)
    grouped: dict[date, list[dict]] = {}
    for record in prompt_records(sources):
        stamp_date = prompt_utc_date(record)
        if stamp_date is None:
            continue
        if wanted is not None and stamp_date != wanted:
            continue
        if since is not None and stamp_date < since:
            continue
        if through is not None and stamp_date > through:
            continue
        grouped.setdefault(stamp_date, []).append(record)
    for stamp_date, day_records in grouped.items():
        date_text = stamp_date.isoformat()
        day_records = sorted(
            day_records,
            key=lambda record: (
                record["source_timestamp"],
                record["source"],
                record["occurrence"],
                record["record_id"],
            ),
        )
        units.append(
            {
                "source": None,
                "records": day_records,
                "prompts": day_records,
                "prompt_date": date_text,
                "input_hash": day_input_sha256(day_records),
                "state_key": daily_state_key(date_text),
                "state_hash_field": "dayInputSha256",
                "output_path": daily_output_path(date_text),
            }
        )
    return sorted(units, key=lambda unit: unit["prompt_date"], reverse=not oldest_first)


def select_work_units(
    units: list[dict], state: dict, *, force: bool = False, limit: int = 0
) -> tuple[list[dict], int, int]:
    selected: list[dict] = []
    skipped = 0
    for unit in units:
        source = unit["source"]
        previous = state.get(
            unit["state_key"], state.get(source.name, {}) if source is not None else {}
        )
        revision_matches = (
            unit["prompt_date"] is None or previous.get("pipelineRevision") == PIPELINE_REVISION
        )
        if (
            not force
            and previous.get(unit["state_hash_field"]) == unit["input_hash"]
            and revision_matches
            and unit["output_path"].exists()
        ):
            skipped += 1
            continue
        selected.append(unit)
    deferred = max(0, len(selected) - limit) if limit else 0
    if limit:
        selected = selected[:limit]
    return (selected, skipped, deferred)


def select_work_sources(
    sources: list[Path], state: dict, *, force: bool = False, limit: int = 0
) -> tuple[list[Path], int]:
    units = build_work_units(sources)
    selected, skipped, _deferred = select_work_units(units, state, force=force, limit=limit)
    return ([unit["source"] for unit in selected], skipped)


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=config.default_config())
    parser.add_argument("--root", help="Explicit temporary repository worktree")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--model", default=None, help="Override the configured translation model")
    parser.add_argument(
        "--classify-model", default=None, help="Override the configured classifier model"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--classify-batch-size", type=int, default=DEFAULT_CLASSIFY_BATCH_SIZE)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Legacy full-session mode: only sources active within last N rolling days",
    )
    parser.add_argument(
        "--date",
        help="Only prompts occurring on this exact UTC date (YYYY-MM-DD); writes one daily record file",
    )
    parser.add_argument(
        "--since-date",
        "--since",
        dest="since_date",
        help="Daily records on or after this UTC date (YYYY-MM-DD), including durable backlog",
    )
    parser.add_argument(
        "--through-date", help="With --since-date, stop at this inclusive UTC date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="Process oldest sources first (default: newest first)",
    )
    parser.add_argument(
        "--only", help="Only process source files whose name contains this substring"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit-files",
        type=int,
        default=0,
        help="Cap source or source/date work units after state checks",
    )
    parser.add_argument(
        "--limit-days",
        type=int,
        default=0,
        help="Cap record-based daily work units after state checks",
    )
    parser.add_argument(
        "--no-classify",
        action="store_true",
        help="Skip classification step; translate everything past the filter",
    )
    parser.add_argument(
        "--cheatsheet",
        action="store_true",
        help="After translating, regenerate the configured cheatsheet",
    )
    parser.add_argument(
        "--cheatsheet-only",
        action="store_true",
        help="Skip translation, only regenerate the cheatsheet from existing pairs",
    )
    parser.add_argument(
        "--cheatsheet-sample",
        type=int,
        default=200,
        help="How many pairs to sample for the cheatsheet",
    )
    parser.add_argument(
        "--credential-source",
        choices=("auto", "configured", "anthropic", "gsk", "gsk-proxy"),
        default="auto",
        help="Require the configured credential kind; never discover other credentials",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when credentials or any per-file LLM call fails",
    )
    args = parser.parse_args(argv)
    configure(config.load(args.config, root=args.root))
    args.model = args.model or DEFAULT_TRANSLATE_MODEL
    args.classify_model = args.classify_model or DEFAULT_CLASSIFY_MODEL
    if args.doctor:
        return doctor()
    if (
        not args.dry_run
        and (args.strict or SETTINGS["api"]["required"])
        and not config.credential_value(SETTINGS)
    ):
        raise config.ConfigurationError("configured-credential-missing")
    if (
        min(args.batch_size, args.classify_batch_size, args.cheatsheet_sample) < 1
        or min(args.limit_files, args.limit_days) < 0
        or (args.days is not None and args.days < 0)
    ):
        raise config.ConfigurationError("translation-count-option-invalid")
    if sum((value is not None for value in (args.days, args.date, args.since_date))) > 1:
        parser.error("--days, --date, and --since-date are mutually exclusive")
    if args.limit_days and (not (args.date or args.since_date)):
        parser.error("--limit-days requires --date or --since-date")
    if args.limit_days and args.limit_files:
        parser.error("--limit-days and --limit-files cannot be combined")
    if not SOURCE_DIR.is_dir():
        print(f"[ERROR] source dir not found: {SOURCE_DIR}", file=sys.stderr)
        return 1
    if args.cheatsheet_only and args.dry_run:
        pairs, _start, _end = load_existing_pairs(args.cheatsheet_sample)
        tokens = sum(rough_token_count(pair["zh"] + pair["en"]) for pair in pairs)
        print(
            f"OK cheatsheet dry run: {len(pairs)} sampled pairs, approximately {tokens} input tokens; no API calls or writes"
        )
        return 0
    if args.cheatsheet_only:
        client, source = make_client(args.credential_source)
        if client is None:
            print(f"[WARN] {source} — cannot regen cheatsheet without API key", file=sys.stderr)
            return 2 if args.strict or SETTINGS["api"]["required"] else 0
        print(f"[info] using credential: {source}")
        generate_cheatsheet(client, args.model, args.cheatsheet_sample)
        return 0
    sources = sorted(
        discover_sources(SOURCE_DIR), key=lambda p: p.name, reverse=not args.oldest_first
    )
    sources = filter_sources_by_date(sources, args.days)
    if args.only:
        sources = [s for s in sources if args.only in s.name]
    try:
        units = build_work_units(
            sources,
            exact_date=args.date,
            since_date=args.since_date,
            through_date=args.through_date,
            oldest_first=args.oldest_first,
        )
    except ValueError as exc:
        parser.error(str(exc))
    state = load_state()
    matched_source_count = len(sources)
    matched_unit_count = len(units)
    work_units, skipped, deferred = select_work_units(
        units, state, force=args.force, limit=args.limit_days or args.limit_files
    )
    pricing = model_pricing(args.model)
    classify_pricing = model_pricing(args.classify_model)
    est_in = est_out = est_classify_in = est_classify_out = 0
    n_translate_batches = n_classify_batches = 0
    for unit in work_units:
        prompts = unit["prompts"]
        kept, _ = filter_prompts(prompts)
        if not kept:
            continue
        if not args.no_classify:
            for i in range(0, len(kept), args.classify_batch_size):
                b = kept[i : i + args.classify_batch_size]
                bin_ = sum((rough_token_count(p["body"]) for p in b))
                est_classify_in += bin_
                est_classify_out += len(b) * 2
                n_classify_batches += 1
        translate_count = max(1, len(kept) // 2) if not args.no_classify else len(kept)
        avg_tokens = sum((rough_token_count(p["body"]) for p in kept)) // max(1, len(kept))
        for i in range(0, translate_count, args.batch_size):
            n_in_batch = min(args.batch_size, translate_count - i)
            est_in += avg_tokens * n_in_batch
            est_out += avg_tokens * n_in_batch
            n_translate_batches += 1
    classify_usd = (
        est_classify_in * classify_pricing["input"] / 1000000.0
        + est_classify_out * classify_pricing["output"] / 1000000.0
    )
    translate_usd = est_in * pricing["input"] / 1000000.0 + est_out * pricing["output"] / 1000000.0
    total_usd = classify_usd + translate_usd
    prices_configured = bool(SETTINGS.get("pricing"))
    print("=== Plan ===")
    print(f"  translation model:    {args.model}")
    print(
        f"  classify model:       {(args.classify_model if not args.no_classify else '(disabled)')}"
    )
    if args.date:
        print(f"  date filter:          prompts occurring on {args.date} UTC (daily slices)")
    elif args.since_date:
        end = args.through_date or "latest available prompt"
        print(f"  date filter:          prompt slices {args.since_date} through {end} UTC")
    elif args.days:
        print(
            f"  date filter:          last {args.days} days (since {(datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%d')})"
        )
    else:
        print("  date filter:          (none, all sources)")
    print(f"  source files:         {matched_source_count} scanned")
    print(
        f"  work units:           {matched_unit_count} matched, {len(work_units)} to process, {skipped} skipped, {deferred} deferred by cap"
    )
    if not args.no_classify:
        print(
            f"  classify batches:     ~{n_classify_batches}, ~{est_classify_in:,} input tok"
            + (f", ~${classify_usd:.2f}" if prices_configured else "")
        )
    print(
        f"  translate batches:    ~{n_translate_batches}"
        + (" (assumes ~50% substantive)" if not args.no_classify else "")
        + f", ~{est_in:,} input + ~{est_out:,} output tok"
        + (f", ~${translate_usd:.2f}" if prices_configured else "")
    )
    print(
        f"  estimated total cost: ~${total_usd:.2f} USD"
        if prices_configured
        else "  estimated total cost: unavailable (prices are not configured)"
    )
    print()
    if args.dry_run:
        print("=== Dry run — no API calls made ===")
        return 0
    if not work_units:
        if args.date or args.since_date:
            removed = sum(
                (
                    cleanup_legacy_daily_slices(unit["prompt_date"])
                    for unit in units
                    if unit["output_path"].exists()
                )
            )
            if removed:
                print(f"[cleanup] removed {removed} superseded per-source daily slices")
        print("[INFO] nothing to translate")
        if args.cheatsheet:
            client, source = make_client(args.credential_source)
            if client is not None:
                generate_cheatsheet(client, args.model, args.cheatsheet_sample)
        return 0
    client, source = make_client(args.credential_source)
    if client is None:
        print(f"WARN {source}; translation skipped", file=sys.stderr)
        return 2 if args.strict or SETTINGS["api"]["required"] else 0
    print(f"[info] using credential: {source}\n")
    counts = {"translated": 0, "empty_after_filter": 0, "error": 0, "skipped": skipped}
    for unit in work_units:
        src = unit["source"]
        prompts = unit["prompts"]
        prompt_date = unit["prompt_date"]
        current_hash = unit["input_hash"]
        out_path = unit["output_path"]
        state_key = unit["state_key"]
        state_hash_field = unit["state_hash_field"]
        if prompt_date is not None:
            print(f"\n=== {prompt_date} ({len(unit['records'])} records) ===")
            previous = state.get(state_key, {})
            try:
                ledger_records, daily_stats = process_daily_records(
                    client,
                    unit["records"],
                    previous,
                    model=args.model,
                    classify_model=args.classify_model,
                    batch_size=args.batch_size,
                    classify_batch_size=args.classify_batch_size,
                    no_classify=args.no_classify,
                    force=args.force,
                )
                md = render_daily_pair_md(
                    prompt_date,
                    current_hash,
                    unit["records"],
                    ledger_records,
                    args.model,
                    args.classify_model,
                )
                write_text_atomic(out_path, md)
                removed = cleanup_legacy_daily_slices(prompt_date)
                state[state_key] = {
                    "dayInputSha256": current_hash,
                    "pipelineRevision": PIPELINE_REVISION,
                    "records": ledger_records,
                    "promptCountTotal": len(unit["records"]),
                    "promptCountTranslated": daily_stats["translated"],
                    "translatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "model": args.model,
                }
                save_state(state)
            except Exception:
                print("FAIL daily processing could not complete", file=sys.stderr)
                counts["error"] += 1
                continue
            counts["translated"] += 1
            if not daily_stats["translated"]:
                counts["empty_after_filter"] += 1
            print(
                f"  ledger: translated={daily_stats['translated']}, filtered={daily_stats['filtered']}, classified_trivial={daily_stats['classified_trivial']}; reused={daily_stats['reused']}, processed={daily_stats['processed']}"
            )
            cleanup_note = f"; removed {removed} legacy slices" if removed else ""
            print(f"  → {out_path.relative_to(REPO_DIR)}{cleanup_note}")
            continue
        unit_label = f"{src.name} @ {prompt_date}" if prompt_date else src.name
        if not prompts:
            print(f"[ERROR] {unit_label}: no prompts found", file=sys.stderr)
            counts["error"] += 1
            continue
        kept, filter_stats = filter_prompts(prompts)
        print(f"\n=== {unit_label} ===")
        print(
            f"  parse: {filter_stats['input']} prompts; filter: kept {filter_stats['kept']} (too_short={filter_stats['too_short']}, non_chinese={filter_stats['non_chinese']}, truncated={filter_stats['truncated']})"
        )
        if not kept:
            counts["empty_after_filter"] += 1
            md = render_pair_md(src, current_hash, [], args.model, filter_stats, None, prompt_date)
            write_text_atomic(out_path, md)
            state[state_key] = {
                state_hash_field: current_hash,
                "promptCountTotal": filter_stats["input"],
                "promptCountTranslated": 0,
                "translatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "model": args.model,
            }
            save_state(state)
            print(f"  → {out_path.name}: no substantive prompts after filter; placeholder written")
            continue
        classify_stats = None
        if args.no_classify:
            substantive = kept
        else:
            try:
                kept_flags: list[bool] = []
                for i in range(0, len(kept), args.classify_batch_size):
                    batch = kept[i : i + args.classify_batch_size]
                    bodies = [p["body"] for p in batch]
                    flags = classify_substantive(client, bodies, args.classify_model)
                    kept_flags.extend(flags)
                    time.sleep(0.3)
                substantive = [p for p, keep in zip(kept, kept_flags) if keep]
                classify_stats = {"kept": len(substantive), "dropped": len(kept) - len(substantive)}
                print(
                    f"  classify: kept {classify_stats['kept']} substantive, dropped {classify_stats['dropped']} trivial"
                )
            except Exception:
                print("FAIL classification could not complete", file=sys.stderr)
                counts["error"] += 1
                continue
        if not substantive:
            md = render_pair_md(
                src, current_hash, [], args.model, filter_stats, classify_stats, prompt_date
            )
            write_text_atomic(out_path, md)
            state[state_key] = {
                state_hash_field: current_hash,
                "promptCountTotal": filter_stats["input"],
                "promptCountTranslated": 0,
                "translatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "model": args.model,
            }
            save_state(state)
            print(f"  → {out_path.name}: classifier dropped everything; placeholder written")
            counts["empty_after_filter"] += 1
            continue
        try:
            translations: list[str] = []
            for i in range(0, len(substantive), args.batch_size):
                batch = substantive[i : i + args.batch_size]
                bodies = [p["body"] for p in batch]
                print(
                    f"  translate batch {i // args.batch_size + 1}/{(len(substantive) + args.batch_size - 1) // args.batch_size}: {len(batch)} prompts…"
                )
                translated = translate_batch(client, bodies, args.model)
                translations.extend(translated)
                time.sleep(0.3)
        except Exception:
            print("FAIL translation could not complete", file=sys.stderr)
            counts["error"] += 1
            continue
        pairs = [{**p, "english": t} for p, t in zip(substantive, translations)]
        md = render_pair_md(
            src, current_hash, pairs, args.model, filter_stats, classify_stats, prompt_date
        )
        write_text_atomic(out_path, md)
        state[state_key] = {
            state_hash_field: current_hash,
            "promptCountTotal": filter_stats["input"],
            "promptCountTranslated": len(pairs),
            "translatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": args.model,
        }
        save_state(state)
        counts["translated"] += 1
        print(f"  → {out_path.relative_to(REPO_DIR)}")
    print(f"\n=== Done: {counts} ===")
    if args.cheatsheet:
        print()
        generate_cheatsheet(client, args.model, args.cheatsheet_sample)
    return 1 if counts["error"] else 0


def main(argv=None):
    try:
        return _main(argv)
    except config.ConfigurationError as error:
        print("FAIL " + str(error), file=sys.stderr)
        return 1
    except Exception:
        print("FAIL prompt translation could not complete", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
