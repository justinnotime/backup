"""Caller-configured, read-only Slack conversation archiving."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

API = "https://slack.com/api"
RATE_DELAY = 1.3
PAGE_LIMIT = 200
DRY_RUN = False
OUTPUT_DIR = None
STATE_FILE = None
_last_request = 0.0


class ArchiveError(Exception):
    pass


def log(message):
    print(message, flush=True)


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d{1,6}", value):
        raise ArchiveError("invalid Slack timestamp")
    return Decimal(value)


def component(value):
    if not isinstance(value, str) or not value or value in (".", "..") or any(c in value for c in "/\\\x00\r\n"):
        raise ArchiveError("workspace names and output aliases must be single path components")
    return value


def output_path(*parts):
    path = OUTPUT_DIR.joinpath(*(component(p) for p in parts))
    if not path.resolve().is_relative_to(OUTPUT_DIR.resolve()):
        raise ArchiveError("archive path escapes the configured output directory")
    return path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ArchiveError("Slack API redirect refused")


_opener = urllib.request.build_opener(NoRedirect())


def api_call(token, method, **params):
    global _last_request
    if method not in {"users.list", "users.info", "users.conversations", "conversations.history", "conversations.replies"}:
        raise ArchiveError("unsupported Slack read method")
    for attempt in range(4):
        time.sleep(max(0, RATE_DELAY - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{API}/{method}?{query}", headers={"Authorization": f"Bearer {token}"})
        delay = 5
        try:
            with _opener.open(req, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise ArchiveError(f"{method}: HTTP {exc.code}") from None
            retry = exc.headers.get("Retry-After", "30")
            delay = int(retry) if retry.isdigit() else 30
        except (OSError, ValueError):
            # Never interpolate transport exceptions, which may echo request data.
            pass
        else:
            if not isinstance(data, dict):
                raise ArchiveError(f"{method}: invalid response")
            if data.get("ok") is True:
                return data
            error = data.get("error", "unknown_error")
            if error != "ratelimited":
                label = error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,80}", error) else "unknown_error"
                raise ArchiveError(f"{method}: {label}")
            delay = int(data.get("retry_after", 30)) if str(data.get("retry_after", "")).isdigit() else 30
        if attempt < 3:
            time.sleep(delay)
    raise ArchiveError(f"{method}: retries exhausted")


def paginate(token, method, list_key, max_pages=0, **params):
    """Exhaust cursors, or Slack's timestamp pagination when no cursor exists."""
    items, cursor, cursors, pages = [], None, set(), 0
    while True:
        data = api_call(token, method, cursor=cursor, limit=PAGE_LIMIT, **params)
        page = data.get(list_key)
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ArchiveError(f"{method}: missing or invalid {list_key}")
        items.extend(page)
        pages += 1
        metadata = data.get("response_metadata") or {}
        if not isinstance(metadata, dict):
            raise ArchiveError(f"{method}: invalid pagination metadata")
        following = metadata.get("next_cursor") or None
        if following is not None and not isinstance(following, str):
            raise ArchiveError(f"{method}: invalid pagination cursor")
        if not following and not data.get("has_more"):
            return items
        if max_pages and pages >= max_pages:
            raise ArchiveError(f"{method}: page limit reached with data still pending")
        if following:
            if following in cursors:
                raise ArchiveError(f"{method}: repeated pagination cursor")
            cursors.add(following)
            cursor = following
            continue
        if method not in {"conversations.history", "conversations.replies"} or not page:
            raise ArchiveError(f"{method}: incomplete page without a continuation")
        # History is newest first; replies are oldest first. Retain the other
        # time bound, and exclude the boundary already returned on this page.
        key = "latest" if method == "conversations.history" else "oldest"
        boundary = (min if key == "latest" else max)([m.get("ts") for m in page], key=timestamp)
        previous = params.get(key)
        if previous and ((timestamp(boundary) >= timestamp(previous)) if key == "latest" else (timestamp(boundary) <= timestamp(previous))):
            raise ArchiveError(f"{method}: timestamp pagination made no progress")
        params[key] = boundary
        params["inclusive"] = "false"
        cursor = None


class Users(dict):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def resolve(self, uid):
        if not uid:
            return None
        if uid not in self:
            data = api_call(self.token, "users.info", user=uid)
            profile = (data.get("user") or {}).get("profile") or {}
            self[uid] = profile.get("display_name") or profile.get("real_name") or uid
        return self[uid]


def load_users(token):
    users = Users(token)
    for user in paginate(token, "users.list", "members"):
        if not user.get("id"):
            raise ArchiveError("users.list: user identity missing")
        profile = user.get("profile") or {}
        users[user["id"]] = profile.get("display_name") or profile.get("real_name") or user.get("name") or user["id"]
    return users


def list_conversations(token, users):
    conversations = paginate(token, "users.conversations", "channels", types="public_channel,private_channel,mpim,im", exclude_archived="true")
    for conv in conversations:
        if not conv.get("id"):
            raise ArchiveError("conversation identity missing")
        if conv.get("is_im"):
            conv["_name"] = users.resolve(conv.get("user")) or conv.get("user", "dm")
            conv["_type"] = "im"
        elif conv.get("is_mpim"):
            conv["_name"] = (conv.get("name") or "").replace("mpdm-", "").replace("--", ", ").rstrip("-1")
            conv["_type"] = "mpim"
        else:
            conv["_name"] = conv.get("name") or conv["id"]
            conv["_type"] = "private" if conv.get("is_private") else "channel"
    return conversations


def workspaces(cfg, base, token_dir=None):
    entries = cfg.get("workspaces", [])
    if not isinstance(entries, list):
        raise ArchiveError("workspaces must be a list")
    result, names = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArchiveError("workspace must be a mapping")
        ws = dict(entry)
        name = component(ws.get("name"))
        if name in names:
            raise ArchiveError("duplicate workspace name")
        names.add(name)
        for key, default in (("mode", "whitelist"), ("chats", []), ("bootstrap_days", 14), ("max_pages", 0)):
            ws.setdefault(key, cfg.get(key, default))
        if ws["mode"] not in ("whitelist", "blacklist") or not isinstance(ws["chats"], list):
            raise ArchiveError("invalid conversation selection")
        for item in ws["chats"]:
            match = item.get("match") if isinstance(item, dict) else item
            if not isinstance(match, str) or not match.strip():
                raise ArchiveError("each conversation selection needs a nonempty match")
            if isinstance(item, dict) and "alias" in item:
                component(item["alias"])
        for key in ("bootstrap_days", "max_pages"):
            if isinstance(ws[key], bool) or not isinstance(ws[key], int) or ws[key] < 0:
                raise ArchiveError(f"{key} must be a nonnegative integer")
        token = ws.get("token_file")
        if not token and token_dir:
            token = str(Path(token_dir) / f"slack-token-{name}")
        if not isinstance(token, str) or not token:
            raise ArchiveError(f"workspace {name}: token_file is required")
        path = Path(token).expanduser()
        ws["token_file"] = path if path.is_absolute() else base / path
        result.append(ws)
    return result


def select_conversations(conversations, ws):
    result = []
    for conv in conversations:
        text = f"{conv['_name']} | {conv['id']}".lower()
        hit = None
        for entry in ws["chats"]:
            item = entry if isinstance(entry, dict) else {"match": entry}
            if item["match"].lower() in text:
                hit = item
                break
        if (ws["mode"] == "whitelist") == (hit is not None):
            alias = (hit or {}).get("alias") if ws["mode"] == "whitelist" else None
            result.append((conv, component(alias or slugify(conv["_name"]))))
    return result


def read_token(ws):
    try:
        value = ws["token_file"].read_text().strip()
    except OSError:
        raise ArchiveError(f"workspace {ws['name']}: cannot read token file") from None
    if not value or any(c.isspace() for c in value):
        raise ArchiveError(f"workspace {ws['name']}: token file is empty or invalid")
    return value


def load_state():
    if not STATE_FILE.exists():
        return {"version": 1, "channels": {}}
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        raise ArchiveError("cannot read synchronization state") from None
    if not isinstance(state, dict) or state.get("version", 1) != 1 or not isinstance(state.get("channels"), dict):
        raise ArchiveError("unsupported or malformed synchronization state")
    for value in state["channels"].values():
        if not isinstance(value, dict):
            raise ArchiveError("invalid channel state")
        for key in ("watermark", "archive_from", "scanned_before"):
            if value.get(key) is not None:
                timestamp(value[key])
        if "slug" in value:
            component(value["slug"])
    return state


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, STATE_FILE)


def archive_start(directory, ws, cstate, started):
    """Preserve the original archive range across upgrades and later runs."""
    if cstate.get("archive_from"):
        return cstate["archive_from"]
    candidates = [f"{max(0, float(started) - ws['bootstrap_days'] * 86400):.6f}"]
    if cstate.get("watermark"):
        candidates.append(cstate["watermark"])
    for file in directory.glob("????-??.md"):
        if not file.resolve().is_relative_to(OUTPUT_DIR.resolve()):
            raise ArchiveError("archive file escapes output directory")
        candidates.extend(archived_ids(file.read_text()))
    return min(candidates, key=timestamp)


def fetch_channel(token, conv, oldest, latest, max_pages, users):
    # Discover threads across ALL retained parent history. A first reply may
    # arrive on a parent older than both the last run and the archive start.
    raw = paginate(token, "conversations.history", "messages", max_pages, channel=conv["id"], latest=latest)
    messages = []
    for parent in raw:
        ts = parent.get("ts")
        value = timestamp(ts)
        if timestamp(oldest) <= value <= timestamp(latest):
            normalized = norm_message(parent, users)
            if normalized:
                messages.append(normalized)
        # Unchanged threads need no reply request. If Slack omits latest_reply,
        # query the thread instead of inferring that it has no new replies.
        if parent.get("reply_count") and parent.get("thread_ts", ts) == ts:
            last_reply = parent.get("latest_reply")
            if last_reply and timestamp(last_reply) < timestamp(oldest):
                continue
            replies = paginate(token, "conversations.replies", "messages", max_pages, channel=conv["id"], ts=ts, oldest=oldest, latest=latest, inclusive="true")
            for reply in replies:
                reply_ts = reply.get("ts")
                value = timestamp(reply_ts)
                if reply_ts != ts and timestamp(oldest) <= value <= timestamp(latest):
                    normalized = norm_message(reply, users)
                    if normalized:
                        messages.append(normalized)
    return messages


def cmd_sync(wss):
    state = load_state()
    started = f"{time.time():.6f}"
    total = 0
    for ws in wss:
        token = read_token(ws)
        users = load_users(token)
        conversations = list_conversations(token, users)
        selected = select_conversations(conversations, ws)
        log(f"slack/{ws['name']}: {len(conversations)} conversations visible, {len(selected)} selected")
        owners = {value['slug']: key for key, value in state['channels'].items() if key.startswith(ws['name'] + '/') and value.get('slug')}
        for conv, slug in selected:
            cid = f"{ws['name']}/{conv['id']}"
            cstate = state["channels"].setdefault(cid, {})
            slug = cstate.get("slug", slug)
            if owners.get(slug, cid) != cid:
                slug = f"{slug}-{conv['id']}"
            if owners.get(slug, cid) != cid:
                raise ArchiveError("ambiguous output directory ownership")
            owners[slug] = cid
            directory = output_path(ws["name"], slug)
            start = archive_start(directory, ws, cstate, started)
            # Legacy watermarks skipped late replies. The first upgraded run
            # rechecks the archive range; later runs overlap the last start.
            oldest = cstate.get("scanned_before", start)
            messages = fetch_channel(token, conv, oldest, started, ws["max_pages"], users)
            front = {"platform": "slack", "workspace": ws["name"], "channel_id": conv["id"], "channel_name": conv["_name"], "channel_type": conv["_type"]}
            count = append_messages(directory, front, conv["_name"], messages)
            cstate.update(slug=slug, archive_from=start, scanned_before=started, watermark=started)
            total += count
            log(f"  {ws['name']}/{slug}: +{count} messages")
    # No workspace/channel failure may promote ANY staged progress.
    save_state(state)
    log(f"slack: {'dry run, ' if DRY_RUN else 'done, '}{total} new messages")


def cmd_list_channels(wss):
    for ws in wss:
        token = read_token(ws)
        users = load_users(token)
        for conv in sorted(list_conversations(token, users), key=lambda c: (c["_type"], c["_name"])):
            log(f"{ws['name']} [{conv['_type']}] {conv['_name']} ({conv['id']})")


def cmd_peek(wss, match, limit):
    candidates = []
    for ws in wss:
        token = read_token(ws)
        users = load_users(token)
        conversations = list_conversations(token, users)
        candidates.extend((ws, token, users, c) for c in conversations)
    exact = [item for item in candidates if match in (item[3]['id'], f"{item[0]['name']}/{item[3]['id']}")]
    matches = exact or [item for item in candidates if match.lower() in f"{item[0]['name']} | {item[3]['_name']} | {item[3]['id']}".lower()]
    if len(matches) != 1:
        raise ArchiveError(f"peek must match one conversation; found {len(matches)}")
    ws, token, users, conv = matches[0]
    # Reading all retained parents also finds recent replies in older threads.
    messages = fetch_channel(token, conv, "0.000000", f"{time.time():.6f}", ws["max_pages"], users)
    unique = {m['id']: m for m in messages}
    log(f"# {conv['_name']} ({ws['name']}/{conv['id']})\n")
    for message in sorted(unique.values(), key=lambda m: timestamp(m['id']))[-limit:]:
        log(f"### {message['ts']} — {message['sender']}\n{message['body']}\n")


def publication_settings(cfg):
    settings = cfg.get("publish")
    if not isinstance(settings, dict):
        raise ArchiveError("publish configuration is required")
    command = settings.get("command")
    if not isinstance(command, list) or not command or any(
        not isinstance(value, str) or not value or "\x00" in value for value in command
    ):
        raise ArchiveError("publish.command must be a nonempty argument array")
    for key in ("base_env", "state_env"):
        if not isinstance(settings.get(key), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings[key]):
            raise ArchiveError("publish requires base_env and state_env variable names")
    if settings["base_env"] == settings["state_env"]:
        raise ArchiveError("publication worktree and state variables must differ")
    return settings


def publication_output(base):
    try:
        relative = OUTPUT_DIR.resolve().relative_to(base.resolve())
    except ValueError:
        raise ArchiveError("publication output must be inside base_dir") from None
    if relative == Path("."):
        raise ArchiveError("publication output must be a subdirectory of base_dir")
    return relative


def cmd_publish(cfg, config_path, base, token_dir):
    settings = publication_settings(cfg)
    relative = publication_output(base)
    values = {"base_dir": str(base), "output_dir": str(relative),
              "state_dir": str(STATE_FILE.parent),
              "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    command = []
    for argument in settings["command"]:
        for key, value in values.items():
            argument = argument.replace("{" + key + "}", value)
        command.append(argument)
    # The configured publisher appends its writer command to this prefix. It
    # owns locks, isolated output, staged state, and publication failure policy.
    command += [sys.executable, "-B", str(Path(__file__).resolve()),
                "--config", str(config_path), "--base-dir", str(base),
                "--output-dir", str(relative), "--state-file", str(STATE_FILE),
                "--transaction-writer"]
    if token_dir:
        command += ["--token-dir", str(token_dir)]
    return subprocess.run(command, check=False).returncode


def main(argv=None):
    global OUTPUT_DIR, STATE_FILE, DRY_RUN, RATE_DELAY, PAGE_LIMIT, _last_request
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--state-file")
    parser.add_argument("--token-dir")
    parser.add_argument("--dry-run", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-channels", action="store_true")
    modes.add_argument("--peek", metavar="MATCH")
    modes.add_argument("--publish", action="store_true", help="run the configured transactional publisher")
    modes.add_argument("--transaction-writer", action="store_true", help="write into the publisher's worktree and staged state")
    parser.add_argument("--peek-limit", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).expanduser().resolve()
        settings = yaml.safe_load(config_path.read_text())
        if not isinstance(settings, dict) or not isinstance(settings.get("slack"), dict):
            raise ArchiveError("configuration requires a slack mapping")
        cfg = settings["slack"]
        if args.base_dir:
            base = Path(args.base_dir).expanduser().resolve()
        else:
            base_value = cfg.get("base_dir")
            base = Path(base_value).expanduser() if base_value else config_path.parent
            base = (base if base.is_absolute() else config_path.parent / base).resolve()
        def path(value, label):
            if not isinstance(value, str) or not value:
                raise ArchiveError(f"{label} is required")
            p = Path(value).expanduser()
            return p if p.is_absolute() else base / p
        OUTPUT_DIR = path(args.output_dir or cfg.get("output_dir"), "output_dir")
        STATE_FILE = path(args.state_file or cfg.get("state_file"), "state_file")
        token_value = args.token_dir or cfg.get("token_dir")
        token_dir = path(token_value, "token_dir") if token_value else None
        DRY_RUN = args.dry_run
        RATE_DELAY = float(cfg.get("request_interval", 1.3))
        PAGE_LIMIT = cfg.get("page_size", 200)
        if not 0 <= RATE_DELAY <= 300 or not isinstance(PAGE_LIMIT, int) or isinstance(PAGE_LIMIT, bool) or not 1 <= PAGE_LIMIT <= 999 or args.peek_limit < 1:
            raise ArchiveError("invalid request interval, page size, or peek limit")
        _last_request = 0.0
        if DRY_RUN and (args.publish or args.transaction_writer):
            raise ArchiveError("dry-run cannot be combined with publication modes")
        # Credentials keep their configured base when output is relocated.
        wss = workspaces(cfg, base, token_dir)
        if args.transaction_writer:
            settings = publication_settings(cfg)
            relative = publication_output(base)
            staged_base = os.environ.get(settings["base_env"], "")
            staged_state = os.environ.get(settings["state_env"], "")
            if not Path(staged_base).is_absolute() or not Path(staged_state).is_absolute():
                raise ArchiveError("publisher must provide absolute worktree and staged-state paths")
            base = Path(staged_base).resolve()
            OUTPUT_DIR = base / relative
            STATE_FILE = Path(staged_state) / STATE_FILE.name
        if cfg.get("enabled", True) is False:
            log("slack: disabled by configuration")
        elif args.publish:
            return cmd_publish(cfg, config_path, base, token_dir)
        elif args.list_channels:
            cmd_list_channels(wss)
        elif args.peek:
            cmd_peek(wss, args.peek, args.peek_limit)
        else:
            if not wss:
                raise ArchiveError("no workspaces configured")
            cmd_sync(wss)
        return 0
    except (ArchiveError, OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        # Parser errors can contain configuration lines; report only our own
        # messages. Never print credential values or arbitrary remote payloads.
        log(f"ERROR: {exc if isinstance(exc, ArchiveError) else type(exc).__name__}")
        return 1



def slugify(text: str, max_len=60) -> str:
    text = re.sub(r'[<>:"/\\|?*#\[\]@]', "", str(text))
    text = re.sub(r"\s+", "-", text.strip()).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


# ---------------------------------------------------------------------------
# Rendering (same month-file shape as the teams/whatsapp mirrors)
# ---------------------------------------------------------------------------

ID_RE = re.compile(r"(?m)^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — [^\n]*\n<!-- id: (\d+\.\d{1,6}) -->\n")
MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def archived_ids(content):
    # Only record headers generate identities; quoted markers in message text
    # are not records. Escape the marker in new bodies to prevent forged headers.
    return {ident for display, ident in ID_RE.findall(content)
            if ts_iso(ident).replace("T", " ").replace("Z", "") == display}


def yaml_str(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ts_iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_of(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m")


SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "group_join",
    "group_leave", "bot_add", "bot_remove", "reminder_add",
}


def norm_message(m: dict, users: dict) -> dict | None:
    if m.get("subtype") in SKIP_SUBTYPES:
        return None
    ts = m.get("ts")
    if not ts:
        return None
    text = (m.get("text") or "").strip()
    text = MENTION_RE.sub(lambda mo: "@" + (users.resolve(mo.group(1)) or mo.group(1)), text)
    for f in m.get("files") or []:
        text = (text + "\n\n" if text else "") + f"*[file: {f.get('name') or f.get('id')}]*"
    if not text:
        return None
    sender = users.resolve(m.get("user")) \
        or (m.get("bot_profile") or {}).get("name") \
        or m.get("username") or m.get("user") or "(unknown)"
    if m.get("thread_ts") and m["thread_ts"] != ts:
        sender = f"{sender} (thread reply)"
    return {"id": ts, "ts": ts_iso(ts), "sender": sender, "body": text}


def append_messages(chat_dir: Path, front: dict, heading: str, messages: list[dict]) -> int:
    written = 0
    by_month: dict[str, list[dict]] = {}
    for m in sorted(messages, key=lambda x: timestamp(x["id"])):
        by_month.setdefault(month_of(m["id"]), []).append(m)
    for month, msgs in sorted(by_month.items()):
        path = chat_dir / f"{month}.md"
        if not path.resolve().is_relative_to(OUTPUT_DIR.resolve()):
            raise ArchiveError("archive file escapes output directory")
        if path.exists():
            content = path.read_text()
            seen = archived_ids(content)
        else:
            fm = ["---"] + [f"{k}: {yaml_str(v)}" for k, v in front.items()] \
                + [f'month: "{month}"', "times: UTC", "---", "", f"# {month} — {heading}", ""]
            content = "\n".join(fm)
            seen = set()
        chunks = []
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            ts_disp = m["ts"].replace("T", " ").replace("Z", "")
            sender = m['sender'].replace("\r", " ").replace("\n", " ")
            body = m['body'].replace("<!-- id:", "&lt;!-- id:")
            chunks.append(f"\n### {ts_disp} — {sender}\n<!-- id: {m['id']} -->\n\n{body}\n")
            written += 1
        if chunks and not DRY_RUN:
            chat_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content.rstrip("\n") + "\n" + "".join(chunks))
    return written



if __name__ == "__main__":
    sys.exit(main())
