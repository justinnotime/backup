#!/usr/bin/env python3
"""Archive selected Microsoft Teams chats with caller-owned configuration.

Reads through Microsoft Graph or the gsk command. No message sending.
Archives, credentials, selection, and scheduling belong to the caller.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUTPUT_DIR = Path()
STATE_FILE = Path()
CONFIG_FILE = Path()
REGISTRY_FILE = None
DRY_RUN = False
GSK_COMMAND = "gsk"
RELAY_DIR = "/teams-archive"


class ArchiveError(Exception):
    """An incomplete or invalid archive operation; safe to retry."""


RATE_DELAY = 1.0       # seconds between gsk calls
PAGE_SIZE = 50         # read_chat/list_chats page size (API max)
OVERLAP_MIN = 15       # re-fetch overlap before the watermark; dedupe by message id
MAX_LIST_PAGES = 20    # list_chats pagination cap (Graph often returns <50/page)

DUMP_RAW = False
LAST_FETCH_PAGES = 0   # pages fetched by the most recent paginated backend call


def log(msg: str):
    print(msg, flush=True)


def warn(msg: str):
    print(f"  [WARN] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# gsk plumbing
# ---------------------------------------------------------------------------

def run_gsk(args: list[str], timeout=120) -> dict | None:
    """Run a gsk command, return parsed JSON envelope, or None on failure."""
    cmd = [GSK_COMMAND] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        warn(f"gsk timed out: {' '.join(args[:3])}")
        return None
    if result.returncode:
        warn(f"gsk failed (exit {result.returncode})")
        return None
    output = result.stdout
    json_start = output.find("{")
    if json_start == -1:
        warn("gsk returned no JSON")
        return None
    try:
        data = json.loads(output[json_start:])
    except json.JSONDecodeError as e:
        warn(f"gsk JSON parse failed: {e}")
        return None
    if not isinstance(data, dict) or data.get("success") is False or (isinstance(data.get("data"), dict) and data["data"].get("success") is False):
        warn("gsk reported a failed operation")
        return None
    if DUMP_RAW and not DRY_RUN:
        dump_dir = OUTPUT_DIR / ".debug"
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        (dump_dir / f"{args[1] if len(args) > 1 else args[0]}-{stamp}.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False))
    return data


def result_text(data: dict) -> str:
    """The prose `data.result` field of a gsk envelope (used for error sniffing)."""
    try:
        return str((data.get("data") or {}).get("result") or "")
    except AttributeError:
        return ""


def not_configured(data: dict | None) -> bool:
    return data is not None and "not configured" in result_text(data).lower()


def extract_list(data: dict, *key_candidates: str) -> list:
    """Find the structured list payload in a gsk envelope, tolerating shape drift.

    Preference order: session_state[key], data[key], then any list-of-dicts value
    found in session_state / data.
    """
    for container in (data.get("session_state"), data.get("data")):
        if not isinstance(container, dict):
            continue
        for key in key_candidates:
            val = container.get(key)
            if isinstance(val, list):
                return val
    for container in (data.get("session_state"), data.get("data")):
        if not isinstance(container, dict):
            continue
        for val in container.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    return []


def extract_cursor(data: dict) -> str | None:
    for container in (data.get("session_state"), data.get("data")):
        if isinstance(container, dict):
            cur = container.get("next_cursor")
            if cur:
                return str(cur)
    return None


# ---------------------------------------------------------------------------
# Backend dispatch (read-only in both backends)
# ---------------------------------------------------------------------------

BACKEND = "gsk"          # set from config/--backend in main()
GRAPH_CFG: dict = {}     # teams.graph section of the config


def list_chats() -> list[dict] | None:
    return list_chats_graph() if BACKEND == "graph" else list_chats_gsk()


def read_chat(chat_id: str, since: str | None, max_pages: int,
              *, exact_pages: bool = False) -> tuple[list[dict], bool]:
    """Returns (messages, complete). complete=False → truncated/errored read.
    exact_pages=True honors max_pages literally (peek wants only the newest
    page or two); the default keeps the graph backend's 200-page backlog floor
    that sync correctness depends on."""
    if BACKEND == "graph":
        return read_chat_graph(chat_id, since, max_pages, exact_pages=exact_pages)
    return read_chat_gsk(chat_id, since, max_pages)


# ---------------------------------------------------------------------------
# gsk backend
# ---------------------------------------------------------------------------

def list_chats_gsk() -> list[dict] | None:
    """All chats visible to the account. Returns None if connector unavailable."""
    global LAST_FETCH_PAGES
    LAST_FETCH_PAGES = 0
    chats, cursor = [], None
    for _ in range(MAX_LIST_PAGES):
        args = ["microsoft_teams", "list_chats", "--count", str(PAGE_SIZE)]
        if cursor:
            args += ["--cursor", cursor]
        LAST_FETCH_PAGES += 1
        data = run_gsk(args)
        if data is None:
            return None
        if not_configured(data):
            return None
        page = extract_list(data, "chats")
        chats.extend(page)
        cursor = extract_cursor(data)
        if not cursor:
            return chats
        if not page:
            return None
        time.sleep(RATE_DELAY)
    return None


def read_chat_gsk(chat_id: str, since: str | None, max_pages: int) -> tuple[list[dict], bool]:
    """All messages of one chat since `since` (ISO-8601). Returns (messages, complete)."""
    messages, cursor = [], None
    for _ in range(max_pages):
        args = ["microsoft_teams", "read_chat", "--chat_id", chat_id,
                "--limit", str(PAGE_SIZE)]
        if since:
            args += ["--since", since]
        if cursor:
            args += ["--cursor", cursor]
        data = run_gsk(args)
        if data is None or not_configured(data):
            raise ArchiveError("connector chat page could not be read")
        page = extract_list(data, "messages")
        messages.extend(page)
        cursor = extract_cursor(data)
        if not cursor:
            return messages, True
        if not page:
            return messages, False
        time.sleep(RATE_DELAY)
    warn(f"gsk read_chat hit the {max_pages}-page cap with a cursor still pending")
    return messages, False


# ---------------------------------------------------------------------------
# graph backend — delegated Chat.Read via MSAL device-code (no admin consent)
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ATTACHMENTS_ENABLED = False
GRAPH_SCOPES = ["Chat.Read"]


def graph_cache_path() -> Path:
    p = GRAPH_CFG.get("token_cache")
    if p:
        return Path(os.path.expanduser(p))
    raise ArchiveError("graph.token_cache is required")


def graph_token(interactive: bool = False) -> str | None:
    try:
        import msal
    except ImportError:
        warn("msal not installed (pip install msal); graph backend unavailable")
        return None
    cache = msal.SerializableTokenCache()
    cpath = graph_cache_path()
    if cpath.exists():
        cache.deserialize(cpath.read_text())
    app = msal.PublicClientApplication(
        GRAPH_CFG["client_id"],
        authority=f"https://login.microsoftonline.com/{GRAPH_CFG.get('tenant') or 'organizations'}",
        token_cache=cache)
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
    if not result and interactive:
        flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            warn("device login failed")
            return None
        print(flow["message"], flush=True)  # "go to https://microsoft.com/devicelogin, enter CODE"
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed and not DRY_RUN:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.touch(mode=0o600, exist_ok=True)
        cpath.write_text(cache.serialize())
        cpath.chmod(0o600)
    if result and "access_token" in result:
        return result["access_token"]
    if result:
        warn("Graph authentication failed; run --login with this configuration")
    return None


def graph_get(url: str, token: str, params: dict | None = None) -> dict | None:
    try:
        import requests
    except ImportError:
        warn("requests not installed (ships with msal; pip install requests); graph backend unavailable")
        return None
    for attempt in range(3):
        if not url.startswith(GRAPH_BASE + "/"):
            raise ArchiveError("Graph pagination URL is outside the configured API")
        try:
            r = requests.get(url, params=params, timeout=60,
                             headers={"Authorization": f"Bearer {token}"})
        except requests.RequestException:
            warn("Graph request failed")
            return None
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", "5")), 60))
            continue
        if r.ok:
            data = r.json()
            if DUMP_RAW and not DRY_RUN:
                dump_dir = OUTPUT_DIR / ".debug"
                dump_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                tag = re.sub(r"[^0-9A-Za-z._-]", "_", url.split("?")[0].rsplit("/v1.0/", 1)[-1])[-80:]
                (dump_dir / f"graph-{tag}-{stamp}.json").write_text(
                    json.dumps(data, indent=1, ensure_ascii=False))
            return data
        warn(f"Graph request failed (HTTP {r.status_code})")
        return None
    warn("graph GET kept throttling; giving up this call")
    return None


def graph_paginate(url: str, token: str, params: dict, max_pages: int,
                   *, partial_ok: bool = False) -> tuple[list[dict], bool]:
    """Follow @odata.nextLink. Returns (items, complete) — complete=False when a
    request failed mid-run or the safety cap was hit with more data pending; the
    caller must NOT advance its watermark past an incomplete read. partial_ok
    silences the cap warning for callers that WANT a truncated newest-first
    read (peek); the return value still reports complete=False."""
    global LAST_FETCH_PAGES
    LAST_FETCH_PAGES = 0
    items, page = [], 0
    while url and page < max_pages:
        data = graph_get(url, token, params)
        if data is None:
            if partial_ok:
                raise ArchiveError("chat page could not be read")
            return items, False
        items.extend(data.get("value") or [])
        url, params = data.get("@odata.nextLink"), None  # nextLink embeds the query
        page += 1
        LAST_FETCH_PAGES = page
        time.sleep(RATE_DELAY)
    if url:
        if not partial_ok:
            warn(f"graph pagination hit the {max_pages}-page safety cap with more data pending")
        return items, False
    return items, True


def list_chats_graph() -> list[dict] | None:
    token = graph_token()
    if token is None:
        warn("graph backend: no token — run: python3 teams-archive --backend graph --login")
        return None
    # recency ordering is guaranteed, so the page cap only hides long-dormant
    # chats (the only $orderby Graph supports on this endpoint)
    chats, complete = graph_paginate(f"{GRAPH_BASE}/me/chats", token,
                                     {"$top": 50, "$expand": "members",
                                      "$orderby": "lastMessagePreview/createdDateTime desc"},
                                     MAX_LIST_PAGES)
    if not complete:
        warn("chat listing incomplete")
        return None
    return chats


def read_chat_graph(chat_id: str, since: str | None, max_pages: int,
                    *, exact_pages: bool = False) -> tuple[list[dict], bool]:
    token = graph_token()
    if token is None:
        return [], False
    params = {"$top": 50}
    if since:
        # Graph only allows $filter with a matching $orderby; createdDateTime
        # supports lt only, so incremental watermarks use lastModifiedDateTime gt
        # (also picks up edits; dedupe-by-id keeps the originally captured text).
        params["$orderby"] = "lastModifiedDateTime desc"
        params["$filter"] = f"lastModifiedDateTime gt {since}"
    # This endpoint only pages newest-first, so a SYNC backlog must drain to
    # exhaustion in one run before the watermark may advance — for sync,
    # max_pages is a generous safety bound (floored at 200), not a budget.
    # Peek reads pass exact_pages=True: they want only the newest page(s), and
    # newest-first means page 1 already holds the latest messages — flooring
    # them to 200 pages made every --peek of a long-lived chat drain its whole
    # history at one page per second.
    return graph_paginate(f"{GRAPH_BASE}/chats/{chat_id}/messages", token, params,
                          max_pages if exact_pages else max(max_pages, 200),
                          partial_ok=exact_pages)


# ---------------------------------------------------------------------------
# Normalization (tolerant to Graph-vs-connector field naming)
# ---------------------------------------------------------------------------

def chat_name(chat: dict) -> str:
    """Best display name for a chat: topic, else member names, else id tail."""
    topic = chat.get("topic") or chat.get("name") or chat.get("subject")
    if topic:
        return str(topic)
    members = chat.get("members") or chat.get("participants") or []
    names = []
    for m in members:
        if isinstance(m, dict):
            n = m.get("displayName") or m.get("display_name") or m.get("name")
            if n:
                names.append(str(n))
        elif isinstance(m, str):
            names.append(m)
    if names:
        return ", ".join(names[:4])
    cid = str(chat.get("id") or chat.get("chat_id") or "unknown")
    return f"chat-{cid[-12:]}"


def chat_id_of(chat: dict) -> str | None:
    cid = chat.get("id") or chat.get("chat_id")
    return str(cid) if cid else None


def chat_match_text(chat: dict) -> str:
    """Text a config `match:` is tested against: topic + member names."""
    parts = [chat_name(chat)]
    members = chat.get("members") or chat.get("participants") or []
    for m in members:
        if isinstance(m, dict):
            for k in ("displayName", "display_name", "name", "email"):
                if m.get(k):
                    parts.append(str(m[k]))
        elif isinstance(m, str):
            parts.append(m)
    return " | ".join(parts)


def norm_system_event(msg: dict, ev: dict) -> dict | None:
    """Mechanical one-liner for thread system events (call lifecycle, membership,
    rename). Meeting threads often carry ONLY these for weeks; dropping them all
    made every fetch normalize to zero — a repeated shape-drift warning at best,
    and a silent watermark jump past them once a normal message finally arrived
    (an image-only message)."""
    mid = msg.get("id") or msg.get("message_id")
    if not mid:
        return None
    created = (msg.get("createdDateTime") or msg.get("created_at")
               or msg.get("created") or msg.get("timestamp") or "")
    etype = str(ev.get("@odata.type") or "").rsplit(".", 1)[-1]
    etype = re.sub(r"EventMessageDetail$", "", etype) or "systemEvent"
    label = re.sub(r"(?<!^)(?=[A-Z])", " ", etype).lower()
    who = ""
    init = ev.get("initiator") or {}
    if isinstance(init, dict):
        user = init.get("user") or {}
        if isinstance(user, dict):
            who = user.get("displayName") or ""
    return {"id": str(mid), "ts": normalize_ts(created),
            "sender": who or "(system)", "body": f"*[{label}]*"}


# ---------------------------------------------------------------------------
# Bot/app cards: messages whose payload is an
# Adaptive Card or connector card (deployment notices, daily cost cards)
# carry no readable body -- the content lives in attachments[].content as a
# JSON string. Render the common elements to plain markdown; anything
# unrecognized degrades to the opaque attachment marker, never blocking the
# mirror. Cards need no downloads, so this works without gsk.
# ---------------------------------------------------------------------------

def _card_cell(text: str) -> str:
    return str(text).strip().replace("|", "\\|").replace("\n", " ")


def _card_texts(items) -> list[str]:
    """Flatten a Column's items to one text per element (rows going down)."""
    out = []
    for el in items or []:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "TextBlock":
            out.append(str(el.get("text") or "").strip())
        else:
            out.append(" ".join(t for t in _card_texts(el.get("items")) if t))
    return out


def _card_walk(elements) -> list[str]:
    from itertools import zip_longest
    parts = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        etype = el.get("type") or ""
        if etype == "TextBlock":
            txt = str(el.get("text") or "").strip()
            if txt:
                parts.append(f"**{txt}**" if el.get("weight") == "bolder" else txt)
        elif etype == "RichTextBlock":
            txt = "".join(str(i.get("text") or "") for i in el.get("inlines") or []
                          if isinstance(i, dict)).strip()
            if txt:
                parts.append(txt)
        elif etype == "FactSet":
            for f in el.get("facts") or []:
                if isinstance(f, dict):
                    parts.append(f"- **{str(f.get('title') or '').strip()}** "
                                 f"{str(f.get('value') or '').strip()}")
        elif etype == "ColumnSet":
            # Bots build tables as N columns whose items are the rows going
            # down; equal-height columns with a header row become a real table.
            cols = [[t for t in _card_texts(c.get("items"))]
                    for c in el.get("columns") or [] if isinstance(c, dict)]
            cols = [c for c in cols if any(x for x in c)]
            if len(cols) >= 2:
                rows = [r for r in zip_longest(*cols, fillvalue="")
                        if any(str(x).strip() for x in r)]
                if len(rows) >= 2:
                    lines = ["| " + " | ".join(_card_cell(x) for x in rows[0]) + " |",
                             "|" + "---|" * len(rows[0])]
                    lines += ["| " + " | ".join(_card_cell(x) for x in r) + " |"
                              for r in rows[1:]]
                    parts.append("\n".join(lines))
                elif rows:
                    # one visual row; bots emit tables as a ColumnSet per row,
                    # so tag it and let _merge_row_runs stitch adjacent rows
                    parts.append(("ROW", tuple(_card_cell(x) for x in rows[0])))
            elif cols:
                parts.extend(t for t in cols[0] if t)
        elif etype in ("Container", "Column"):
            parts.extend(_card_walk(el.get("items")))
        elif etype == "Table":
            rows = []
            for r in el.get("rows") or []:
                cells = [" ".join(_card_walk(c.get("items")))
                         for c in r.get("cells") or [] if isinstance(c, dict)]
                rows.append(cells)
            if rows:
                lines = ["| " + " | ".join(_card_cell(x) for x in rows[0]) + " |",
                         "|" + "---|" * len(rows[0])]
                lines += ["| " + " | ".join(_card_cell(x) for x in r) + " |"
                          for r in rows[1:]]
                parts.append("\n".join(lines))
        elif etype == "Image":
            if el.get("url"):
                parts.append(f"[card image]({el['url']})")
        elif etype == "ActionSet":
            for act in el.get("actions") or []:
                if isinstance(act, dict) and act.get("type") == "Action.OpenUrl" \
                        and act.get("url"):
                    parts.append(f"[{act.get('title') or act['url']}]({act['url']})")
    return parts


def _merge_row_runs(parts) -> list[str]:
    """Stitch consecutive single-row ColumnSets into one markdown table
    (first row of a run is the header)."""
    out, run = [], []

    def flush():
        nonlocal run
        if not run:
            return
        if len(run) == 1:
            out.append(" | ".join(x for x in run[0] if x))
        else:
            width = max(len(r) for r in run)
            rows = [list(r) + [""] * (width - len(r)) for r in run]
            lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            out.append("\n".join(lines))
        run = []

    for p in parts:
        if isinstance(p, tuple) and p and p[0] == "ROW":
            run.append(p[1])
        else:
            flush()
            out.append(p)
    flush()
    return out


def render_card(a) -> str | None:
    """attachments[] entry -> markdown, or None when it is not a card we read."""
    ctype = (a.get("contentType") or "").lower()
    raw = a.get("content")
    if not raw:
        return None
    try:
        card = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(card, dict):
        return None
    try:
        if ctype == "application/vnd.microsoft.card.adaptive":
            parts = _merge_row_runs(_card_walk(card.get("body")))
            for act in card.get("actions") or []:
                if isinstance(act, dict) and act.get("type") == "Action.OpenUrl" \
                        and act.get("url"):
                    parts.append(f"[{act.get('title') or act['url']}]({act['url']})")
            return "\n\n".join(p for p in parts if p) or None
        if ctype == "application/vnd.microsoft.teams.card.o365connector":
            parts = []
            if card.get("title"):
                parts.append(f"**{card['title']}**")
            if card.get("text"):
                parts.append(str(card["text"]))
            for s in card.get("sections") or []:
                if not isinstance(s, dict):
                    continue
                for k in ("activityTitle", "activitySubtitle", "text"):
                    if s.get(k):
                        parts.append(str(s[k]))
                for f in s.get("facts") or []:
                    if isinstance(f, dict):
                        parts.append(f"- **{str(f.get('name') or '').strip()}** "
                                     f"{str(f.get('value') or '').strip()}")
            return "\n\n".join(p for p in parts if p) or None
        if ctype in ("application/vnd.microsoft.card.hero",
                     "application/vnd.microsoft.card.thumbnail"):
            parts = [f"**{card['title']}**" if card.get("title") else "",
                     str(card.get("subtitle") or ""), str(card.get("text") or "")]
            return "\n\n".join(p for p in parts if p) or None
    except Exception:  # noqa: BLE001 -- a malformed card must not block the mirror
        return None
    return None


def render_message_reference(a) -> str | None:
    """Reply-quote attachment -> one-line marker with sender and preview."""
    raw = a.get("content")
    try:
        ref = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        return None
    if not isinstance(ref, dict):
        return None
    prev = str(ref.get("messagePreview") or "").strip().replace("\n", " ")
    who = ""
    sender = ref.get("messageSender") or {}
    if isinstance(sender, dict):
        u = sender.get("user") or {}
        if isinstance(u, dict):
            who = u.get("displayName") or ""
    if len(prev) > 100:
        prev = prev[:100] + "..."
    if who or prev:
        return f"*[reply to {who or 'message'}: {prev}]*"
    return f"*[reply to message {a.get('id')}]*"


def norm_message(msg, att=None) -> dict | None:
    """Normalize one message; None → skip (deleted, empty, unrepresentable)."""
    mtype = msg.get("messageType") or msg.get("message_type")
    deleted = bool(msg.get("deletedDateTime") or msg.get("deleted_at"))
    if mtype not in (None, "", "message"):
        # Graph reports meeting-thread system events as messageType
        # "systemEventMessage" / "unknownFutureValue" with an eventDetail
        # payload; represent those, drop other exotic types.
        ev = msg.get("eventDetail") or msg.get("event_detail")
        if isinstance(ev, dict) and not deleted:
            return norm_system_event(msg, ev)
        return None
    if deleted:
        return None

    mid = msg.get("id") or msg.get("message_id")
    created = (msg.get("createdDateTime") or msg.get("created_at")
               or msg.get("created") or msg.get("timestamp") or "")

    sender = ""
    frm = msg.get("from") or msg.get("sender") or {}
    if isinstance(frm, dict):
        user = frm.get("user") or {}
        app = frm.get("application") or {}  # bots/workflows post as applications
        sender = (user.get("displayName") if isinstance(user, dict) else None) \
            or (app.get("displayName") if isinstance(app, dict) else None) \
            or frm.get("displayName") or frm.get("display_name") or frm.get("name") or ""
    elif isinstance(frm, str):
        sender = frm
    sender = sender or "(unknown)"

    body = msg.get("body") or {}
    if isinstance(body, dict):
        content = body.get("content") or ""
        content_type = body.get("contentType") or body.get("content_type") or ""
    else:
        content, content_type = str(body), ""
    if not content:
        content = msg.get("content") or msg.get("text") or ""
        content_type = content_type or ""

    # Convert only when the source declares HTML. No '<'-sniffing: plain text
    # like 'List<String>' must stay verbatim (html2text would eat the <String>).
    inline_links = []
    if content_type.lower() == "html":
        img_alts = re.findall(r'<img\b[^>]*\balt="([^"]*)"', content, flags=re.I)
        n_imgs = len(re.findall(r"<img\b", content, flags=re.I))
        if att is not None and n_imgs:
            inline_links = att.inline_images(mid, content)
        content = html_to_markdown(content)
        if n_imgs and not content.strip():
            # html2text runs with ignore_images: an image-only message (pasted
            # screenshot, emoji rendered as <img>) must leave a trace, not
            # vanish (an image-only message). Teams puts the emoji character in alt=.
            alt_text = " ".join(a for a in img_alts if a.strip())
            content = alt_text or ("*[image]*" if n_imgs == 1 else f"*[{n_imgs} images]*")
    content = content.strip()
    if inline_links:
        content = (content + "\n\n" if content else "") + "\n".join(inline_links)

    atts = msg.get("attachments") or []
    att_lines = []
    for a in atts:
        if isinstance(a, dict):
            ctype = (a.get("contentType") or "").lower()
            line = None
            if ctype.startswith("application/vnd.microsoft.card") \
                    or ctype == "application/vnd.microsoft.teams.card.o365connector":
                line = render_card(a)
            elif ctype == "messagereference":
                line = render_message_reference(a)
            if line is not None and att is not None:
                # register for --backfill-attachments so already-mirrored
                # card/reply messages gain their content in place
                att.links_by_msg.setdefault(str(mid), []).append(line)
            if line is None:
                line = att.file_attachment(mid, a) if att is not None else None
            if line is None:
                name = a.get("name") or a.get("contentUrl") or a.get("id") or "attachment"
                line = f"*[attachment: {name}]*"
            att_lines.append(line)
    if att_lines:
        content = (content + "\n\n" if content else "") + "\n".join(att_lines)

    if not mid or not content:
        return None
    ts_iso = normalize_ts(created)
    return {"id": str(mid), "ts": ts_iso, "sender": sender, "body": content}



# ---------------------------------------------------------------------------
# Attachments: download inline images and file
# attachments for mirrored chats, store content-addressed under
# <chat>/attachments/ (Git-LFS tracked), maintain attachments-manifest.yaml.
# Transport is the gsk microsoft_teams connector (download_attachment); failed
# downloads fail the run so its state cannot advance past missing content.
# ---------------------------------------------------------------------------

_HOSTED_RE = re.compile(r"/messages/([^/]+)/hostedContents/([A-Za-z0-9=\-.]+)/\$value")
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/webp": ".webp", "application/pdf": ".pdf"}


class AttachmentStore:
    def __init__(self, chat_id, chat_dir):
        self.chat_id = chat_id
        self.dir = chat_dir / "attachments"
        self.manifest_path = chat_dir / "attachments-manifest.yaml"
        self.manifest = {}
        self.dirty = False
        self.downloads = 0        # fresh downloads this run (manifest hits excluded)
        self.links_by_msg = {}    # message id -> [markdown link lines] produced this run
        self._dir_ready = False
        if self.manifest_path.exists():
            import yaml
            self.manifest = yaml.safe_load(self.manifest_path.read_text()) or {}

    @staticmethod
    def _jenv(stdout: str) -> dict:
        i = stdout.find("{")
        if i < 0:
            return {}
        try:
            return json.loads(stdout[i:])
        except json.JSONDecodeError:
            return {}

    def _gsk_retry(self, cmd, timeout, check, what, tries=3):
        """Run a gsk command, retrying transient upstream failures. The Graph
        hosted-content endpoint intermittently returns 503 through the
        connector, so plain one-shot calls
        would fail nearly every download in a bulk run."""
        import subprocess
        last = ""
        for attempt in range(tries):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            res = check(self._jenv(r.stdout), r.stdout)
            if res is not None:
                return res
            last = (r.stdout or r.stderr or "").strip()
            if attempt + 1 < tries and re.search(
                    r"HTTP (?:429|5\d\d)|Service Unavailable|Too Many Requests", last, re.I):
                time.sleep(5 * (3 ** attempt))
                continue
            break
        raise ArchiveError(f"{what}: attachment transfer failed")

    def _ensure_relay_dir(self):
        if not self._dir_ready:
            import subprocess
            subprocess.run([GSK_COMMAND, "aidrive", "mkdir", "-p", RELAY_DIR],
                           capture_output=True, text=True, timeout=60)
            self._dir_ready = True

    def _download(self, key, message_id, hosted_id=None, att_id=None, name=""):
        if key in self.manifest:
            filename = self.manifest[key].get("file")
            if filename and (self.dir / safe_component(filename)).is_file():
                return filename
        if DRY_RUN:
            return None
        import subprocess, urllib.request, hashlib as _h

        def _chk_envelope(env, raw):
            d = env.get("data") or {}
            return d if (d.get("success") and d.get("file_wrapper_url")) else None

        def _chk_relay(env, raw):
            return True if "Download Complete" in raw else None

        def _chk_url(env, raw):
            m = re.search(r"https?://[^\s\"\\']+", json.dumps(env))
            return m.group(0).rstrip('\\"') if m else None

        cmd = [GSK_COMMAND, "microsoft_teams", "download_attachment",
               "--chat_id", self.chat_id, "--message_id", str(message_id)]
        cmd += ["--hosted_content_id", hosted_id] if hosted_id else ["--attachment_id", att_id]
        data = self._gsk_retry(cmd, 180, _chk_envelope, "gsk download")
        # The wrapper URL is not directly fetchable (403 for any client-side
        # auth). Relay through AI Drive: the platform backend CAN read its own
        # wrapper URLs. download_file -> get_readable_url -> plain GET.
        # Relay name is per-process: AI Drive download_file does not overwrite,
        # so a leftover file from a crashed run would get a "(1)" copy while
        # get_readable_url silently reads the stale original.
        self._ensure_relay_dir()
        tmp_name = _h.sha1(key.encode()).hexdigest()[:16] + f"-{os.getpid()}.bin"
        try:
            self._gsk_retry([GSK_COMMAND, "aidrive", "download_file",
                             "--file_url", data["file_wrapper_url"],
                             "--target_folder", RELAY_DIR,
                             "--file_name", tmp_name],
                            180, _chk_relay, "aidrive relay")
            rurl = self._gsk_retry([GSK_COMMAND, "aidrive", "get_readable_url",
                                    "--path", f"{RELAY_DIR}/{tmp_name}"],
                                   60, _chk_url, "readable url")
            _req = urllib.request.Request(rurl,
                                          headers={"User-Agent": "curl/8.5.0"})  # edge WAF blocks Python-urllib UA
            blob = urllib.request.urlopen(_req, timeout=120).read()
        finally:
            subprocess.run([GSK_COMMAND, "aidrive", "rm", "-p", f"{RELAY_DIR}/{tmp_name}"],
                           capture_output=True, text=True, timeout=60)
        sha = _h.sha1(blob).hexdigest()
        suffix = Path(name).suffix
        ext = _EXT.get(data.get("content_type"), suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) else ".bin")
        fname = sha + ext
        self.dir.mkdir(parents=True, exist_ok=True)
        out = self.dir / fname
        if not out.exists():
            out.write_bytes(blob)
        self.manifest[key] = {
            "file": fname, "name": name or data.get("file_name", ""),
            "content_type": data.get("content_type", ""), "size": len(blob),
            "message_id": str(message_id),
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.dirty = True
        self.downloads += 1
        return fname

    def inline_images(self, message_id, html):
        links = []
        for _mid, hid in _HOSTED_RE.findall(html or ""):
            key = f"{message_id}:h:{hid[:24]}"
            try:
                fname = self._download(key, message_id, hosted_id=hid)
                if fname:
                    links.append(f"![inline image](attachments/{fname})")
            except Exception as e:
                raise ArchiveError("inline attachment download failed") from e
        if links:
            self.links_by_msg.setdefault(str(message_id), []).extend(links)
        return links

    def file_attachment(self, message_id, a):
        if (a.get("contentType") or "") != "reference" or not a.get("contentUrl"):
            return None
        name = a.get("name") or "attachment"
        key = f"{message_id}:a:{a.get('id')}"
        try:
            fname = self._download(key, message_id, att_id=a.get("id"), name=name)
            if not fname:
                return None
            line = f"*[attachment: [{name}](attachments/{fname})]*"
            self.links_by_msg.setdefault(str(message_id), []).append(line)
            return line
        except Exception as e:
            raise ArchiveError("file attachment download failed") from e

    def save(self):
        if self.dirty and not DRY_RUN:
            import yaml
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(
                yaml.safe_dump(self.manifest, allow_unicode=True, sort_keys=True))


def gsk_available():
    import shutil as _sh
    return _sh.which(GSK_COMMAND) is not None


def normalize_ts(raw: str) -> str:
    """Any ISO-ish timestamp → 'YYYY-MM-DDTHH:MM:SSZ' (UTC). '' when unparseable —
    junk must never flow into watermark comparisons or month filenames."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        warn(f"unparseable timestamp {raw!r}; message goes to the undated bucket")
        return ""


def html_to_markdown(html_body: str) -> str:
    import html2text
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0
    md = h.handle(html_body or "")
    return re.sub(r"\n{4,}", "\n\n\n", md).strip()


def slugify(text: str, max_len=60) -> str:
    text = re.sub(r'[<>:"/\\|?*#\[\]]', "", text)
    text = re.sub(r"\s+", "-", text.strip()).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


# ---------------------------------------------------------------------------
# Config + selection
# ---------------------------------------------------------------------------

def safe_component(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or "/" in value or "\\" in value or any(ord(c) < 32 for c in value)):
        raise ArchiveError("archive directory names must be single path components")
    return value


def validate_selection(cfg: dict, label: str) -> dict:
    mode = cfg.get("mode", "whitelist")
    if mode not in ("whitelist", "blacklist"):
        raise ArchiveError("mode must be whitelist or blacklist")
    cfg["mode"] = mode
    entries = cfg.get("chats", [])
    if not isinstance(entries, list):
        raise ArchiveError("chats must be a list")
    for entry in entries:
        match = entry.get("match") if isinstance(entry, dict) else entry
        if not isinstance(match, str) or not match.strip():
            raise ArchiveError("every chat entry requires a nonempty match")
        if isinstance(entry, dict) and "alias" in entry:
            safe_component(entry["alias"])
    return cfg


def configure(config_file, *, base_dir=None, output_dir=None, state_file=None,
              registry_file=None, token_cache=None, client_id=None, backend=None,
              dry_run=False):
    """Load caller settings. Performs no writes and requires no sibling package."""
    import yaml
    global CONFIG_FILE, OUTPUT_DIR, STATE_FILE, REGISTRY_FILE, GRAPH_CFG, BACKEND
    global ATTACHMENTS_ENABLED, DRY_RUN, GSK_COMMAND, RELAY_DIR
    CONFIG_FILE = Path(config_file).expanduser().resolve()
    try:
        document = yaml.safe_load(CONFIG_FILE.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ArchiveError("cannot read archive configuration") from exc
    if not isinstance(document, dict) or not isinstance(document.get("teams"), dict):
        raise ArchiveError("configuration requires a teams mapping")
    cfg = validate_selection(dict(document["teams"]), "teams")
    base = Path(base_dir).expanduser().resolve() if base_dir else CONFIG_FILE.parent

    def path(value, label, required=True):
        if value is None and not required:
            return None
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            raise ArchiveError(f"{label} is required")
        p = Path(value).expanduser()
        return (p if p.is_absolute() else base / p).resolve()

    OUTPUT_DIR = path(output_dir or cfg.get("output_dir"), "output_dir")
    STATE_FILE = path(state_file or cfg.get("state_file"), "state_file")
    REGISTRY_FILE = path(registry_file or cfg.get("registry_file"), "registry_file", False)
    graph = cfg.get("graph", {})
    if not isinstance(graph, dict):
        raise ArchiveError("graph must be a mapping")
    GRAPH_CFG = dict(graph)
    if token_cache or graph.get("token_cache"):
        GRAPH_CFG["token_cache"] = str(path(token_cache or graph.get("token_cache"), "token_cache"))
    if client_id:
        GRAPH_CFG["client_id"] = client_id
    BACKEND = backend or cfg.get("backend", "graph")
    if BACKEND not in ("graph", "gsk"):
        raise ArchiveError("backend must be graph or gsk")
    if BACKEND == "graph" and not (GRAPH_CFG.get("client_id") and GRAPH_CFG.get("token_cache")):
        raise ArchiveError("graph.client_id and graph.token_cache are required")
    DRY_RUN = dry_run
    GSK_COMMAND = cfg.get("gsk_command", "gsk")
    RELAY_DIR = cfg.get("attachment_relay_dir", "/teams-archive")
    ATTACHMENTS_ENABLED = bool(cfg.get("attachments", False))
    for key in ("bootstrap_days", "max_pages_per_chat"):
        if key in cfg and (not isinstance(cfg[key], int) or cfg[key] <= 0):
            raise ArchiveError(f"{key} must be a positive integer")
    return cfg


def is_one_on_one(chat: dict) -> bool:
    # "type" is the key the local teams-send registry rows use
    return str(chat.get("chatType") or chat.get("chat_type") or chat.get("type") or "").lower() in (
        "oneonone", "one_on_one", "dm")


def select_chats(chats: list[dict], cfg: dict) -> list[tuple[dict, str, dict]]:
    """Apply whitelist/blacklist. Returns [(chat, slug), ...]."""
    entries = cfg.get("chats") or []
    mode = cfg["mode"]
    selected = []
    seen_ids = set()
    for chat in chats:
        cid = chat_id_of(chat)
        if cid and cid in seen_ids:
            continue  # Graph pagination sometimes lists a chat twice
        seen_ids.add(cid)
        full = chat_match_text(chat).lower()
        # real topic ONLY — chat_name() falls back to member names for topicless
        # group chats, which let person entries match every ad-hoc group the
        # person is in (exactly what the whitelist semantics promise not to do)
        topic_only = str(chat.get("topic") or chat.get("name") or chat.get("subject") or "").lower()
        hit = None
        for e in entries:
            e = e if isinstance(e, dict) else {"match": str(e)}
            m = (e.get("match") or "").lower()
            if not m:
                continue
            # Whitelist: member/email matching applies to 1:1 chats only, so a
            # person entry doesn't silently pull in every group they belong to;
            # groups match by topic unless the entry sets include_groups: true.
            # Blacklist: broad matching is the safe direction — keep full text.
            text = full if (mode == "blacklist" or is_one_on_one(chat) or e.get("include_groups")) \
                else topic_only
            if m == str(cid).lower() or m in text:
                hit = e
                break
        if (mode == "whitelist") != (hit is not None):
            continue
        if mode == "whitelist":
            ctype = chat.get("chatType") or chat.get("chat_type") or "?"
            log(f"  selected {chat_name(chat)!r} [{ctype}] via match {hit.get('match')!r}")
        alias = (hit or {}).get("alias") if mode == "whitelist" else None
        selected.append((chat, safe_component(alias or slugify(chat_name(chat))), hit or {}))
    return selected


# ---------------------------------------------------------------------------
# Markdown month files
# ---------------------------------------------------------------------------

ID_RE = re.compile(r"<!-- id: (\S+) -->")
# 'YYYY-MM-DD HH:MM:SS' as written by the message headings below
TS_HEAD_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — ", re.M)
MONTH_FILE_RE = re.compile(r"^\d{4}-\d{2}\.md$")


def month_of(ts_iso: str) -> str:
    return ts_iso[:7] if len(ts_iso) >= 7 else "undated"


def yaml_str(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def newest_ts_on_disk(chat_dir: Path) -> str | None:
    """Newest message timestamp actually mirrored on disk — the durable truth
    against which the state watermark is clamped (a publisher tick's commit can
    be discarded by a rebase/push failure after the state was already saved)."""
    if not chat_dir.exists():
        return None
    for path in sorted((p for p in chat_dir.glob("*.md") if MONTH_FILE_RE.match(p.name)),
                       reverse=True):
        tss = TS_HEAD_RE.findall(path.read_text())
        if tss:
            return max(tss).replace(" ", "T") + "Z"
    return None


def append_messages(chat_dir: Path, front: dict, heading: str, messages: list[dict]) -> int:
    """Append messages (dedup by id marker) into per-month files. Returns #written."""
    written = 0
    by_month: dict[str, list[dict]] = {}
    for m in sorted(messages, key=lambda x: x["ts"]):
        by_month.setdefault(month_of(m["ts"]), []).append(m)

    for month, msgs in sorted(by_month.items()):
        path = chat_dir / f"{month}.md"
        if path.exists():
            content = path.read_text()
            seen = set(ID_RE.findall(content))
        else:
            if not DRY_RUN:
                chat_dir.mkdir(parents=True, exist_ok=True)
            # month-first H1: a chat literally named 'Summary' must not trip the
            # heading classification checks (prefix-anchored regex)
            fm_lines = ["---"] + [f"{k}: {yaml_str(v)}" for k, v in front.items()] \
                + [f'month: "{month}"', "times: UTC", "---", "", f"# {month} — {heading}", ""]
            content = "\n".join(fm_lines)
            seen = set()
        chunks = []
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            ts_disp = m["ts"].replace("T", " ").replace("Z", "") if m["ts"] else "(no time)"
            chunks.append(f"\n### {ts_disp} — {m['sender']}\n<!-- id: {m['id']} -->\n\n{m['body']}\n")
            written += 1
        if chunks and not DRY_RUN:
            path.write_text(content.rstrip("\n") + "\n" + "".join(chunks))
    return written


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    state = None
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            warn("state file corrupt; starting fresh")
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    if not isinstance(state.get("chats"), dict):
        state["chats"] = {}
    state["chats"] = {k: v for k, v in state["chats"].items() if isinstance(v, dict)}
    return state


def save_state(state: dict):
    if DRY_RUN:
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False))
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# --peek chat resolution — registry-first, deterministic ranking
# ---------------------------------------------------------------------------

PEEK_ID_PREFIX = "19:"   # every Teams chat id starts with this ("19:...@thread.v2")

# Rank names indexed by rank_chat_match's return value (lower = better).
PEEK_RANK_NAMES = ("exact topic", "1:1 member name",
                   "group topic substring", "group member substring")


def registry_path() -> Path:
    """Local chat registry maintained by a caller-owned chat directory tool `chats --refresh`."""
    return REGISTRY_FILE


def load_registry_rows() -> list[dict]:
    """Registry chat rows; [] when the file is missing or unreadable (a miss)."""
    path = registry_path()
    if path is None:
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def peek_match_fields(chat: dict) -> tuple[str, list[str]]:
    """(topic, member_names) tolerant of registry rows and live backend chats."""
    topic = str(chat.get("topic") or chat.get("name") or chat.get("subject") or "").strip()
    names = []
    for m in chat.get("members") or chat.get("participants") or []:
        if isinstance(m, dict):
            n = m.get("displayName") or m.get("display_name") or m.get("name")
            if n:
                names.append(str(n))
        elif isinstance(m, str):
            names.append(m)
    return topic, names


def rank_chat_match(needle: str, chat: dict) -> int | None:
    """Rank of `chat` for `needle` (index into PEEK_RANK_NAMES, lower = better);
    None = no match. Exact topic > 1:1 member name > group topic substring >
    group member substring — never first-listed-wins. A 1:1 chat resolves by
    member name (or exact topic) only, so a person's name cannot be shadowed
    by topic fragments of chats they merely appear in."""
    n = needle.strip().lower()
    if not n:
        return None
    topic, names = peek_match_fields(chat)
    t = topic.lower()
    if t and t == n:
        return 0
    if is_one_on_one(chat):
        return 1 if any(n in nm.lower() for nm in names) else None
    if t and n in t:
        return 2
    if any(n in nm.lower() for nm in names):
        return 3
    return None


def select_peek_chat(needle: str, chats: list[dict]) -> tuple[dict | None, int | None, list[dict]]:
    """(winner, rank, tied). A winner requires a UNIQUE chat at the best rank;
    ties return (None, rank, candidates) so the caller prints them instead of
    silently picking the first."""
    best, hits = None, []
    for c in chats:
        r = rank_chat_match(needle, c)
        if r is None:
            continue
        if best is None or r < best:
            best, hits = r, [c]
        elif r == best:
            hits.append(c)
    if best is None:
        return None, None, []
    if len(hits) > 1:
        return None, best, hits
    return hits[0], best, []


def write_registry(chats: list[dict], cfg: dict) -> None:
    """Refresh the registry from a live enumeration, in the exact row schema
    a caller-owned chat directory tool writes. last_message_at is preserved from the old rows (this
    enumeration fetches no message previews); mirrored replicates teams-send's
    flag semantics (any config match substring in topic+members)."""
    old = {r.get("id"): r for r in load_registry_rows()}
    pats = []
    for e in cfg.get("chats") or []:
        m = (e.get("match") if isinstance(e, dict) else str(e)) or ""
        if m:
            pats.append(str(m).lower())
    rows = []
    for c in chats:
        cid = chat_id_of(c)
        if not cid:
            continue
        topic, names = peek_match_fields(c)
        hay = (topic + " " + " ".join(names)).lower()
        rows.append({
            "id": cid,
            "type": c.get("chatType") or c.get("chat_type") or c.get("type"),
            "topic": topic,
            "members": names,
            "label": topic or " & ".join(n for n in names if n)[:80],
            "mirrored": any(p in hay for p in pats),
            "last_message_at": (old.get(cid) or {}).get("last_message_at", ""),
        })
    path = registry_path()
    if path is None or DRY_RUN:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(
        {"refreshed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "chats": rows}, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def live_chat_rows(cfg: dict) -> list[dict] | None:
    """The slow path: ONE live chat enumeration, with its cost printed. On the
    graph backend the result also refreshes the local registry (chat metadata
    cache shared with a caller-owned chat directory tool — never message/sync state), so the next
    --peek resolves without enumerating."""
    t0 = time.monotonic()
    chats = list_chats()
    if chats is None:
        if BACKEND == "gsk":
            warn("Teams connector not configured (the connector settings); cannot peek.")
        else:
            warn("graph backend has no token — run --backend graph --login first; cannot peek.")
        return None
    log(f"registry miss — live chat enumeration: {LAST_FETCH_PAGES} pages, "
        f"{time.monotonic() - t0:.1f}s")
    if BACKEND == "graph":
        # gsk listings may lack member fields, which would degrade the registry
        try:
            write_registry(chats, cfg)
        except OSError as e:
            warn(f"registry refresh failed (peek continues): {e}")
    return chats


def resolve_peek_target(match: str, cfg: dict) -> tuple[str | None, str]:
    """--peek MATCH -> (chat_id, label); (None, "") = resolution failed, and the
    reason was already printed. MATCH starting with 19: is an exact chat id."""
    if match.startswith(PEEK_ID_PREFIX):
        return match, match
    winner, rank, ties = select_peek_chat(match, load_registry_rows())
    if winner is None and not ties:
        live = live_chat_rows(cfg)
        if live is None:
            return None, ""
        winner, rank, ties = select_peek_chat(match, live)
    if ties:
        warn(f"{len(ties)} chats match {match!r} equally well ({PEEK_RANK_NAMES[rank]}) — "
             f"narrow the match or pass the chat id ({PEEK_ID_PREFIX}...):")
        for c in ties[:10]:
            topic, names = peek_match_fields(c)
            log(f"  topic: {topic or '(none)'}  id: {chat_id_of(c)}  "
                f"members: {', '.join(names[:6]) or '?'}")
        if len(ties) > 10:
            log(f"  ... and {len(ties) - 10} more")
        return None, ""
    if winner is None:
        warn(f"no chat matches {match!r}")
        return None, ""
    return chat_id_of(winner), chat_name(winner)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def cmd_list_chats():
    chats = list_chats()
    if chats is None:
        raise ArchiveError("chat listing unavailable or incomplete")
    log(f"{len(chats)} chats visible:")
    for c in chats:
        ctype = c.get("chatType") or c.get("chat_type") or "?"
        log(f"  [{ctype:8s}] {chat_name(c)}")
        log(f"             id: {chat_id_of(c)}")
    log("\nAdd entries to the caller configuration → teams.chats to start mirroring.")


def cmd_peek(match: str, limit: int, cfg: dict):
    cid, label = resolve_peek_target(match, cfg)
    if cid is None:
        raise ArchiveError("chat name did not resolve to one available chat")
    log(f"# {label}  ({cid})\n")
    raw_msgs, complete = read_chat(cid, None, max_pages=(limit // PAGE_SIZE) + 1,
                                   exact_pages=True)
    msgs = [n for n in (norm_message(m) for m in raw_msgs) if n]
    if not msgs and not complete:
        # registry-first resolution skips the chat listing, so this is now the
        # first call that exercises backend auth / chat access
        raise ArchiveError("chat read unavailable or incomplete")
    for m in sorted(msgs, key=lambda x: x["ts"])[-limit:]:
        log(f"### {m['ts']} — {m['sender']}\n{m['body']}\n")


def cmd_sync(cfg: dict):
    if not cfg.get("enabled", True):
        log("teams sync disabled in config; skipping")
        return
    if not (cfg.get("chats") or cfg["mode"] == "blacklist"):
        log("teams: whitelist empty — nothing configured to sync (edit the caller configuration)")
        return

    chats = list_chats()
    if chats is None:
        raise ArchiveError("chat listing unavailable or incomplete")
    if not chats:
        log("teams: no chats returned")
        return

    selected = select_chats(chats, cfg)
    if selected and ATTACHMENTS_ENABLED and not DRY_RUN and not gsk_available():
        raise ArchiveError("attachments require the configured gsk command")
    log(f"teams: {len(chats)} chats visible, {len(selected)} selected by config")

    state = load_state()
    slug_owner = {cs["slug"]: c for c, cs in state["chats"].items() if cs.get("slug")}
    bootstrap_days = int(cfg.get("bootstrap_days", 14))
    max_pages = int(cfg.get("max_pages_per_chat", 10))
    total = 0

    for chat, slug, entry in selected:
        # a whitelist entry may deepen the first-sync window for its chat
        chat_bootstrap = int(entry.get("bootstrap_days", bootstrap_days))
        cid = chat_id_of(chat)
        if not cid:
            continue
        cstate = state["chats"].setdefault(cid, {})
        pinned = cstate.get("slug")
        if pinned:
            safe_component(pinned)
        if pinned:
            slug = pinned
        elif slug_owner.get(slug, cid) != cid:
            base = slug
            slug = f"{slug}-{re.sub(r'[^0-9A-Za-z]', '', cid)[-8:]}"
            warn(f"teams: slug collision on {base!r} (owned by another chat); using {slug!r}")
        slug_owner[slug] = cid
        cstate["slug"] = slug

        # The state can claim more than what survived on disk if a previous
        # tick's commit was discarded (publisher rebase/push failure) — clamp.
        watermark = cstate.get("watermark")
        disk_newest = newest_ts_on_disk(OUTPUT_DIR / slug)
        if watermark and (disk_newest or "") < watermark:
            if disk_newest:
                warn(f"{slug}: watermark {watermark} ahead of disk {disk_newest} "
                     "(a previous commit was discarded) — clamping")
                watermark = disk_newest
            else:
                warn(f"{slug}: watermark {watermark} but nothing on disk — re-bootstrapping")
                watermark = None
        if watermark:
            try:
                since_dt = datetime.fromisoformat(watermark.replace("Z", "+00:00")) \
                    - timedelta(minutes=OVERLAP_MIN)
            except ValueError:
                warn(f"{slug}: bad watermark {watermark!r}; resetting to bootstrap window")
                watermark = None
                since_dt = datetime.now(timezone.utc) - timedelta(days=chat_bootstrap)
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(days=chat_bootstrap)
        since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        raw_msgs, complete = read_chat(cid, since, max_pages)
        if not complete:
            raise ArchiveError("chat messages incomplete; sync state was not saved")
        att_store = AttachmentStore(cid, OUTPUT_DIR / slug) if ATTACHMENTS_ENABLED else None
        msgs = [n for n in (norm_message(m, att_store) for m in raw_msgs) if n]
        if att_store:
            att_store.save()
        if raw_msgs and not msgs:
            if any(not (m.get("deletedDateTime") or m.get("deleted_at"))
                   and (m.get("body") or m.get("text") or m.get("content")) for m in raw_msgs):
                raise ArchiveError("messages could not be normalized; sync state was not saved")
        if not msgs:
            continue
        front = {
            "platform": "teams",
            "chat_id": cid,
            "chat_name": chat_name(chat),
            "chat_type": chat.get("chatType") or chat.get("chat_type") or "",
        }
        n = append_messages(OUTPUT_DIR / slug, front, chat_name(chat), msgs)
        # advance only on a complete read (else the un-fetched remainder would
        # fall behind the watermark forever), and never move backwards (an
        # edited old message re-surfaces with an old createdDateTime)
        newest = max((m["ts"] for m in msgs if m["ts"]), default=None)
        if complete and newest:
            cstate["watermark"] = max(newest, watermark or "")
        elif not complete:
            warn(f"{slug}: incomplete fetch — watermark not advanced; will re-fetch next run")
        total += n
        if n:
            log(f"  {slug}: +{n} messages")
        time.sleep(RATE_DELAY)

    save_state(state)
    log(f"teams: done, {total} new messages")


def disk_slug_map() -> dict[str, str]:
    """chat_id -> existing on-disk slug, read from month-file frontmatter. The
    disk is the truth here: slug pins live in the caller's synchronization state,
    which an on-demand backfill run cannot see."""
    mapping = {}
    if not OUTPUT_DIR.exists():
        return mapping
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        for f in sorted(d.glob("*.md")):
            if not MONTH_FILE_RE.match(f.name):
                continue
            m = re.search(r'^chat_id: "(.+)"$', f.read_text()[:600], re.M)
            if m:
                mapping[m.group(1)] = d.name
                break
    return mapping


def patch_attachment_links(chat_dir: Path, msgs: list[dict], links_by_msg: dict) -> int:
    """Insert attachment link lines into already-mirrored message blocks
    (matched by id marker). Existing text is never altered: link lines are
    appended to the end of the block body, and only when missing, so the
    operation is idempotent. Returns #messages patched."""
    patched = 0
    by_month: dict[str, list[dict]] = {}
    for m in msgs:
        if links_by_msg.get(m["id"]):
            by_month.setdefault(month_of(m["ts"]), []).append(m)
    for month, mlist in sorted(by_month.items()):
        path = chat_dir / f"{month}.md"
        if path is None or not path.exists():
            continue
        content = path.read_text()
        changed = False
        for m in mlist:
            marker = f"<!-- id: {m['id']} -->"
            start = content.find(marker)
            if start < 0:
                continue  # not in this file; append_messages owns new blocks
            nxt = TS_HEAD_RE.search(content, start + len(marker))
            body_end = nxt.start() if nxt else len(content)
            block = content[start:body_end]
            missing = [ln for ln in links_by_msg[m["id"]] if ln not in block]
            if not missing:
                continue
            tail = content[body_end:]
            content = (content[:body_end].rstrip("\n") + "\n\n"
                       + "\n".join(missing) + ("\n\n" if tail else "\n") + tail)
            changed = True
            patched += 1
        if changed and not DRY_RUN:
            path.write_text(content)
    return patched


def cmd_backfill_attachments(cfg: dict, days: int):
    """Re-fetch the last N days of every selected chat and download attachments:
    messages already on disk gain link lines in place; messages missing from
    disk are appended through the normal dedup path. Watermark state is neither
    read nor written, so this is safe to run from any host at any time."""
    if not ATTACHMENTS_ENABLED or (not DRY_RUN and not gsk_available()):
        raise ArchiveError("backfill requires attachments enabled and the configured gsk command")
    chats = list_chats()
    if chats is None:
        warn("no chat listing available (backend auth missing?); cannot backfill")
        sys.exit(1)
    selected = select_chats(chats, cfg)
    on_disk = disk_slug_map()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_pages = max(int(cfg.get("max_pages_per_chat", 10)), 40)
    tot_new = tot_patched = tot_dl = 0
    for chat, slug, _entry in selected:
        cid = chat_id_of(chat)
        if not cid:
            continue
        slug = on_disk.get(cid, slug)
        chat_dir = OUTPUT_DIR / slug
        if not chat_dir.exists():
            log(f"  {slug}: not mirrored on disk yet (regular sync owns the first bootstrap); skipping")
            continue
        raw_msgs, complete = read_chat(cid, since, max_pages)
        if not complete:
            raise ArchiveError("chat messages incomplete; sync state was not saved")
        att = AttachmentStore(cid, chat_dir)
        msgs = [n for n in (norm_message(m, att) for m in raw_msgs) if n]
        att.save()
        if not msgs:
            continue
        front = {
            "platform": "teams",
            "chat_id": cid,
            "chat_name": chat_name(chat),
            "chat_type": chat.get("chatType") or chat.get("chat_type") or "",
        }
        n_new = append_messages(chat_dir, front, chat_name(chat), msgs)
        n_patched = patch_attachment_links(chat_dir, msgs, att.links_by_msg)
        if not complete:
            warn(f"{slug}: window truncated by the {max_pages}-page cap; "
                 "oldest part of the window not covered")
        log(f"  {slug}: +{n_new} appended, {n_patched} patched, {att.downloads} downloaded")
        tot_new += n_new
        tot_patched += n_patched
        tot_dl += att.downloads
        time.sleep(RATE_DELAY)
    log(f"teams backfill: {tot_new} appended, {tot_patched} patched, {tot_dl} attachments downloaded")


def main(argv=None):
    global DUMP_RAW
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--state-file")
    parser.add_argument("--registry-file")
    parser.add_argument("--token-cache")
    parser.add_argument("--client-id")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-chats", action="store_true")
    modes.add_argument("--peek", metavar="MATCH")
    modes.add_argument("--login", action="store_true")
    modes.add_argument("--backfill-attachments", type=int, metavar="DAYS")
    parser.add_argument("--peek-limit", type=int, default=30)
    parser.add_argument("--backend", choices=["gsk", "graph"])
    parser.add_argument("--dump-raw", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="read and plan without archive, state, cache, or attachment writes")
    args = parser.parse_args(argv)
    if args.peek_limit <= 0 or (args.backfill_attachments is not None and args.backfill_attachments <= 0):
        parser.error("limits must be positive")
    if args.dry_run and args.login:
        parser.error("--login cannot be combined with --dry-run")
    try:
        cfg = configure(args.config, base_dir=args.base_dir, output_dir=args.output_dir,
                        state_file=args.state_file, registry_file=args.registry_file,
                        token_cache=args.token_cache, client_id=args.client_id,
                        backend=args.backend, dry_run=args.dry_run)
        DUMP_RAW = args.dump_raw
        if args.login:
            if BACKEND != "graph":
                raise ArchiveError("--login requires the Graph backend")
            if not graph_token(interactive=True):
                raise ArchiveError("login failed")
            log("login OK")
        elif args.list_chats:
            cmd_list_chats()
        elif args.peek:
            cmd_peek(args.peek, args.peek_limit, cfg)
        elif args.backfill_attachments:
            cmd_backfill_attachments(cfg, args.backfill_attachments)
        else:
            cmd_sync(cfg)
        return 0
    except (ArchiveError, OSError, ValueError) as exc:
        # Avoid echoing response bodies, token cache contents, or signed URLs.
        warn(str(exc) if isinstance(exc, ArchiveError) else "archive operation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
