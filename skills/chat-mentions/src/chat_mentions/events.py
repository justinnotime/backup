"""Classify inbound direct messages and mentions of the configured account."""

from datetime import datetime, timedelta, timezone

MAX_TRIGGERS_PER_SENDER_HOUR = 4
SEEN_IDS_KEEP = 200
PREVIEW_CHARS = 280


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def classify_message(msg: dict, me_id: str, one_on_one: bool) -> str | None:
    if (msg.get("messageType") or "") != "message":
        return None
    frm = msg.get("from") or {}
    if frm.get("application"):
        return None
    user = frm.get("user") or {}
    sender_id = user.get("id")
    if not sender_id or sender_id == me_id:
        return None
    if one_on_one:
        return "dm"
    for m in msg.get("mentions") or []:
        mentioned_user = (m.get("mentioned") or {}).get("user") or {}
        if mentioned_user.get("id") == me_id:
            return "mention"
    return None


def cap_allows(
    sender_times: list[str], now: datetime, cap: int = MAX_TRIGGERS_PER_SENDER_HOUR
) -> tuple[bool, list[str]]:
    keep = []
    for t in sender_times:
        ts = parse_ts(t)
        if ts is not None and now - ts < timedelta(hours=1):
            keep.append(t)
    return (len(keep) < cap, keep)


def _fallback_preview(msg: dict) -> str:
    body = (msg.get("body") or {}).get("content") or ""
    import re

    return " ".join(re.sub("<[^>]+>", " ", body).split())


def collect_events(
    msgs: list[dict],
    chat_state: dict,
    senders: dict,
    me_id: str,
    one_on_one: bool,
    now: datetime,
    preview_fn=None,
    cap: int = MAX_TRIGGERS_PER_SENDER_HOUR,
) -> tuple[list[dict], int]:
    preview_fn = preview_fn or _fallback_preview
    seen_list = list(chat_state.get("seen") or [])
    seen = set(seen_list)
    newest = parse_ts(chat_state.get("watermark") or "")
    events: list[dict] = []
    suppressed = 0
    for msg in sorted(msgs, key=lambda m: str(m.get("createdDateTime") or "")):
        mid = str(msg.get("id") or "")
        ts = parse_ts(str(msg.get("createdDateTime") or ""))
        if not mid or ts is None or mid in seen:
            continue
        seen.add(mid)
        seen_list.append(mid)
        if newest is None or ts > newest:
            newest = ts
        kind = classify_message(msg, me_id, one_on_one)
        if kind is None:
            continue
        user = (msg.get("from") or {}).get("user") or {}
        sender_id = user["id"]
        allowed, kept = cap_allows(senders.get(sender_id) or [], now, cap)
        if not allowed:
            senders[sender_id] = kept
            suppressed += 1
            continue
        kept.append(iso(now))
        senders[sender_id] = kept
        events.append(
            {
                "kind": kind,
                "msg_id": mid,
                "ts": iso(ts),
                "sender_id": sender_id,
                "sender_name": user.get("displayName") or "?",
                "preview": (preview_fn(msg) or "")[:PREVIEW_CHARS],
            }
        )
    chat_state["seen"] = seen_list[-SEEN_IDS_KEEP:]
    if newest is not None:
        chat_state["watermark"] = iso(newest)
    return (events, suppressed)
