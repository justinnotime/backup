"""Preview and send authorized GenTeam messages, including threads and proposals."""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .client import USER_AGENT, APIError, Client, channel_label, open_request, rows
from .config import ConfigurationError, Settings

SETTINGS: Settings | None = None
CLIENT: Client | None = None
STATE: Path | None = None
QUEUE: Path | None = None
AUDIT: Path | None = None
UA = USER_AGENT
MARKER = ""
TTL_SECONDS = 3600


def die(msg: str, code: int = 1):
    print(f"FAIL {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# GenTeam backend plumbing (cookie never printed)
# ---------------------------------------------------------------------------


def backend(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    return CLIENT.request(method, path, body=body, params=params)


def visible_channels() -> list[tuple[dict, str, str]]:
    return [
        (channel, channel_label(channel, members), sid)
        for channel, members, sid, _slug in CLIENT.channels(include_threads=False)
    ]


def resolve_target(to: str) -> tuple[dict, str, str]:
    """Resolve --to (substring or ch_* id) to (channel, label, server_id)."""
    chans = visible_channels()
    if to.startswith("ch_"):
        for ch, label, sid in chans:
            if ch["id"] == to:
                return ch, label, sid
        die(f"no visible channel with id {to}")
    needle = to.lower()
    hits = [(ch, label, sid) for ch, label, sid in chans if needle in label.lower()]
    if not hits:
        die(f"no visible channel matches {to!r} (try: scripts/send channels)")
    if len(hits) > 1:
        names = ", ".join(label for _, label, _ in hits)
        die(f"{to!r} is ambiguous: {names}")
    return hits[0]


# ---------------------------------------------------------------------------
# CometChat client REST (the SDK's own wire contract)
# ---------------------------------------------------------------------------


def comet_send(base: dict, group_guid: str, text: str, parent_message_id: str | None) -> str:
    """Send a text message; returns the CometChat server-assigned id."""
    body = {
        "receiver": group_guid,
        "receiverType": "group",
        "category": "message",
        "type": "text",
        "data": {"text": text},
        "muid": f"de_{int(time.time() * 1000)}_{secrets.token_hex(4)}",
    }
    if parent_message_id:
        body["parentMessageId"] = (
            int(parent_message_id) if parent_message_id.isdigit() else parent_message_id
        )
    if any(
        not isinstance(base.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", base[key])
        for key in ("app_id", "region")
    ) or not base.get("auth_token"):
        raise APIError("CometChat authorization fields are incomplete")
    url = f"https://{base['app_id']}.apiclient-{base['region']}.cometchat.io/v3.0/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "appId": base["app_id"],
            "authToken": base["auth_token"],
            "Content-Type": "application/json",
            "Accept": "application/json",
            "sdk": "javascript@4.1.2",
            "User-Agent": UA,
        },
    )
    try:
        with open_request(req, timeout=60) as r:
            sent = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise APIError(
            f"CometChat send failed (HTTP {e.code}); delivery may be unconfirmed"
        ) from None
    mid = str((sent.get("data") or {}).get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", mid):
        raise APIError("CometChat send returned no valid message id; delivery is unconfirmed")
    return mid


# ---------------------------------------------------------------------------
# The three-step send
# ---------------------------------------------------------------------------


def enforce_marker(text: str) -> str:
    return text if not MARKER or MARKER in text else f"{MARKER} {text}"


def audit(entry: dict):
    AUDIT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_private_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def perform_send(plan: dict) -> str:
    """Publish once to the chat transport, then confirm with GenTeam's backend."""
    if plan.get("pending_parent_channel_id"):
        created = backend(
            "POST",
            "/threads",
            {
                "server_id": plan["server_id"],
                "parent_channel_id": plan["pending_parent_channel_id"],
                "parent_comet_message_id": plan["parent_comet_message_id"],
            },
        )
        thread = created.get("thread") or {}
        if not thread.get("id"):
            raise APIError("thread create/get returned no thread id")
        plan.pop("pending_parent_channel_id")
        plan.update(
            thread_id=thread["id"], auth_channel_id=thread["id"], intercept_channel_id=thread["id"]
        )
    mid = plan.get("comet_message_id")
    if not mid:
        info = backend(
            "POST",
            "/cometchat/auth_info",
            {
                "server_id": plan["server_id"],
                "channel_id": plan["auth_channel_id"],
            },
        )
        guid = info.get("comet_group_guid")
        if not guid or not isinstance(info.get("base"), dict):
            raise APIError("auth_info returned no complete channel authorization")
        mid = comet_send(info["base"], guid, plan["text"], plan.get("parent_comet_message_id"))
        plan["comet_message_id"] = mid
        plan["comet_app_id"] = info["base"].get("app_id")
    pending = STATE / "pending-intercepts" / f"{mid}.json"
    try:
        write_private_json(pending, plan)
    except OSError:
        raise APIError(
            f"message {mid} reached chat transport but local recovery could not be saved; "
            "inspect the destination before any retry"
        ) from None
    intercept = {
        "comet_message_id": mid,
        "server_id": plan["server_id"],
        "channel_id": plan["intercept_channel_id"],
    }
    for key in ("thread_id", "parent_comet_message_id", "comet_app_id"):
        if plan.get(key):
            intercept[key] = plan[key]
    try:
        result = backend("POST", "/messages/intercept", intercept)
        if result.get("status") not in ("ok", "accepted", None):
            raise APIError("backend intercept was not accepted")
    except APIError:
        audit(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "intercept_pending",
                "comet_message_id": mid,
            }
        )
        raise APIError(
            f"message {mid} reached chat transport but backend confirmation failed; "
            f"use recover {mid} after inspection, do not send it again"
        ) from None
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "confirmed",
        "channel": plan["label"],
        "channel_id": plan["intercept_channel_id"],
        "server_id": plan["server_id"],
        "thread_id": plan.get("thread_id"),
        "parent_comet_message_id": plan.get("parent_comet_message_id"),
        "comet_message_id": mid,
        "text_sha256": hashlib.sha256(plan["text"].encode()).hexdigest()[:16],
        "via": plan.get("via", "send"),
    }
    preview_length = int(SETTINGS.get("send.audit_text_prefix_length", 0))
    if preview_length > 0:
        entry["text_head"] = plan["text"][:preview_length]
    audit(entry)
    pending.unlink()
    return mid


def build_plan(args) -> dict:
    """Resolve --to/--thread/--reply-to into a self-contained send plan."""
    if not args.text.strip():
        die("empty message text")
    text = enforce_marker(args.text.strip())

    if args.thread:
        parent = backend("GET", f"/threads/{args.thread}/parent")
        thread = parent.get("thread") or {}
        pmsg = parent.get("parent") or parent.get("parent_message") or {}
        parent_mid = thread.get("parent_comet_message_id") or str(
            (pmsg.get("data") or {}).get("comet_message_id") or ""
        )
        if not parent_mid:
            die(f"could not resolve parent message of thread {args.thread}")
        return {
            "label": f"thread {thread.get('thread_short_id') or args.thread}",
            "server_id": thread.get("server_id"),
            "auth_channel_id": args.thread,
            "intercept_channel_id": args.thread,
            "thread_id": args.thread,
            "parent_comet_message_id": str(parent_mid),
            "text": text,
        }

    if not args.to:
        die("--to is required (or use --thread THREAD_ID)")
    ch, label, server_id = resolve_target(args.to)

    if args.reply_to:
        return {
            "label": f"{label} (thread on msg {args.reply_to})",
            "server_id": server_id,
            "auth_channel_id": ch["id"],
            "intercept_channel_id": ch["id"],
            "pending_parent_channel_id": ch["id"],
            "parent_comet_message_id": str(args.reply_to),
            "text": text,
        }

    return {
        "label": label,
        "server_id": server_id,
        "auth_channel_id": ch["id"],
        "intercept_channel_id": ch["id"],
        "text": text,
    }


def print_preview(plan: dict, heading: str):
    print(f"{heading}")
    print(f"  to:      {plan['label']}")
    print(
        f"  channel: {plan['intercept_channel_id']}"
        + (
            f"  (thread of msg {plan['parent_comet_message_id']})"
            if plan.get("parent_comet_message_id")
            else ""
        )
    )
    print(f"  server:  {plan['server_id']}")
    print("  text:")
    for line in plan["text"].splitlines() or [""]:
        print(f"    {line}")


# ---------------------------------------------------------------------------
# Queue (propose / list / approve / reject)
# ---------------------------------------------------------------------------


def queue_path(pid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", pid):
        die(f"bad proposal id {pid!r}")
    return QUEUE / f"{pid}.json"


def load_proposal(pid: str) -> dict:
    p = queue_path(pid)
    if not p.exists():
        die(f"no pending proposal {pid}")
    prop = json.loads(p.read_text())
    if time.time() - prop["created"] > TTL_SECONDS:
        p.unlink()
        die(f"proposal {pid} expired (>{TTL_SECONDS}s); propose again")
    return prop


def cmd_propose(args):
    plan = build_plan(args)
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid = secrets.token_hex(6)
    write_private_json(queue_path(pid), {"created": time.time(), "plan": plan})
    print_preview(plan, "PROPOSED (not sent)")
    print(f"  id:      {pid}")
    print(f"approve in a terminal within {TTL_SECONDS // 60} min: scripts/send approve {pid}")


def cmd_list(_args):
    QUEUE.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    rows = 0
    for p in sorted(QUEUE.glob("*.json")):
        prop = json.loads(p.read_text())
        age = int(now - prop["created"])
        if age > TTL_SECONDS:
            p.unlink()
            continue
        rows += 1
        plan = prop["plan"]
        print(f"{p.stem}  age {age}s  -> {plan['label']}: {plan['text'][:60]}")
    if not rows:
        print("no pending proposals")


def cmd_approve(args):
    if not sys.stdin.isatty():
        die("approve requires a real terminal (tty)", 2)
    prop = load_proposal(args.id)
    print_preview(prop["plan"], "APPROVING")
    answer = input("send this? [y/N] ").strip().lower()
    if answer != "y":
        print("not sent (proposal kept)")
        return
    prop["plan"]["via"] = f"approve:{args.id}"
    try:
        mid = perform_send(prop["plan"])
    except APIError:
        write_private_json(queue_path(args.id), prop)
        raise
    queue_path(args.id).unlink()
    print(f"OK sent as message {mid}")


def cmd_reject(args):
    load_proposal(args.id)
    queue_path(args.id).unlink()
    print(f"OK rejected {args.id}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run(argv=None):
    ap = argparse.ArgumentParser(description="GenTeam sender (preview-first; see module docstring)")
    ap.add_argument("--config", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("channels", help="list sendable channels")
    p.add_argument("filter", nargs="?", default="")

    p = sub.add_parser("threads", help="list threads of a channel")
    p.add_argument("channel")

    for name in ("send", "propose"):
        p = sub.add_parser(name)
        p.add_argument("--to", help="channel match or ch_* id")
        p.add_argument("--thread", help="existing thread channel id")
        p.add_argument(
            "--reply-to", dest="reply_to", help="comet message id to thread on (with --to)"
        )
        p.add_argument("--text", required=True)
        if name == "send":
            p.add_argument(
                "--yes",
                action="store_true",
                help="actually send (subject to configured terminal policy)",
            )

    p = sub.add_parser("list", help="pending proposals")
    p = sub.add_parser("approve")
    p.add_argument("id")
    p = sub.add_parser("reject")
    p.add_argument("id")

    p = sub.add_parser("recover", help="confirm an already-sent message without sending it again")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")

    args = ap.parse_args(argv)
    configure(args.config)
    if args.cmd == "recover":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.id):
            raise ConfigurationError("invalid message id")
        plan = json.loads((STATE / "pending-intercepts" / f"{args.id}.json").read_text())
        print_preview(plan, "RECOVER BACKEND CONFIRMATION")
        if args.yes:
            perform_send(plan)
        else:
            print("re-run with --yes to confirm with the backend; no message is resent")
        return

    if args.cmd == "channels":
        for ch, label, _sid in visible_channels():
            if args.filter.lower() in label.lower():
                print(f"{ch['id']}  [{ch.get('channel_type')}]  {label}")
        return
    if args.cmd == "threads":
        ch, label, _sid = resolve_target(args.channel)
        threads = rows(backend("GET", f"/channels/{ch['id']}/threads"), "threads")
        if not threads:
            print(f"no threads in {label}")
            return
        for t in threads:
            print(
                f"{t.get('id')}  replies={t.get('reply_count')}  "
                f"last={t.get('last_reply_at')}  "
                f"short={t.get('thread_short_id')}"
            )
        return
    if args.cmd == "propose":
        return cmd_propose(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "approve":
        return cmd_approve(args)
    if args.cmd == "reject":
        return cmd_reject(args)

    # send
    plan = build_plan(args)
    if not args.yes:
        print_preview(plan, "PREVIEW (nothing sent)")
        print("re-run with --yes to send")
        return
    if (
        SETTINGS.get("send.require_tty", False)
        and not sys.stdin.isatty()
        and not os.environ.get("GENTEAM_SEND_NO_TTY_OK")
    ):
        die(
            "send --yes requires a real terminal — unattended agents must use "
            "propose/approve (or set GENTEAM_SEND_NO_TTY_OK=1 if the operator "
            "explicitly authorized this run)",
            2,
        )
    plan["via"] = "send"
    mid = perform_send(plan)
    print(f"OK sent as message {mid}")


def configure(source=None):
    global SETTINGS, CLIENT, STATE, QUEUE, AUDIT, MARKER, TTL_SECONDS
    SETTINGS = Settings(source)
    CLIENT = Client(SETTINGS)
    default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "genteam"
    STATE = SETTINGS.path("send.state_directory", default)
    QUEUE = STATE / "send-queue"
    AUDIT = SETTINGS.path("send.audit_file", STATE / "genteam-send.log")
    MARKER = SETTINGS.get("send.marker", "")
    TTL_SECONDS = int(SETTINGS.get("send.proposal_ttl_seconds", 3600))
    if not isinstance(MARKER, str) or TTL_SECONDS <= 0:
        raise ConfigurationError("invalid send marker or proposal lifetime")


def main(argv=None):
    try:
        return run(argv) or 0
    except (APIError, ConfigurationError, OSError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
