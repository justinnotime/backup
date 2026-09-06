"""Archive a complete configured calendar range into the current quarter file."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from .common import ArchiveError, Client, add_common_arguments, load_config, output_path, write_text


def utc_now():
    return datetime.now(timezone.utc)


def event_time(event, field):
    value = event.get(field)
    if not isinstance(value, dict):
        raise ArchiveError("calendar event time is missing or invalid")
    text = value.get("dateTime") or value.get("date")
    if not isinstance(text, str) or not text:
        raise ArchiveError("calendar event time is missing or invalid")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ArchiveError("calendar event time is not ISO formatted") from None
    return text


def contact_name(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise ArchiveError("calendar contact is invalid")
    address = value.get("emailAddress", value)
    if not isinstance(address, dict):
        raise ArchiveError("calendar contact address is invalid")
    result = (
        address.get("name")
        or address.get("address")
        or address.get("email")
        or value.get("email")
        or ""
    )
    if not isinstance(result, str):
        raise ArchiveError("calendar contact name is invalid")
    return result


def event_to_markdown(event: dict) -> str:
    if not isinstance(event, dict):
        raise ArchiveError("calendar event must be an object")
    title = event.get("title") or event.get("summary") or "Untitled"
    if not isinstance(title, str):
        raise ArchiveError("calendar event title is invalid")
    start_time, end_time = event_time(event, "start"), event_time(event, "end")
    organizer = contact_name(event.get("organizer") or {})
    attendees = event.get("attendees") or []
    attendee_total, attendee_note = None, ""
    if isinstance(attendees, dict):
        attendee_total = attendees.get("total")
        attendee_names = attendees.get("preview")
        attendee_note = attendees.get("note") or ""
        if (
            type(attendee_total) is not int
            or attendee_total < 0
            or not isinstance(attendee_names, list)
            or any(not isinstance(name, str) for name in attendee_names)
            or len(attendee_names) > attendee_total
            or not isinstance(attendee_note, str)
        ):
            raise ArchiveError("calendar attendee preview is invalid")
    elif isinstance(attendees, list):
        attendee_names = [name for name in map(contact_name, attendees) if name]
    else:
        raise ArchiveError("calendar attendees must be an array or a counted preview")
    location = event.get("location") or ""
    if isinstance(location, dict):
        location = location.get("displayName") or json.dumps(
            location, ensure_ascii=False, sort_keys=True
        )
    if not isinstance(location, str):
        raise ArchiveError("calendar location is invalid")
    description = event.get("description") or ""
    if not isinstance(description, str):
        raise ArchiveError("calendar description is invalid")
    lines = [f"## {start_time[:16]} — {title}", "", f"- **Time:** {start_time} → {end_time}"]
    if organizer:
        lines.append(f"- **Organizer:** {organizer}")
    if attendee_names:
        label = "Attendees (available preview)" if attendee_total is not None else "Attendees"
        lines.append(f"- **{label}:** {', '.join(attendee_names)}")
    if attendee_total is not None:
        lines.append(f"- **Attendee count:** {attendee_total}")
    if attendee_note:
        lines.append(f"- **Attendee detail:** {attendee_note}")
    if location:
        lines.append(f"- **Location:** {location}")
    if description:
        lines.append(f"- **Description:** {description}")
    lines.append("")
    return "\n".join(lines)


def calendar_events(response, limit):
    if not isinstance(response, dict):
        raise ArchiveError("calendar response must be an object")
    data = response.get("data", response.get("session_state", response))
    if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
        raise ArchiveError("calendar response reports a failure")
    if "events" in data:
        events = data["events"]
    elif "calendar_events" in data:
        events = data["calendar_events"]
    else:
        raise ArchiveError("calendar response has no event collection")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ArchiveError("calendar event collection is invalid")
    if (
        len(events) >= limit
        or data.get("truncated")
        or data.get("has_more")
        or any(data.get(key) for key in ("continuation_token", "next_token", "next_page_token"))
    ):
        raise ArchiveError(
            "calendar coverage is incomplete; narrow the date range or increase list_limit"
        )
    for key in ("total", "total_count"):
        if key in data and (type(data[key]) is not int or data[key] > len(events)):
            raise ArchiveError("calendar response reports incomplete or invalid coverage")
    return events


def run(args):
    settings = load_config(args.config, "calendar", root=args.root, state_file=args.state_file)
    days_back = (
        args.days_back if args.days_back is not None else settings.options.get("days_back", 90)
    )
    days_forward = (
        args.days_forward
        if args.days_forward is not None
        else settings.options.get("days_forward", 90)
    )
    limit = (
        args.list_limit if args.list_limit is not None else settings.options.get("list_limit", 1000)
    )
    if (
        any(type(value) is not int or value < 0 for value in (days_back, days_forward))
        or type(limit) is not int
        or limit < 1
    ):
        raise ArchiveError("calendar range and limit must be valid nonnegative integers")
    now = utc_now()
    time_min = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00Z")
    time_max = (now + timedelta(days=days_forward)).strftime("%Y-%m-%dT23:59:59Z")
    quarter = (now.month - 1) // 3 + 1
    label = f"{now.year}-Q{quarter}"
    destination = output_path(settings, f"{label}-events.md")
    if args.doctor or args.dry_run:
        print(
            "OK calendar configuration; selected range and output validated; no service calls or writes"
        )
        return 0
    client = Client(settings)
    command = [
        "calendar",
        "list",
        "--time_min",
        time_min,
        "--time_max",
        time_max,
        "--limit",
        str(limit),
    ]
    if settings.account:
        command += ["-a", settings.account]
    events = calendar_events(client.call(command), limit)
    rendered = [(event_time(event, "start"), event_to_markdown(event)) for event in events]
    rendered.sort(key=lambda pair: pair[0])
    text = "\n".join([f"# Calendar Events ({label})", "", *(body for _, body in rendered)])
    if not destination.exists() or destination.read_text(encoding="utf-8") != text:
        write_text(destination, text)
    print(f"OK archived {len(events)} calendar events")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument("--days-back", type=int)
    parser.add_argument("--days-forward", type=int)
    parser.add_argument("--list-limit", type=int)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ArchiveError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError, OverflowError):
        print("FAIL calendar input or output is invalid", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
