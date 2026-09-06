"""Local draft management; this module never invokes a transport."""

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import atomic_write

EXPIRE_HOURS = 48
STATUSES = ("pending", "sent", "dismissed", "expired")
STATE = None


def configure(settings):
    global STATE, EXPIRE_HOURS
    STATE = settings["state_directory"]
    EXPIRE_HOURS = settings["draft_expiry_hours"]


def drafts_root():
    return STATE / "drafts"


def queue_path():
    return STATE / "queue.jsonl"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slugify(text: str) -> str:
    s = re.sub("[^A-Za-z0-9]+", "-", str(text)).strip("-").lower()
    return s[:60] or "chat"


def render(meta: dict, body: str) -> str:
    for value in meta.values():
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise ValueError("draft-metadata-must-be-single-line")
    lines = ["---"]
    for key in (
        "status",
        "chat_id",
        "chat_topic",
        "msg_id",
        "sender",
        "created",
        "note",
    ):
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    lines += ["---", "", body.rstrip() + "\n"]
    return "\n".join(lines)


def parse(text: str) -> tuple[dict, str]:
    m = re.match("\\A---\\n(.*?)\\n---\\n?", text, re.S)
    if not m:
        return ({}, text)
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if sep:
            meta[key.strip()] = val.strip()
    return (meta, text[m.end() :].lstrip("\n"))


def effective_status(meta: dict, now: datetime | None = None) -> str:
    status = meta.get("status") or "pending"
    if status == "pending":
        created = parse_ts(meta.get("created") or "")
        if created and (now or utcnow()) - created > timedelta(hours=EXPIRE_HOURS):
            return "expired"
    return status


def draft_files() -> list[Path]:
    root = drafts_root()
    if not root.is_dir():
        return []
    return sorted(
        f
        for f in root.glob("*/*.md")
        if f.is_file()
        and not f.is_symlink()
        and not f.parent.is_symlink()
        and f.resolve().is_relative_to(root.resolve())
    )


def resolve(ref):
    files = draft_files()
    path = Path(ref).expanduser().resolve()
    hits = [
        f
        for f in files
        if f.resolve() == path or parse(f.read_text())[0].get("msg_id") == ref
    ]
    if len(hits) != 1:
        raise ValueError("draft-reference-must-match-one-stored-draft")
    if not parse(hits[0].read_text())[0].get("msg_id"):
        raise ValueError("draft-format-invalid")
    return hits[0]


def set_status(path: Path, status: str, note: str | None = None) -> None:
    meta, body = parse(path.read_text())
    meta["status"] = status
    if note:
        meta["note"] = note
    atomic_write(path, render(meta, body))


def cmd_open(args) -> None:
    drafted = set()
    for f in draft_files():
        meta, _ = parse(f.read_text())
        if meta.get("msg_id"):
            drafted.add((meta.get("chat_id"), meta["msg_id"]))
    qp = queue_path()
    if not qp.is_file():
        print("(no queue at the configured path; check collection settings)")
        return
    rows = 0
    lines = qp.read_text().splitlines()
    for line in lines:
        if not line.strip():
            continue
        e = json.loads(line)
        if not isinstance(e, dict) or not e.get("chat_id") or not e.get("msg_id"):
            raise ValueError("queue-format-invalid")
        if (e.get("chat_id"), str(e.get("msg_id"))) in drafted:
            continue
        rows += 1
        print(
            f"[{e.get('kind', '?'):7s}] {e.get('ts', '?'):20s} {(e.get('sender_name') or '?')[:20]:<20s} {(e.get('chat_topic') or e.get('chat_id') or '?')[:30]:<30s} {(e.get('preview') or '')[:60]}"
        )
        print(f"          msg {e.get('msg_id')}  chat {e.get('chat_id')}")
        if args.limit and rows >= args.limit:
            print(
                f"          ... limit {args.limit} reached ({len(lines)} queue lines total)"
            )
            break
    if not rows:
        print("(queue fully handled — no open events)")


def cmd_new(args) -> None:
    if not args.chat_id.strip() or not args.msg_id.strip():
        raise ValueError("draft-chat-and-message-identifiers-required")
    body = args.body if args.body is not None else sys.stdin.read()
    if not body.strip():
        sys.exit("empty draft body (pass --body or pipe text on stdin)")
    for f in draft_files():
        meta, _ = parse(f.read_text())
        if meta.get("msg_id") == args.msg_id and meta.get("chat_id") == args.chat_id:
            sys.exit(
                f"a draft for msg {args.msg_id} already exists ({meta.get('status')}): {f}; inspect the existing draft before editing"
            )
    now = utcnow()
    day_dir = drafts_root() / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256((args.chat_id + "\0" + args.msg_id).encode()).hexdigest()[
        :16
    ]
    path = day_dir / f"{slugify(args.topic or args.chat_id)}-{digest}.md"
    if path.exists():
        sys.exit(f"draft already exists: {path}")
    meta = {
        "status": "pending",
        "chat_id": args.chat_id,
        "chat_topic": args.topic or "",
        "msg_id": args.msg_id,
        "sender": args.sender or "",
        "created": iso(now),
    }
    atomic_write(path, render(meta, body), exclusive=True)
    print(path)


def cmd_list(args) -> None:
    now = utcnow()
    want = args.status
    rows = 0
    for f in draft_files():
        meta, _ = parse(f.read_text())
        status = effective_status(meta, now)
        if want != "all" and status != want:
            continue
        rows += 1
        print(
            f"[{status:9s}] {meta.get('created', '?'):20s} {(meta.get('chat_topic') or meta.get('chat_id') or '?')[:40]:<40s} {f}"
        )
    if not rows:
        print(f"(no {want} drafts)")


def cmd_show(args) -> None:
    path = resolve(args.ref)
    meta, body = parse(path.read_text())
    print(f"# {path}")
    print(f"status: {effective_status(meta)}")
    for key in ("chat_topic", "chat_id", "msg_id", "sender", "created", "note"):
        if meta.get(key):
            print(f"{key}: {meta[key]}")
    print()
    print(body)


def cmd_dismiss(args) -> None:
    path = resolve(args.ref)
    set_status(path, "dismissed")
    print(f"dismissed: {path}")


def cmd_mark_sent(args) -> None:
    path = resolve(args.ref)
    set_status(path, "sent", note=args.note)
    print(f"recorded as sent (the send itself happens in teams-send): {path}")
