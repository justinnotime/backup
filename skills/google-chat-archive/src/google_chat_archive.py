#!/usr/bin/env python3
"""Archive selected Google Chat spaces as monthly Markdown; no message writes."""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_FILE = None
REPO_DIR = OUTPUT_DIR = STATE_FILE = TOKEN_FILE = None
CHAT_API = "https://chat.googleapis.com/v1"
PEOPLE_API = "https://people.googleapis.com/v1"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
RATE_DELAY = 0.5          # Chat API per-user read quotas are generous; stay polite
PAGE_SIZE = 100
MAX_PAGES_DEFAULT = 20
OVERLAP_MIN = 15          # re-fetch overlap before the watermark; dedupe by message id


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"  [WARN] {msg}", file=sys.stderr, flush=True)


class AuthProblem(Exception):
    """Authentication failed; the caller must not publish staged state."""


class ScopeProblem(AuthProblem):
    """Scope / API-enablement problem — needs the one-time GCP setup."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthProblem("authenticated redirects are not supported")


def request(req):
    return urllib.request.build_opener(NoRedirect).open(req, timeout=60)


# ---------------------------------------------------------------------------
# OAuth token (authorized-user JSON supplied by the caller)
# ---------------------------------------------------------------------------

_token_cache = {"token": None, "exp": 0.0}


def get_access_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    try:
        info = json.loads(TOKEN_FILE.read_text())
        data = urllib.parse.urlencode({
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "refresh_token": info["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        with request(urllib.request.Request(TOKEN_ENDPOINT, data=data)) as resp:
            tok = json.loads(resp.read())
        _token_cache.update(token=tok["access_token"],
                            exp=now + float(tok.get("expires_in", 3600)))
        return _token_cache["token"]
    except Exception:
        raise AuthProblem("OAuth token refresh failed; check the configured authorized-user file") from None


# ---------------------------------------------------------------------------
# API plumbing (urllib only — no extra deps)
# ---------------------------------------------------------------------------

def api_get(url: str, **params) -> dict | None:
    """GET a Google API object; only an absent optional People record returns None.
    Failed required reads raise instead of becoming a successful empty page."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url}?{qs}" if qs else url
    for attempt in range(4):
        token = get_access_token()
        if token is None:
            raise AuthProblem("configured OAuth credential is missing")
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {token}"})
        try:
            with request(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Google API returned an invalid object")
                return payload
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 429 or e.code >= 500:
                retry_after = e.headers.get("Retry-After", "")
                if attempt < 3:
                    time.sleep(min(int(retry_after), 60) if retry_after.isdigit() else 2 ** attempt)
                continue
            if e.code in (401, 403) and (
                    "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body
                    or "insufficient authentication scopes" in body.lower()
                    or "SERVICE_DISABLED" in body
                    or "it is disabled" in body):
                raise ScopeProblem(f"Google API returned HTTP {e.code}; check OAuth scopes and API enablement") from None
            if e.code == 404 and url.startswith(PEOPLE_API + "/"):
                return None
            raise RuntimeError(f"Google API returned HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError("Google API read failed after bounded retries")


def paginate(url: str, list_key: str, max_pages: int, **params) -> tuple[list, bool]:
    """Token pagination. Returns (items, complete) — complete=False on a
    mid-run failure or cap hit. Items are still a contiguous prefix of the
    result set (pages arrive in order), which watermark advancement relies on."""
    items, page_token, pages = [], None, 0
    while pages < max_pages:
        data = api_get(url, pageToken=page_token, pageSize=PAGE_SIZE, **params)
        page = data.get(list_key) or []
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ValueError("Google API returned an invalid page")
        items.extend(page)
        page_token = data.get("nextPageToken") or None
        pages += 1
        if not page_token:
            return items, True
        time.sleep(RATE_DELAY)
    warn(f"{url} hit the {max_pages}-page safety cap with more data pending")
    return items, False


# ---------------------------------------------------------------------------
# Timestamps (Chat createTime is RFC-3339 UTC, variable fractional digits)
# ---------------------------------------------------------------------------

FRACTION_RE = re.compile(r"\.(\d+)")


def parse_ts(ts: str) -> datetime:
    ts = FRACTION_RE.sub(lambda m: "." + m.group(1)[:6], ts.strip())
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def ts_display(ts: str) -> str:
    return parse_ts(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def month_of(ts: str) -> str:
    return parse_ts(ts).astimezone(timezone.utc).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Sender-name resolution: state cache → membership displayName → People API
# ---------------------------------------------------------------------------

class Users(dict):
    """'users/<id>' -> display name. Successes only are cached persistently
    (state file); failures get a per-RUN negative cache (retry next run — failure recovery). Bots are never sent to the People API (Chat apps have no
    People records — every lookup would be a guaranteed 404)."""

    def __init__(self, cached: dict):
        super().__init__({k: v for k, v in (cached or {}).items()
                          if isinstance(k, str) and isinstance(v, str)})
        self.people_unavailable = False
        self._failed = set()

    def resolve(self, uid: str | None, member_type: str | None = None) -> str | None:
        if not uid:
            return None
        if uid in self:
            return self[uid]
        if (member_type == "BOT" or self.people_unavailable
                or uid in self._failed or not uid.startswith("users/")):
            return uid
        try:
            data = api_get(f"{PEOPLE_API}/people/{uid.split('/', 1)[1]}",
                           personFields="names")
        except ScopeProblem:
            # People API absent from the token is fine — degrade to raw ids.
            # (Token-refresh AuthProblem intentionally propagates and aborts.)
            self.people_unavailable = True
            warn("People API unavailable (scope/API not granted); "
                 "senders render as users/<id>")
            return uid
        names = (data or {}).get("names") or []
        name = next((n.get("displayName") for n in names if n.get("displayName")), None)
        if name:
            self[uid] = name
            time.sleep(0.2)
            return name
        self._failed.add(uid)  # in-run only; persistent cache keeps successes
        return uid


# ---------------------------------------------------------------------------
# Space inventory
# ---------------------------------------------------------------------------

def space_members(space_name: str, users: Users, max_pages: int) -> list[dict]:
    """Human-oriented member list: [{'uid', 'name', 'type'}] (bots included)."""
    ms, complete = paginate(f"{CHAT_API}/{space_name}/members", "memberships", max_pages)
    if not complete:
        raise RuntimeError("space membership listing is incomplete")
    out = []
    for m in ms:
        member = m.get("member") or {}
        uid = member.get("name") or ""
        disp = member.get("displayName")
        mtype = member.get("type") or "HUMAN"
        if disp and uid:
            users.setdefault(uid, disp)
        out.append({"uid": uid, "name": users.resolve(uid, mtype) or uid,
                    "type": mtype})
    return out


def find_self_user(space_name: str, self_email: str, state: dict) -> str | None:
    """Resolve the configured account's users/<id> once via the documented email alias
    (spaces.members.get accepts the user's email as the member id)."""
    if state.get("self_user"):
        return state["self_user"]
    data = api_get(f"{CHAT_API}/{space_name}/members/{urllib.parse.quote(self_email)}")
    uid = ((data or {}).get("member") or {}).get("name")
    if uid:
        state["self_user"] = uid
    return uid


def list_spaces(cfg: dict, users: Users, state: dict) -> list[dict]:
    spaces, complete = paginate(f"{CHAT_API}/spaces", "spaces", int(cfg["max_pages"]))
    if not complete:
        raise RuntimeError("space listing is incomplete; increase max_pages")
    self_email = (cfg.get("self_email") or "").strip()
    for s in spaces:
        stype = s.get("spaceType") or s.get("type") or ""
        s["_type"] = {"SPACE": "space", "GROUP_CHAT": "group",
                      "DIRECT_MESSAGE": "dm"}.get(stype, stype.lower() or "space")
        name = (s.get("displayName") or "").strip()
        if not name:
            # unnamed DM / group chat: derive from the other members ONCE and
            # cache the result in state (one members.list per DM per run adds
            # up under mirror-everything)
            cached = (state["spaces"].get(s["name"]) or {}).get("display")
            if isinstance(cached, str) and cached:
                name = cached
            else:
                members = space_members(s["name"], users, int(cfg["max_pages"]))
                self_uid = find_self_user(s["name"], self_email, state) if self_email else None
                humans = [m["name"] for m in members
                          if m["type"] != "BOT" and m["uid"] != self_uid]
                bots = [m["name"] for m in members if m["type"] == "BOT"]
                picked = sorted(humans)[:3] or sorted(bots)[:3]
                name = ", ".join(picked) or s["name"].split("/")[-1]
                self_failed = bool(self_email) and not self_uid
                if self_failed or any(p.startswith("users/") for p in picked):
                    name = s["name"].split("/")[-1]
                else:
                    state["spaces"].setdefault(s["name"], {})["display"] = name
                time.sleep(RATE_DELAY)
        s["_name"] = name
    return spaces


# ---------------------------------------------------------------------------
# Selection + config (selection and private runtime settings)
# ---------------------------------------------------------------------------

def path_value(value, base):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("configuration requires a nonempty path")
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else base / path


def safe_slug(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or "/" in value or "\\" in value or "\x00" in value):
        raise ValueError("archive alias must be a single directory name")
    return value


def load_config(args) -> dict:
    import yaml
    global CONFIG_FILE, REPO_DIR, OUTPUT_DIR, STATE_FILE, TOKEN_FILE
    CONFIG_FILE = path_value(args.config, Path.cwd()).resolve()
    try:
        document = yaml.safe_load(CONFIG_FILE.read_text())
    except yaml.YAMLError:
        raise ValueError("invalid private YAML configuration") from None
    if not isinstance(document, dict) or not isinstance(document.get("googlechat"), dict):
        raise ValueError("configuration requires a googlechat mapping")
    cfg = dict(document["googlechat"])
    allowed = {"enabled", "mode", "chats", "bootstrap_days", "max_pages", "self_email",
               "base_dir", "output_dir", "state_file", "token_file"}
    if cfg.keys() - allowed:
        raise ValueError("googlechat configuration contains unknown fields")
    mode = cfg.setdefault("mode", "whitelist")
    if mode not in ("whitelist", "blacklist"):
        raise ValueError("mode must be whitelist or blacklist")
    entries = cfg.setdefault("chats", [])
    if not isinstance(entries, list):
        raise ValueError("chats must be a list")
    for entry in entries:
        match = entry.get("match") if isinstance(entry, dict) else entry
        if not isinstance(match, str) or not match.strip():
            raise ValueError("every chat selection requires a nonempty match")
        if isinstance(entry, dict) and "alias" in entry:
            safe_slug(entry["alias"])
    for field, default in (("bootstrap_days", 14), ("max_pages", MAX_PAGES_DEFAULT)):
        cfg.setdefault(field, default)
        if type(cfg[field]) is not int or cfg[field] < 1:
            raise ValueError(f"{field} must be a positive integer")
    if type(cfg.setdefault("enabled", True)) is not bool:
        raise ValueError("enabled must be a boolean")
    if not isinstance(cfg.setdefault("self_email", ""), str):
        raise ValueError("self_email must be a string")
    configured_base = path_value(cfg.get("base_dir", "."), CONFIG_FILE.parent).resolve()
    REPO_DIR = path_value(args.base_dir, Path.cwd()).resolve() if args.base_dir else configured_base
    OUTPUT_DIR = path_value(args.output_dir or cfg.get("output_dir", "archive/google-chat"), REPO_DIR).resolve()
    STATE_FILE = path_value(args.state_file or cfg.get("state_file", str(OUTPUT_DIR / ".sync_state.json")), REPO_DIR).resolve()
    TOKEN_FILE = path_value(args.token_file or cfg.get("token_file"), configured_base).resolve()
    if STATE_FILE in (TOKEN_FILE, CONFIG_FILE):
        raise ValueError("state_file must not replace credentials or configuration")
    _token_cache.update(token=None, exp=0.0)
    return cfg


def match_entry(name: str, sid: str, entries: list) -> dict | None:
    text = f"{name} | {sid}".lower()
    for e in entries:
        e = e if isinstance(e, dict) else {"match": str(e)}
        m = (e.get("match") or "").lower()
        if m and m in text:
            return e
    return None


def select_spaces(spaces: list[dict], cfg: dict) -> list[tuple[dict, str]]:
    mode = cfg["mode"]
    selected = []
    for s in spaces:
        hit = match_entry(s["_name"], s["name"], cfg.get("chats") or [])
        if (mode == "whitelist") != (hit is not None):
            continue
        if mode == "whitelist":
            log(f"  selected {s['_name']!r} [{s['_type']}] via match {hit.get('match')!r}")
        alias = (hit or {}).get("alias") if mode == "whitelist" else None
        selected.append((s, alias or slugify(s["_name"])))
    return selected


def slugify(text: str, max_len=60) -> str:
    text = re.sub(r'[<>:"/\\|?*#\[\]@]', "", str(text))
    text = re.sub(r"\s+", "-", text.strip()).strip("-")
    text = text[:max_len].rstrip("-")
    if not text.strip("."):  # '.' / '..' would escape the space directory
        return "untitled"
    return text


# ---------------------------------------------------------------------------
# Rendering (monthly Markdown with stable message IDs)
# ---------------------------------------------------------------------------

# One entry = heading line + id marker on the NEXT line, exactly as written
# below. Anchoring both together keeps verbatim message bodies containing a
# stray '### ...' line or '<!-- id: ... -->' from poisoning dedupe/clamping.
ENTRY_RE = re.compile(
    r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — .*\n<!-- id: (\S+) -->", re.M)
MONTH_FILE_RE = re.compile(r"^\d{4}-\d{2}\.md$")
SPACE_ID_RE = re.compile(r'^space_id: "([^"]+)"', re.M)


def yaml_str(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def norm_message(m: dict, users: Users) -> dict | None:
    ts = m.get("createTime")
    if not ts or "deletionMetadata" in m:
        return None
    text = (m.get("text") or "").strip()
    for att in m.get("attachment") or []:
        label = att.get("contentName") or att.get("name") or "attachment"
        text = (text + "\n\n" if text else "") + f"*[file: {label}]*"
    if not text and (m.get("cardsV2") or m.get("cards") or m.get("attachedGifs")):
        # card/GIF-only app messages must still land on disk, or their space's
        # watermark could never advance (it may only hold on-disk timestamps)
        text = "*[card message]*"
    if not text:
        return None
    sender = m.get("sender") or {}
    uid = sender.get("name")
    if uid and sender.get("displayName"):
        users.setdefault(uid, sender["displayName"])
    name = users.resolve(uid, sender.get("type")) or "(unknown)"
    if sender.get("type") == "BOT" and not name.startswith("users/"):
        name = f"{name} (bot)"
    mid = (m.get("name") or "").split("/messages/")[-1] or None
    if not mid:
        return None
    return {"id": mid, "ts": ts, "sender": name, "body": text}


def append_messages(chat_dir: Path, front: dict, heading: str, messages: list[dict]) -> int:
    written = 0
    by_month: dict[str, list[dict]] = {}
    for m in sorted(messages, key=lambda x: parse_ts(x["ts"])):
        by_month.setdefault(month_of(m["ts"]), []).append(m)
    for month, msgs in sorted(by_month.items()):
        path = chat_dir / f"{month}.md"
        if path.exists():
            content = path.read_text()
            seen = {mid for _, mid in ENTRY_RE.findall(content)}
        else:
            chat_dir.mkdir(parents=True, exist_ok=True)
            fm = ["---"] + [f"{k}: {yaml_str(v)}" for k, v in front.items()] \
                + [f'month: "{month}"', "times: UTC", "---", "", f"# {month} — {heading}", ""]
            content = "\n".join(fm)
            seen = set()
        chunks = []
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            chunks.append(f"\n### {ts_display(m['ts'])} — {m['sender']}\n"
                          f"<!-- id: {m['id']} -->\n\n{m['body']}\n")
            written += 1
        if chunks:
            atomic_write(path, content.rstrip("\n") + "\n" + "".join(chunks))
    return written


def newest_ts_on_disk(chat_dir: Path) -> str | None:
    """Newest message timestamp actually mirrored on disk — the durable truth
    against which the state watermark is clamped (a transaction's commit can
    be discarded by a rebase/push failure after the state was already saved).
    Unparseable heading timestamps are skipped so a hostile body line can
    never crash the clamp."""
    if not chat_dir.exists():
        return None
    for path in sorted((p for p in chat_dir.glob("*.md") if MONTH_FILE_RE.match(p.name)),
                       reverse=True):
        best = None
        for ts, _ in ENTRY_RE.findall(path.read_text()):
            try:
                dt = parse_ts(ts.replace(" ", "T") + "Z")
            except ValueError:
                continue
            if best is None or dt > best[0]:
                best = (dt, ts)
        if best:
            return best[1].replace(" ", "T") + "Z"
    return None


def disk_owner_maps() -> tuple[dict, dict]:
    """({space_id: dirname}, {dirname: space_id}) recovered from committed
    month-file frontmatter — slug pinning must survive the loss of the
    external state file (a transaction may start from a fresh checkout), or
    two colliding spaces could interleave into one directory."""
    by_id, by_dir = {}, {}
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir()):
            if not d.is_dir():
                continue
            for p in sorted(p for p in d.glob("*.md") if MONTH_FILE_RE.match(p.name)):
                mm = SPACE_ID_RE.search(p.read_text()[:2000])
                if mm:
                    by_id.setdefault(mm.group(1), d.name)
                    by_dir[d.name] = mm.group(1)
                break
    return by_id, by_dir


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    state = None
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            raise RuntimeError("state file is corrupt or unreadable; preserve it for recovery") from None
        if not isinstance(state, dict) or state.get("version", 1) != 1:
            raise RuntimeError("unsupported archive state format")
        if (not isinstance(state.get("spaces", {}), dict)
                or any(not isinstance(item, dict) for item in state.get("spaces", {}).values())
                or not isinstance(state.get("users", {}), dict)):
            raise RuntimeError("invalid archive state records; preserve them for recovery")
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    if not isinstance(state.get("spaces"), dict):
        state["spaces"] = {}
    state["spaces"] = {k: v for k, v in state["spaces"].items() if isinstance(v, dict)}
    if not isinstance(state.get("users"), dict):
        state["users"] = {}
    return state


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("refusing to replace a symbolic link")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def save_state(state: dict):
    atomic_write(STATE_FILE, json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def fetch_space(space: dict, since_iso: str | None, max_pages: int,
                users: Users) -> tuple[list[dict], bool]:
    """All messages after since_iso (threads come back inline — no extra
    replies call needed, unlike Slack). Returns (messages, complete)."""
    flt = f'createTime > "{since_iso}"' if since_iso else None
    raw, complete = paginate(f"{CHAT_API}/{space['name']}/messages", "messages",
                             max_pages, filter=flt, orderBy="createTime ASC")
    msgs = []
    for m in raw:
        n = norm_message(m, users)
        if n:
            msgs.append(n)
    return msgs, complete


def cmd_list_spaces(cfg: dict):
    state = load_state()
    users = Users(state.get("users"))
    spaces = list_spaces(cfg, users, state)
    log(f"google-chat: {len(spaces)} spaces visible:")
    for s in sorted(spaces, key=lambda x: (x["_type"], x["_name"])):
        log(f"  [{s['_type']:5s}] {s['_name']}  ({s['name']})")
    log("Select spaces through the private configuration googlechat section.")


def cmd_peek(cfg: dict, match: str, limit: int):
    state = load_state()
    users = Users(state.get("users"))
    for s in list_spaces(cfg, users, state):
        if match.lower() in f"{s['_name']} | {s['name']}".lower():
            data = api_get(f"{CHAT_API}/{s['name']}/messages",
                           pageSize=limit, orderBy="createTime DESC")
            raw = (data or {}).get("messages") or []
            msgs = [n for n in (norm_message(m, users) for m in raw) if n]
            log(f"# {s['_name']}  ({s['name']})\n")
            for m in sorted(msgs, key=lambda x: parse_ts(x["ts"])):
                log(f"### {ts_display(m['ts'])} — {m['sender']}\n{m['body']}\n")
            return
    raise RuntimeError("no space matches the requested name or ID")


def cmd_sync(cfg: dict, dry_run=False):
    if not cfg.get("enabled", True):
        log("google-chat sync disabled in config; skipping")
        return
    if not (cfg.get("chats") or cfg["mode"] == "blacklist"):
        log("google-chat: whitelist empty — nothing configured to sync")
        return

    state = load_state()
    users = Users(state.get("users"))
    spaces = list_spaces(cfg, users, state)
    selected = select_spaces(spaces, cfg)
    log(f"google-chat: {len(spaces)} spaces visible, {len(selected)} selected")

    total = 0
    slug_owner = {ss["slug"]: sid for sid, ss in state["spaces"].items()
                  if isinstance(ss.get("slug"), str)}
    disk_by_id = disk_by_dir = None
    for space, slug in selected:
        sid = space["name"]
        sstate = state["spaces"].setdefault(sid, {})
        pinned = sstate.get("slug") if isinstance(sstate.get("slug"), str) else None
        if pinned:
            slug = pinned
        else:
            if disk_by_id is None:
                disk_by_id, disk_by_dir = disk_owner_maps()
            if sid in disk_by_id:
                slug = disk_by_id[sid]  # recover the pin from committed frontmatter
            elif slug_owner.get(slug, sid) != sid or disk_by_dir.get(slug, sid) != sid:
                base = slug
                slug = f"{slug}-{sid.split('/')[-1][-6:]}"
                warn(f"google-chat: slug collision on {base!r}; using {slug!r}")
        if slug_owner.get(slug, sid) != sid or (disk_by_dir or {}).get(slug, sid) != sid:
            raise RuntimeError("archive directory belongs to another space; configure unique aliases")
        slug_owner[slug] = sid
        safe_slug(slug)
        if not (OUTPUT_DIR / slug).resolve().is_relative_to(OUTPUT_DIR.resolve()):
            raise RuntimeError("archive directory resolves outside configured output")
        sstate["slug"] = slug

        time.sleep(RATE_DELAY)  # pace every space, even ones that skip below

        watermark = sstate.get("watermark")
        disk_newest = newest_ts_on_disk(OUTPUT_DIR / slug)
        if watermark:
            try:
                wm_dt = parse_ts(watermark)
            except (ValueError, TypeError, AttributeError):
                warn(f"{slug}: bad watermark {watermark!r}; resetting to bootstrap window")
                watermark = None
                sstate.pop("watermark", None)
        if watermark:
            if disk_newest is None:
                warn(f"{slug}: watermark {watermark} but nothing on disk — re-bootstrapping")
                watermark = None
                sstate.pop("watermark", None)
            # disk headings have second precision; allow 1s before calling it a hole
            elif wm_dt > parse_ts(disk_newest) + timedelta(seconds=1):
                warn(f"{slug}: watermark {watermark} ahead of disk {disk_newest} "
                     "(a prior tick's commit was likely discarded) — clamping")
                watermark = disk_newest
                sstate["watermark"] = watermark  # persist the repair now: an empty
                # re-fetch would otherwise leave the stale value warning every tick
        if not watermark:
            watermark = (datetime.now(timezone.utc)
                         - timedelta(days=int(cfg["bootstrap_days"]))
                         ).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch from a short overlap before the watermark (id dedupe absorbs
        # the replay): full-precision watermarks vs second-precision disk
        # headings leave a sub-second hole after a discarded commit that the
        # 1s clamp tolerance cannot see; the overlap covers it (overlapping reads tolerate that precision loss).
        since = (parse_ts(watermark) - timedelta(minutes=OVERLAP_MIN)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        msgs, complete = fetch_space(space, since, int(cfg["max_pages"]), users)
        if not complete:
            warn(f"{slug}: incomplete fetch — backlog continues draining next run")
        if not msgs:
            # Nothing renderable in the window; the watermark stays put (it
            # must only ever hold an on-disk timestamp, or the disk clamp
            # would fire on every quiet tick). One cheap empty call per run.
            continue
        front = {"platform": "google-chat", "space_id": sid,
                 "space_name": space["_name"], "space_type": space["_type"]}
        n = len(msgs) if dry_run else append_messages(OUTPUT_DIR / slug, front, space["_name"], msgs)
        # Advance even on an INCOMPLETE read: ASC ordering makes msgs a
        # contiguous prefix of the window and every one of them was just
        # written to disk, so the unfetched remainder is strictly newer —
        # this is what drains a >max_pages backlog across runs instead of
        # refetching the same oldest prefix forever. Never move backwards.
        newest = max((m["ts"] for m in msgs), key=parse_ts)
        sstate["watermark"] = max(newest, watermark, key=parse_ts)
        total += n
        if n:
            log(f"  {slug}: +{n} messages")

    state["users"] = dict(users)
    if not dry_run:
        save_state(state)
    log(f"google-chat: {'dry run, fetched' if dry_run else 'done, new'} messages: {total}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("GOOGLE_CHAT_ARCHIVE_CONFIG"))
    parser.add_argument("--base-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--state-file")
    parser.add_argument("--token-file")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-spaces", action="store_true")
    modes.add_argument("--peek", metavar="MATCH")
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    parser.add_argument("--peek-limit", type=int, default=30)
    parser.add_argument("--skip-unconfigured", action="store_true", help="skip only a missing credential file on a non-owner host")
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("supply --config or GOOGLE_CHAT_ARCHIVE_CONFIG")
    if not 1 <= args.peek_limit <= 1000:
        parser.error("--peek-limit must be 1..1000")
    try:
        cfg = load_config(args)
        if not cfg["enabled"] and not (args.list_spaces or args.peek or args.doctor):
            log("google-chat: disabled in private configuration")
            return 0
        if not TOKEN_FILE.exists():
            if args.skip_unconfigured:
                log("SKIP Google Chat credential is not installed")
                return 0
            raise AuthProblem("configured OAuth credential is missing")
        if args.doctor:
            sample = api_get(CHAT_API + "/spaces", pageSize=1).get("spaces", [])
            if sample:
                name = sample[0]["name"]
                api_get(f"{CHAT_API}/{name}/messages", pageSize=1)
                api_get(f"{CHAT_API}/{name}/members", pageSize=1)
            log("OK Google Chat authentication and read access; archive and state unchanged")
        elif args.list_spaces:
            cmd_list_spaces(cfg)
        elif args.peek:
            cmd_peek(cfg, args.peek, args.peek_limit)
        else:
            cmd_sync(cfg, dry_run=args.dry_run)
    except (AuthProblem, RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
        detail = str(error) if isinstance(error, (AuthProblem, RuntimeError)) else "invalid configuration, local file, or API response"
        warn(f"google-chat: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
