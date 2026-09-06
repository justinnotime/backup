"""Preview or send authorized Teams messages using caller-owned configuration."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

GRAPH = "https://graph.microsoft.com/v1.0"
SEND_SCOPES = ["ChatMessage.Send"]
READ_SCOPES = ["Chat.Read"]
STATE = QUEUE = SEND_CACHE = READ_CACHE = AUDIT = REGISTRY = CONFIG_PATH = None
CLIENT_ID = None
SETTINGS = {}
MARKER = ""
TTL_SECONDS = 3600


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def configure(path):
    global STATE, QUEUE, SEND_CACHE, READ_CACHE, AUDIT, REGISTRY, CLIENT_ID
    global MARKER, TTL_SECONDS, SETTINGS, CONFIG_PATH
    CONFIG_PATH = pathlib.Path(os.path.expandvars(str(path))).expanduser().resolve()
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    allowed = {
        "schema",
        "state_directory",
        "read_token_file",
        "send_token_file",
        "registry_file",
        "client_id",
        "authority",
        "login_hint",
        "marker",
        "proposal_ttl_seconds",
        "gsk_command",
        "mirrored_chat_patterns",
        "audit_preview_chars",
    }
    if (
        not isinstance(value, dict)
        or value.get("schema") != "teams-send/v1"
        or set(value) - allowed
    ):
        raise ValueError("config-schema-invalid")

    def path_field(name, default=None):
        text = value.get(name, default)
        if not isinstance(text, str) or not text:
            raise ValueError("config-path-missing: " + name)
        expanded = os.path.expandvars(text)
        if re.search(r"\$(?:\w+|\{[^}]+\})", expanded):
            raise ValueError("config-path-variable-unresolved: " + name)
        target = pathlib.Path(expanded).expanduser()
        return (
            (CONFIG_PATH.parent / target).resolve()
            if not target.is_absolute()
            else target.resolve()
        )

    STATE = path_field("state_directory")
    QUEUE = STATE / "teams-send-queue"
    AUDIT = STATE / "teams-send.log"
    READ_CACHE = path_field("read_token_file")
    SEND_CACHE = path_field("send_token_file")
    REGISTRY = path_field("registry_file", str(STATE / "teams-chats.json"))
    if len({CONFIG_PATH, READ_CACHE, SEND_CACHE, REGISTRY, AUDIT}) != 5:
        raise ValueError("config-files-must-be-distinct")
    CLIENT_ID = value.get("client_id")
    if not isinstance(CLIENT_ID, str) or not CLIENT_ID.strip():
        raise ValueError("config-client-id-missing")
    MARKER = value.get("marker", "")
    if not isinstance(MARKER, str) or "\n" in MARKER or "\r" in MARKER:
        raise ValueError("config-marker-invalid")
    TTL_SECONDS = value.get("proposal_ttl_seconds", 3600)
    if type(TTL_SECONDS) is not int or TTL_SECONDS <= 0:
        raise ValueError("config-proposal-ttl-invalid")
    preview = value.get("audit_preview_chars", 0)
    if type(preview) is not int or preview < 0:
        raise ValueError("config-audit-preview-invalid")
    command = value.get("gsk_command", ["gsk", "microsoft_teams", "send"])
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(x, str) or not x for x in command)
    ):
        raise ValueError("config-gsk-command-invalid")
    patterns = value.get("mirrored_chat_patterns", [])
    if not isinstance(patterns, list) or any(not isinstance(x, str) for x in patterns):
        raise ValueError("config-mirrored-patterns-invalid")
    for name in ["authority", "login_hint"]:
        if name in value and (not isinstance(value[name], str) or not value[name]):
            raise ValueError("config-auth-field-invalid: " + name)
    SETTINGS = value


def _proposal_path(identifier):
    if not re.fullmatch("[0-9a-f]{8}", identifier):
        raise ValueError("proposal-id-invalid")
    return QUEUE / (identifier + ".json")


def _message_id(response):
    identifier = response.get("id") if isinstance(response, dict) else None
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("send-result-unconfirmed; inspect the chat before retrying")
    return identifier


def _audit(record, body):
    count = SETTINGS.get("audit_preview_chars", 0)
    if count:
        record["head"] = body[:count]
    record.update(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sha12=hashlib.sha256(body.encode()).hexdigest()[:12],
    )
    AUDIT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def _finish_delivery(record, body, proposal=None):
    identifier = record["msg_id"]
    try:
        if proposal is not None:
            proposal.unlink(missing_ok=True)
        _audit(record, body)
    except OSError:
        print(
            f"DELIVERED message id {identifier}; local-record-write-failed; do not resend",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def cmd_doctor(_args):
    print("OK configuration parsed; no authentication or send attempted")
    print("read cache: " + ("present" if READ_CACHE.is_file() else "missing"))
    print("send cache: " + ("present" if SEND_CACHE.is_file() else "missing"))
    print("registry: " + ("present" if REGISTRY.is_file() else "missing"))
    executable = SETTINGS.get("gsk_command", ["gsk"])[0]
    print(
        "external connector: "
        + ("available" if shutil.which(executable) else "unavailable")
    )


def _msal_token(
    cache_path: pathlib.Path, scopes: list, interactive: bool
) -> str | None:
    import msal

    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=SETTINGS.get(
            "authority", "https://login.microsoftonline.com/organizations"
        ),
        token_cache=cache,
    )
    result = None
    accounts = app.get_accounts(username=SETTINGS.get("login_hint"))
    if len(accounts) > 1:
        raise ValueError("multiple-cached-accounts; configure login_hint")
    if len(accounts) == 1:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result and interactive:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise ValueError("device-login-unavailable")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        _atomic_write(cache_path, cache.serialize())
    return result.get("access_token") if result and "access_token" in result else None


def _graph(method: str, path: str, token: str, payload=None):
    import requests

    url = GRAPH + path if path.startswith("/") else path
    base, target = urlsplit(GRAPH), urlsplit(url)
    if (target.scheme, target.netloc) != (
        base.scheme,
        base.netloc,
    ) or not target.path.startswith(base.path + "/"):
        raise ValueError("graph-url-outside-service")
    try:
        r = requests.request(
            method,
            url,
            timeout=60,
            allow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except requests.RequestException:
        raise ValueError(
            "send-result-unconfirmed; inspect the chat before retrying"
            if method == "POST"
            else "graph-request-failed"
        ) from None
    if not 200 <= r.status_code < 300:
        raise ValueError(f"graph-http-{r.status_code}")
    try:
        result = r.json()
    except ValueError:
        raise ValueError("graph-invalid-response") from None
    if not isinstance(result, dict):
        raise ValueError("graph-invalid-response")
    return result


def _collection(path, token):
    rows, seen = [], set()
    for _ in range(20):
        if path in seen:
            raise ValueError("graph-pagination-cycle")
        seen.add(path)
        data = _graph("GET", path, token)
        if not isinstance(data.get("value"), list) or any(
            not isinstance(x, dict) for x in data["value"]
        ):
            raise ValueError("graph-invalid-collection")
        rows.extend(data["value"])
        path = data.get("@odata.nextLink")
        if not path:
            return rows
        if not isinstance(path, str):
            raise ValueError("graph-invalid-next-link")
    raise ValueError("graph-list-incomplete")


def _resolve_chat(substr: str) -> tuple[str, str]:
    tok = _msal_token(READ_CACHE, READ_SCOPES, interactive=False)
    if not tok:
        sys.exit(
            "no read token (Chat.Read) — the Teams mirror login is missing on this machine"
        )
    chats = _collection(
        "/me/chats?$top=50&$expand=members&$orderby=lastMessagePreview/createdDateTime desc",
        tok,
    )
    needle = substr.lower()
    hits = []
    for c in chats:
        topic = (c.get("topic") or "").lower()
        members = " ".join(
            (m.get("displayName") or "" for m in c.get("members", []))
        ).lower()
        if needle in topic or (c.get("chatType") == "oneOnOne" and needle in members):
            hits.append((c["id"], c.get("topic") or members[:60]))
    if len(hits) != 1:
        sys.exit(
            f"chat match must be exactly 1, got {len(hits)}: {[h[1] for h in hits][:5]}"
        )
    return hits[0]


def _enforce_marker(text: str) -> str:
    text = text.strip()
    return text if not MARKER or text.startswith(MARKER) else f"{MARKER} {text}"


def _load_pending() -> dict:
    out = {}
    now = time.time()
    for f in QUEUE.glob("*.json"):
        if f.is_symlink():
            continue
        try:
            item = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if (
            not isinstance(item, dict)
            or item.get("id") != f.stem
            or not re.fullmatch("[0-9a-f]{8}", f.stem)
            or not isinstance(item.get("created_ts"), (int, float))
            or any(
                not isinstance(item.get(key), str)
                for key in ("chat_id", "chat_topic", "message")
            )
        ):
            continue
        if now - item.get("created_ts", 0) > TTL_SECONDS:
            continue
        out[item["id"]] = item
    return out


def _mirror_patterns() -> list:
    return [value.lower() for value in SETTINGS.get("mirrored_chat_patterns", [])]


def cmd_chats(args):
    if args.refresh or not REGISTRY.exists():
        tok = _msal_token(READ_CACHE, READ_SCOPES, interactive=False)
        if not tok:
            sys.exit("no read token (Chat.Read)")
        chats = _collection("/me/chats?$top=50&$expand=members,lastMessagePreview", tok)
        pats = _mirror_patterns()
        reg = []
        for c in chats:
            topic = c.get("topic") or ""
            members = [m.get("displayName") or "?" for m in c.get("members", [])]
            label = topic or " & ".join((n for n in members if n))[:80]
            hay = (topic + " " + " ".join(members)).lower()
            reg.append(
                {
                    "id": c["id"],
                    "type": c.get("chatType"),
                    "topic": topic,
                    "members": members,
                    "label": label,
                    "mirrored": any((p in hay for p in pats)),
                    "last_message_at": (c.get("lastMessagePreview") or {}).get(
                        "createdDateTime", ""
                    ),
                }
            )
        _atomic_write(
            REGISTRY,
            json.dumps(
                {
                    "refreshed": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "chats": reg,
                },
                ensure_ascii=True,
                indent=1,
            ),
        )
        print(f"registry refreshed: {len(reg)} chats -> {REGISTRY}")
    data = json.loads(REGISTRY.read_text())
    needle = (args.filter or "").lower()
    rows = [
        c
        for c in data["chats"]
        if not needle or needle in (c["label"] + " " + c["id"]).lower()
    ]
    rows.sort(key=lambda c: c.get("last_message_at") or "", reverse=True)
    shown = rows[:40]
    for c in shown:
        mark = "M" if c["mirrored"] else " "
        print(f"[{mark}] {c['type']:<9} {c['label'][:58]:<58} {c['id']}")
    if len(rows) > len(shown):
        print(
            f"NOTE {len(rows) - len(shown)} more chats matched but are not shown ({len(rows)} total; pass a filter substring to narrow)"
        )
    if not rows:
        print("(no match — try `chats --refresh`)")


def _resolve_registry(substr: str):
    needle = substr.lower()
    if REGISTRY.exists():
        chats = json.loads(REGISTRY.read_text())["chats"]
        hits = [
            c
            for c in chats
            if needle in c["label"].lower()
            or (
                c["type"] == "oneOnOne"
                and any((needle in (m or "").lower() for m in c["members"]))
            )
        ]
        if len(hits) == 1:
            return (hits[0]["id"], hits[0]["label"])
        if len(hits) > 1:
            sys.exit(
                f"chat match must be exactly 1, got {len(hits)}: {[h['label'] for h in hits][:6]} — narrow the filter or use --chat-id"
            )
    return _resolve_chat(substr)


def _image_data_uri(path: pathlib.Path) -> str:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or ""
    if not mime.startswith("image/"):
        sys.exit(f"not an image (by extension): {path}")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


_URL_RE = re.compile('https?://[^\\s<>\\"]+')
_URL_TRAIL = ".,;:!?)]}'" + "。，；：）、」》"


def _linkify_escaped(escaped: str) -> str:

    def _one(m: "re.Match[str]") -> str:
        url, trail = (m.group(0), "")
        while url and url[-1] in _URL_TRAIL:
            trail = url[-1] + trail
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trail}' if url else trail

    return _URL_RE.sub(_one, escaped)


def _html_body(text: str, images: list) -> str:
    import html as htmlmod

    def _inline(line: str) -> str:
        escaped = htmlmod.escape(line)
        anchors: list = []

        def _mdlink(m: "re.Match[str]") -> str:
            anchors.append(f'<a href="{m.group(2)}">{m.group(1)}</a>')
            return f"\x00{len(anchors) - 1}\x00"

        escaped = re.sub("\\[([^\\]]+)\\]\\((https?://[^)\\s]+)\\)", _mdlink, escaped)
        escaped = _linkify_escaped(escaped)
        escaped = re.sub("\\*\\*([^*]+)\\*\\*", "<strong>\\1</strong>", escaped)
        for i, anchor in enumerate(anchors):
            escaped = escaped.replace(f"\x00{i}\x00", anchor)
        return escaped

    parts, bullets = ([], [])

    def _flush_bullets():
        nonlocal bullets
        if bullets:
            parts.append("<ul>" + "".join((f"<li>{b}</li>" for b in bullets)) + "</ul>")
            bullets = []

    blank_run = 0
    for line in text.strip("\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            blank_run += 1
            continue
        m = re.match("[-*]\\s+(.+)", stripped)
        if m:
            bullets.append(_inline(m.group(1)))
            blank_run = 0
            continue
        _flush_bullets()
        if blank_run >= 2 and parts:
            parts.append("<p>&nbsp;</p>")
        blank_run = 0
        parts.append(f"<p>{_inline(stripped)}</p>")
    _flush_bullets()
    parts += [f'<p><img src="{_image_data_uri(p)}"></p>' for p in images]
    return "".join(parts)


def _resolve_mentions(chat_id: str, needles: list) -> tuple[list, list]:
    tok = _msal_token(READ_CACHE, READ_SCOPES, interactive=False)
    if not tok:
        sys.exit("no read token (Chat.Read) to resolve mentions")
    members = _collection(f"/chats/{quote(chat_id, safe='')}/members", tok)
    ids, names = ([], [])
    for needle in needles:
        hits = [
            (m.get("userId"), m.get("displayName"))
            for m in members
            if needle.lower() in (m.get("displayName") or "").lower()
        ]
        if len(hits) != 1:
            sys.exit(
                f"mention '{needle}' must match exactly 1 chat member, got {len(hits)}: {[h[1] for h in hits][:5]}"
            )
        if not isinstance(hits[0][0], str) or not hits[0][0]:
            raise ValueError("mention-user-id-missing")
        ids.append(hits[0][0])
        names.append(hits[0][1])
    return (ids, names)


def cmd_send(args):
    text = args.message or sys.stdin.read()
    if not text.strip():
        sys.exit("empty message")
    images = [pathlib.Path(i).expanduser() for i in args.image or []]
    for pth in images:
        if not pth.is_file():
            sys.exit(f"image not found: {pth}")
    if images and args.via != "gsk":
        sys.exit(
            "inline images need --via gsk (the direct Graph path here is text-only)"
        )
    if args.reply_to and args.via != "gsk":
        sys.exit("--reply-to needs --via gsk (quoting rides gsk's reply_to_message_id)")
    if args.mention and args.via != "gsk":
        sys.exit("--mention needs --via gsk (mentions ride gsk's mention_user_ids)")
    for i, needle in enumerate(args.mention or []):
        if f"{{mention_{i}}}" not in text:
            sys.exit(
                f"message text needs a {{mention_{i}}} placeholder for --mention '{needle}'"
            )
    if args.chat_id:
        chat_id, label = (args.chat_id, args.topic or args.chat_id)
    else:
        chat_id, label = _resolve_registry(args.chat)
    body = _enforce_marker(text)
    content = _html_body(body, images)
    if args.via == "gsk" and len(content.encode()) > 120000:
        raise ValueError("connector-content-too-large; reduce text or image size")
    print(
        f"=== teams-send {('SEND' if args.yes else 'PREVIEW (nothing sent; add --yes to send)')} ==="
    )
    print(f"to      : {label}")
    print(f"chat id : {chat_id}")
    for pth in images:
        print(f"image   : {pth} ({pth.stat().st_size // 1024} KB)")
    if args.reply_to:
        print(f"reply to: message {args.reply_to} (quoted)")
    mention_ids, mention_names = ([], [])
    if args.mention:
        mention_ids, mention_names = _resolve_mentions(chat_id, args.mention)
        for i, nm in enumerate(mention_names):
            print(f"mention : {{mention_{i}}} -> {nm}")
    print(f"message :\n{body}\n")
    if not args.yes:
        return
    if args.via == "gsk":
        cmd = [
            *SETTINGS.get("gsk_command", ["gsk", "microsoft_teams", "send"]),
            "--chat_id",
            chat_id,
            "--content",
            content,
        ]
        if args.reply_to:
            cmd += ["--reply_to_message_id", args.reply_to]
        if mention_ids:
            cmd += [
                "--mention_user_ids",
                *mention_ids,
                "--mention_user_names",
                *mention_names,
            ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
        except subprocess.TimeoutExpired:
            raise ValueError(
                "send-result-unconfirmed; inspect the chat before retrying"
            ) from None
        msg_id = None
        try:
            out = json.loads(r.stdout)
            msg_id = (out.get("data") or {}).get("message_id")
            ok = r.returncode == 0 and out.get("status") == "ok" and msg_id
        except (ValueError, AttributeError, TypeError):
            ok = False
        if not ok:
            raise ValueError(
                "connector-send-unconfirmed; inspect the chat before retrying"
            )
        resp = {"id": msg_id}
    else:
        tok = _msal_token(SEND_CACHE, SEND_SCOPES, interactive=False)
        if not tok:
            sys.exit("no send token — run: teams-send login")
        resp = _graph(
            "POST",
            f"/chats/{quote(chat_id, safe='')}/messages",
            tok,
            {"body": {"contentType": "html", "content": _html_body(body, [])}},
        )
    identifier = _message_id(resp)
    _finish_delivery(
        {
            "via": f"send--yes/{args.via}",
            "chat_id": chat_id,
            "topic": label,
            "msg_id": identifier,
            **(
                {"images": [f"{p.name}:{p.stat().st_size}" for p in images]}
                if images
                else {}
            ),
        },
        body,
    )
    print(f"sent to [{label}] message id {identifier}")


def cmd_login(_args):
    tok = _msal_token(SEND_CACHE, SEND_SCOPES, interactive=True)
    if not tok:
        raise ValueError("login-failed")
    print("send token acquired")


def cmd_propose(args):
    text = args.message or sys.stdin.read()
    if not text.strip():
        sys.exit("empty message")
    if args.chat_id:
        chat_id, topic = (args.chat_id, args.topic or args.chat_id)
    else:
        chat_id, topic = _resolve_registry(args.chat)
    item = {
        "id": uuid.uuid4().hex[:8],
        "chat_id": chat_id,
        "chat_topic": topic,
        "message": _enforce_marker(text),
        "created_ts": time.time(),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_write(
        _proposal_path(item["id"]), json.dumps(item, ensure_ascii=True, indent=1)
    )
    print(f"queued {item['id']} -> [{topic}]")
    print(f"  preview: {item['message'][:120]}")
    print(
        "  Approve with this config: "
        + shlex.join(
            ["teams-send", "--config", str(CONFIG_PATH), "approve", item["id"]]
        )
    )
    print(
        f"  (expires in {TTL_SECONDS // 60} min; nothing is sent until approved on a real terminal)"
    )


def cmd_list(_args):
    pending = _load_pending()
    if not pending:
        print("(no pending proposals)")
    for pid, it in sorted(pending.items(), key=lambda kv: kv[1]["created_ts"]):
        age = int(time.time() - it["created_ts"])
        print(f"{pid}  [{it['chat_topic']}]  age {age // 60}m  {it['message'][:100]!r}")


def cmd_reject(args):
    f = _proposal_path(args.id)
    if f.exists():
        f.unlink()
        print(f"rejected {args.id}")
    else:
        print("no such pending id")


def _confirm_proposal(item):
    try:
        with open("/dev/tty", "r") as tty_r, open("/dev/tty", "w") as tty_w:
            tty_w.write(
                f"\n=== teams-send approval ===\nto      : {item['chat_topic']}\nchat id : {item['chat_id']}\nmessage :\n{item['message']}\n\nsend this AS YOU? type exactly 'yes': "
            )
            tty_w.flush()
            return tty_r.readline().strip() == "yes"
    except OSError:
        raise ValueError("approve-requires-interactive-terminal") from None


def cmd_approve(args):
    proposal = _proposal_path(args.id)
    pending = _load_pending()
    item = pending.get(args.id)
    if not item:
        sys.exit(f"no pending proposal {args.id} (expired or never existed)")
    if not _confirm_proposal(item):
        print("not confirmed; proposal kept in queue")
        return
    tok = _msal_token(SEND_CACHE, SEND_SCOPES, interactive=False)
    if not tok:
        sys.exit("no send token — run: teams-send login")
    body = _enforce_marker(item["message"])
    resp = _graph(
        "POST",
        f"/chats/{quote(item['chat_id'], safe='')}/messages",
        tok,
        {"body": {"contentType": "html", "content": _html_body(body, [])}},
    )
    identifier = _message_id(resp)
    _finish_delivery(
        {
            "id": args.id,
            "chat_id": item["chat_id"],
            "topic": item["chat_topic"],
            "msg_id": identifier,
        },
        body,
        proposal,
    )
    print(f"sent to [{item['chat_topic']}] message id {identifier}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--config",
        default=os.environ.get("TEAMS_SEND_CONFIG")
        or str(
            pathlib.Path(
                os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config"
            )
            / "teams-send/config.json"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    pc = sub.add_parser("chats")
    pc.add_argument("filter", nargs="?", default="")
    pc.add_argument("--refresh", action="store_true")
    pc.set_defaults(fn=cmd_chats)
    ps = sub.add_parser("send")
    target = ps.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat")
    target.add_argument("--chat-id")
    ps.add_argument("--topic")
    ps.add_argument("-m", "--message")
    ps.add_argument(
        "--yes", action="store_true", help="actually send (default is preview only)"
    )
    ps.add_argument(
        "--via",
        choices=["graph", "gsk"],
        default="graph",
        help="transport: direct Graph token (default) or gsk connector",
    )
    ps.add_argument(
        "--image",
        action="append",
        default=[],
        help="inline image file to embed (repeatable; requires --via gsk)",
    )
    ps.add_argument(
        "--reply-to",
        dest="reply_to",
        help="message id to quote-reply to (requires --via gsk)",
    )
    ps.add_argument(
        "--mention",
        action="append",
        default=[],
        help="chat member to @mention (name substring, repeatable; text must contain {mention_N} placeholders; requires --via gsk)",
    )
    ps.set_defaults(fn=cmd_send)
    pp = sub.add_parser("propose")
    target = pp.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--chat", help="topic/member substring, must match exactly one chat"
    )
    target.add_argument("--chat-id", help="exact Teams chat id")
    pp.add_argument("--topic", help="display label when using --chat-id")
    pp.add_argument("-m", "--message", help="message text (or pipe via stdin)")
    pp.set_defaults(fn=cmd_propose)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    pa = sub.add_parser("approve")
    pa.add_argument("id")
    pa.set_defaults(fn=cmd_approve)
    pr = sub.add_parser("reject")
    pr.add_argument("id")
    pr.set_defaults(fn=cmd_reject)
    args = p.parse_args(argv)
    try:
        configure(args.config)
        if (
            args.cmd in ("send", "propose")
            and not (args.chat_id or args.chat or "").strip()
        ):
            raise ValueError("chat-selector-empty")
        args.fn(args)
        return 0
    except ValueError as error:
        print("FAIL " + str(error), file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError):
        print("FAIL operation-data-or-file-error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
