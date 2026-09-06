"""Collect attention events without sending messages or installing a schedule."""

import fcntl
import json
import os
from datetime import timedelta

from .config import atomic_write
from .events import collect_events, iso, parse_ts, utcnow
from .graph import Graph, chat_label


def read_queue(path):
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if any(
        not isinstance(row, dict) or not row.get("chat_id") or not row.get("msg_id")
        for row in rows
    ):
        raise ValueError("queue-format-invalid")
    return rows


def run(settings):
    if not settings["collection_enabled"]:
        print("SKIP collection disabled by private configuration")
        return
    lock = settings["lock_file"]
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("SKIP another collector holds the configured lock")
            return
        collect(settings)


def collect(settings):
    state_dir = settings["state_directory"]
    state_path, queue_path = state_dir / "state.json", state_dir / "queue.jsonl"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("state-format-invalid")
    previous_queue = read_queue(queue_path)
    graph = Graph(settings)
    graph.authenticate()
    own_id = graph.own_id()
    if state.get("me_id") and state["me_id"] != own_id:
        raise ValueError("state-belongs-to-another-account")
    state["me_id"] = own_id
    now = utcnow()
    run_watermark = parse_ts(state.get("run_watermark")) or now - timedelta(
        minutes=settings["first_run_lookback_minutes"]
    )
    overlap = timedelta(minutes=settings["overlap_minutes"])
    chats = graph.active_chats(run_watermark - overlap)
    senders = state.setdefault("senders", {})
    collected, suppressed = [], 0
    for chat in chats:
        identifier = chat.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("chat-id-missing")
        chat_state = state.setdefault("chats", {}).setdefault(identifier, {})
        since = (parse_ts(chat_state.get("watermark")) or run_watermark) - overlap
        messages = graph.messages(identifier, since)
        events, capped = collect_events(
            messages,
            chat_state,
            senders,
            own_id,
            chat.get("chatType") == "oneOnOne",
            now,
            cap=settings["sender_hourly_limit"],
        )
        for event in events:
            event.update(
                chat_id=identifier,
                chat_topic=chat_label(chat),
                chat_type=chat.get("chatType") or "",
                collected_at=iso(now),
            )
        collected.extend(events)
        suppressed += capped
    # Queue-first commit preserves events if the following state write fails.
    # A subsequent collection deduplicates against those already durable events.
    seen = {(row["chat_id"], row["msg_id"]) for row in previous_queue}
    fresh = []
    for row in collected:
        key = (row["chat_id"], row["msg_id"])
        if key not in seen:
            fresh.append(row)
            seen.add(key)
    if fresh:
        atomic_write(
            queue_path,
            "".join(
                json.dumps(row, ensure_ascii=True) + "\n"
                for row in previous_queue + fresh
            ),
        )
    state["run_watermark"] = iso(now)
    atomic_write(
        state_path,
        json.dumps(state, ensure_ascii=True, indent=1, sort_keys=True) + "\n",
    )
    print(
        f"OK collection complete: {len(chats)} active chats, {len(fresh)} new events, {suppressed} capped"
    )
