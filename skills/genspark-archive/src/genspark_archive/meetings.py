"""Archive original meeting notes and transcripts with complete pagination."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import date as date_type
from datetime import datetime, timezone

from .common import (
    ArchiveError,
    Client,
    add_common_arguments,
    load_config,
    output_path,
    read_state,
    write_state,
    write_text,
)


def utc_now():
    return datetime.now(timezone.utc)


def slugify(text: str, max_len=60) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text[:max_len].rstrip("-") or "untitled"


def meeting_to_markdown(meeting: dict, detail: dict | None) -> str:
    data = detail or {}
    title = (
        meeting.get("title") or data.get("title") or meeting.get("task_name") or "Untitled Meeting"
    )
    date = meeting.get("created_at") or data.get("created_at") or ""
    status = data.get("status") or meeting.get("status") or ""
    ident = meeting.get("id") or data.get("id") or ""
    if any(not isinstance(value, str) for value in (title, date, status, ident)):
        raise ArchiveError("meeting metadata is invalid")
    lines = [
        f"# {title}",
        "",
        f"- **Date:** {date}",
        f"- **Status:** {status}",
        f"- **Meeting ID:** `{ident}`",
        "- **kind:** transcript",
        "",
        "---",
        "",
    ]
    user_notes = data.get("user_notes") or ""
    transcript = (
        data.get("transcription_text") or data.get("transcription") or data.get("transcript") or ""
    )
    if not isinstance(user_notes, str) or not isinstance(transcript, str):
        raise ArchiveError("meeting original text must be a string")
    if user_notes:
        lines += ["## User Notes", "", user_notes, ""]
    if transcript:
        lines += ["## Transcript", "", transcript, ""]
    return "\n".join(lines)


def payload(response):
    if not isinstance(response, dict):
        raise ArchiveError("meeting response must be an object")
    data = response.get("data", response.get("session_state", response))
    if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
        raise ArchiveError("meeting response reports a failure")
    return data


def meeting_page(response, page_size):
    data = payload(response)
    if "notes" in data:
        records = data["notes"]
    elif "meetings" in data:
        records = data["meetings"]
    else:
        raise ArchiveError("meeting response has no meeting collection")
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ArchiveError("meeting collection is invalid")
    for record in records:
        if not isinstance(record.get("id"), str) or not record["id"]:
            raise ArchiveError("meeting identity is missing")
    continuation = data.get("continuation_token") or data.get("next_token")
    has_more = data.get("has_more")
    if has_more is None:
        if continuation or len(records) >= page_size:
            raise ArchiveError("meeting pagination completeness is unknown")
        has_more = False
    if type(has_more) is not bool:
        raise ArchiveError("meeting pagination flag is invalid")
    if continuation is not None and (not isinstance(continuation, str) or not continuation):
        raise ArchiveError("meeting continuation token is invalid")
    if has_more and not continuation:
        raise ArchiveError("meeting continuation token is missing")
    return records, continuation if has_more else None


def list_meetings(client, page_size):
    records, seen_tokens = {}, set()
    continuation = None
    while True:
        command = ["meeting", "list", "--page_size", str(page_size)]
        if continuation:
            command += ["--continuation_token", continuation]
        page, following = meeting_page(client.call(command), page_size)
        for record in page:
            records[record["id"]] = record
        if not following:
            return list(records.values())
        if following in seen_tokens:
            raise ArchiveError("meeting continuation token repeated")
        seen_tokens.add(following)
        continuation = following
        client.pause()


def meeting_detail(response, ident):
    data = payload(response)
    detail = data.get("meeting", data)
    if (
        not isinstance(detail, dict)
        or not isinstance(detail.get("status"), str)
        or not detail["status"]
    ):
        raise ArchiveError("meeting detail is missing its status")
    if detail.get("error") or detail.get("success") is False:
        raise ArchiveError("meeting detail reports a failure")
    if "id" in detail and detail["id"] != ident:
        raise ArchiveError("meeting detail identity does not match")
    for key in ("user_notes", "transcription_text", "transcription", "transcript"):
        if detail.get(key) is not None and not isinstance(detail[key], str):
            raise ArchiveError("meeting original text must be a string")
    return detail


def file_date(value):
    if not isinstance(value, str) or not value:
        return "unknown"
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return "unknown"


def complete(detail, date, now, give_up_days):
    status = detail["status"].upper()
    if status in {"FAILED", "ERROR"}:
        return False
    transcript = (
        detail.get("transcription_text") or detail.get("transcription") or detail.get("transcript")
    )
    if transcript or status in {"COMPLETED", "SHARED"}:
        return True
    if status == "INIT" and date != "unknown":
        age = (now.date() - date_type.fromisoformat(date)).days
        return age > give_up_days
    return False


def run(args):
    settings = load_config(args.config, "meetings", root=args.root, state_file=args.state_file)
    page_size = (
        args.page_size if args.page_size is not None else settings.options.get("page_size", 50)
    )
    give_up_days = (
        args.give_up_days
        if args.give_up_days is not None
        else settings.options.get("give_up_days", 3)
    )
    if (
        type(page_size) is not int
        or not 1 <= page_size <= 50
        or type(give_up_days) is not int
        or give_up_days < 0
    ):
        raise ArchiveError("meeting page size or retry period is invalid")
    state = read_state(settings.state_file)
    if args.doctor or args.dry_run:
        print("OK meeting configuration and state; no service calls or writes")
        return 0
    synced_ids = set(state["synced_ids"])
    client = Client(settings)
    records = list_meetings(client, page_size)
    now = utc_now()
    writes = []
    finished, pending = 0, 0
    for meeting in records:
        ident = meeting["id"]
        if ident in synced_ids:
            continue
        detail = meeting_detail(
            client.call(["meeting", "get", "--task_id", ident, "--detail_level", "full"]), ident
        )
        client.pause()
        markdown = meeting_to_markdown(meeting, detail)
        title = (
            meeting.get("title") or detail.get("title") or meeting.get("task_name") or "Untitled"
        )
        date = file_date(meeting.get("created_at") or detail.get("created_at"))
        digest = hashlib.md5(ident.encode()).hexdigest()[:8]
        destination = output_path(settings, f"{date}_{slugify(title, max_len=50)}_{digest}.md")
        writes.append((destination, markdown))
        if complete(detail, date, now, give_up_days):
            synced_ids.add(ident)
            finished += 1
        else:
            pending += 1
    # Pagination and every selected detail must succeed before any archive or
    # progress write. A failed get must never become a permanently synced stub.
    for destination, markdown in writes:
        if not destination.exists() or destination.read_text(encoding="utf-8") != markdown:
            write_text(destination, markdown)
    state["synced_ids"] = sorted(synced_ids)
    state["last_sync"] = now.isoformat()
    write_state(settings.state_file, state)
    print(
        f"OK archived {len(writes)} meeting records; {finished} completed, {pending} remain retryable"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--page-size", type=int)
    parser.add_argument("--give-up-days", type=int)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ArchiveError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError):
        print("FAIL meeting input or output is invalid", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
