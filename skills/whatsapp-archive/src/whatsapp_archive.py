#!/usr/bin/env python3
"""Archive selected WhatsApp spool messages as deterministic monthly Markdown."""

import argparse
import hashlib
import json
import os
import re
import sys
import subprocess

import yaml
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE = BASE_DIR = OUTPUT_DIR = STATE_FILE = None
COMMAND_ENV = None


class ArchiveError(Exception):
    pass


def path_setting(value, base):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ArchiveError("configured paths must be nonempty strings")
    path = Path(value).expanduser()
    return (base / path).resolve()


def configure(config_file, *, base_dir=None, output_dir=None, state_file=None, spool=None):
    global CONFIG_FILE, BASE_DIR, OUTPUT_DIR, STATE_FILE, COMMAND_ENV
    CONFIG_FILE = Path(config_file).resolve()
    try:
        document = yaml.safe_load(CONFIG_FILE.read_text())
    except yaml.YAMLError:
        raise ArchiveError("configuration is not valid YAML") from None
    if not isinstance(document, dict) or not isinstance(document.get("whatsapp"), dict):
        raise ArchiveError("configuration requires a whatsapp mapping")
    cfg = dict(document["whatsapp"])
    # CLI base overrides apply only to output; input locations always use the configured base.
    configured_base = path_setting(cfg.get("base_dir", "."), CONFIG_FILE.parent)
    BASE_DIR = Path(base_dir).expanduser().resolve() if base_dir else configured_base
    OUTPUT_DIR = path_setting(output_dir or cfg.get("output_dir"), BASE_DIR)
    STATE_FILE = path_setting(state_file or cfg.get("state_file"), BASE_DIR)
    cfg["spool_dir"] = str(path_setting(spool or cfg.get("spool_dir"), configured_base))
    environment = cfg.get("command_environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or not isinstance(v, str) or "\x00" in v for k, v in environment.items()
    ):
        raise ArchiveError("command_environment must map variable names to strings")
    COMMAND_ENV = {**os.environ, **environment}
    for key in ("enabled", "refresh_before_sync"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ArchiveError("enabled and refresh_before_sync must be booleans")
    return validate_selection(cfg)


def component(value):
    if not isinstance(value, str) or not value or value in (".", "..") or any(
        c in value for c in '/\\\x00\n\r'
    ):
        raise ArchiveError("chat alias or stored slug is not a safe directory name")
    return value


def log(msg: str):
    print(msg, flush=True)


def warn(msg: str):
    print(f"  [WARN] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Config + spool access
# ---------------------------------------------------------------------------

def validate_selection(cfg):
    mode = cfg.get("mode", "whitelist")
    if not isinstance(mode, str) or mode.lower() not in ("whitelist", "blacklist"):
        raise ArchiveError("mode must be whitelist or blacklist")
    cfg["mode"] = mode.lower()
    entries = cfg.get("chats", [])
    if not isinstance(entries, list):
        raise ArchiveError("chats must be a list")
    for entry in entries:
        match = entry.get("match") if isinstance(entry, dict) else entry
        if not isinstance(match, str) or not match.strip():
            raise ArchiveError("every chat selection must have a nonempty match")
        if isinstance(entry, dict) and "alias" in entry:
            component(entry["alias"])
    return cfg


def spool_dir(cfg):
    return Path(cfg["spool_dir"])


def load_chats_index(sdir):
    file = sdir / "chats.json"
    if not file.exists():
        return {}
    result = json.loads(file.read_text())
    if not isinstance(result, dict) or any(not isinstance(v, dict) for v in result.values()):
        raise ArchiveError("chat index must map identifiers to objects")
    return result


def spool_files(sdir):
    return sorted((sdir / "spool").glob("*.ndjson"))


def read_spool_file(path):
    data = path.read_bytes()
    # A writer may be appending. Retry a partial record without accepting a shorter archive.
    if data and not data.endswith(b"\n"):
        raise ArchiveError("spool has an unfinished record; retry after the bridge flushes")
    rows = []
    for line in data.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, dict) or row.get("v") != 1
                or not isinstance(row.get("chat_jid"), str) or not row["chat_jid"]
                or not isinstance(row.get("ts"), (int, float))
                or not isinstance(row.get("msg_id"), str) or not row["msg_id"]):
            raise ArchiveError("invalid spool message")
        month_of(row["ts"])
        rows.append(row)
    return rows, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def chat_display_name(jid: str, chats_idx: dict, fallback: str = "") -> str:
    entry = chats_idx.get(jid) or {}
    return entry.get("name") or fallback or jid


def match_entry(jid: str, name: str, entries: list) -> dict | None:
    text = f"{name} | {jid}".lower()
    for e in entries:
        m = (e.get("match") or "") if isinstance(e, dict) else str(e)
        if m and m.lower() in text:
            return e if isinstance(e, dict) else {"match": m}
    return None


def is_selected(jid: str, name: str, cfg: dict) -> tuple[bool, str | None]:
    """(selected?, alias-or-None) under whitelist/blacklist semantics."""
    entries = cfg.get("chats") or []
    hit = match_entry(jid, name, entries)
    if cfg["mode"] == "blacklist":
        return (hit is None, None)
    return (hit is not None, (hit or {}).get("alias"))


def slugify(text: str, max_len=60) -> str:
    text = re.sub(r'[<>:"/\\|?*#\[\]@]', "", text)
    text = re.sub(r"\s+", "-", text.strip()).strip("-")
    return text[:max_len].rstrip("-") or "untitled"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def ts_iso(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_of(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m")


def render_body(m: dict) -> str:
    text = (m.get("text") or "").strip()
    media = m.get("media")
    if media:
        kind = media.get("kind") or m.get("type") or "media"
        bits = [f"*[{kind}"]
        if media.get("filename"):
            bits.append(f": {media['filename']}")
        bits.append("]*")
        placeholder = "".join(bits)
        caption = (media.get("caption") or "").strip()
        text = "\n\n".join(x for x in (placeholder, caption, text) if x)
    return text


def sender_label(m: dict) -> str:
    if m.get("from_me"):
        return "me"
    return m.get("sender_name") or (m.get("sender_jid") or "unknown").split("@")[0]


def yaml_str(v) -> str:
    return json.dumps(str(v), ensure_ascii=False)


def render_month(chat_jid: str, name: str, chat_type: str, month: str, msgs: list[dict]) -> str:
    lines = [
        "---",
        'platform: "whatsapp"',
        f"chat_jid: {yaml_str(chat_jid)}",
        f"chat_name: {yaml_str(name)}",
        f"chat_type: {yaml_str(chat_type)}",
        f'month: "{month}"',
        "times: UTC",
        "---",
        "",
        # month-first H1: a chat literally named 'Summary' must not trip the
        # structure-lint Raw/ purity heading check (prefix-anchored regex)
        f"# {month} — {name}",
    ]
    for m in msgs:
        body = render_body(m)
        if not body:
            continue
        ts_disp = ts_iso(m["ts"]).replace("T", " ").replace("Z", "")
        lines.append(f"\n### {ts_disp} — {sender_label(m)}")
        lines.append(f"<!-- id: {m.get('msg_id', '?')} -->")
        lines.append("")
        lines.append(body)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if not isinstance(state, dict):
        raise ArchiveError("state must be an object")
    state.setdefault("version", 1)
    if state["version"] != 1:
        raise ArchiveError("unsupported state version")
    for key in ("files", "chats"):
        state.setdefault(key, {})
        if not isinstance(state[key], dict):
            raise ArchiveError("invalid synchronization state")
    for value in state["chats"].values():
        if not isinstance(value, dict):
            raise ArchiveError("invalid chat state")
        if value.get("slug"):
            component(value["slug"])
    return state


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def save_state(state):
    atomic_write(STATE_FILE, json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def cmd_list_chats(cfg: dict):
    sdir = spool_dir(cfg)
    idx = load_chats_index(sdir)
    if not idx:
        warn(f"no chats.json under {sdir} — is the bridge paired and running? "
             "(see the Skill configuration reference)")
        return
    log(f"{len(idx)} chats known to the bridge:")
    for jid, e in sorted(idx.items(), key=lambda kv: -(kv[1].get("last_ts") or 0)):
        last = ts_iso(e["last_ts"]) if e.get("last_ts") else "never"
        log(f"  [{e.get('type', '?'):5s}] {e.get('name') or '(unnamed)'}  last={last}")
        log(f"          jid: {jid}")
    log("\nAdd entries to the private configuration → whatsapp.chats to start mirroring."
        "\n(Selection changes trigger a full-history rebuild for the affected chats on the"
        "\nnext sync; `--full` forces one manually.)")


def cmd_peek(cfg: dict, match: str, limit: int):
    sdir = spool_dir(cfg)
    idx = load_chats_index(sdir)
    target_jid = None
    for jid in idx:
        if match.lower() in f"{idx[jid].get('name', '')} | {jid}".lower():
            target_jid = jid
            break
    if target_jid is None:
        warn(f"no spooled chat matches {match!r}")
        return
    collected = []
    for f in reversed(spool_files(sdir)):
        for m in read_spool_file(f)[0]:
            if m.get("chat_jid") == target_jid:
                collected.append(m)
        if len(collected) >= limit:
            break
    collected.sort(key=lambda m: m.get("ts") or 0)
    log(f"# {chat_display_name(target_jid, idx)}  ({target_jid})\n")
    for m in collected[-limit:]:
        body = render_body(m)
        if body:
            log(f"### {ts_iso(m['ts'])} — {sender_label(m)}\n{body}\n")


def cmd_sync(cfg: dict, full: bool, dry_run=False, allow_missing=False):
    if not cfg.get("enabled", True):
        log("whatsapp sync disabled in config; skipping")
        return
    sdir = spool_dir(cfg)
    files = spool_files(sdir)
    if not files:
        if allow_missing:
            log("whatsapp: no local spool; skipping as requested")
            return
        raise ArchiveError("no spool files found")

    idx = load_chats_index(sdir)
    state = load_state()
    snapshots = {file.name: read_spool_file(file) for file in files}
    if set(state["files"]) - set(snapshots):
        raise ArchiveError("previously archived spool files are missing")

    # Selection changes (newly whitelisted chat, mode flip) must backfill the
    # chat's whole spooled history, not just months touched by future messages —
    # detect them via a config hash and force a full rebuild once.
    sel_hash = hashlib.sha256(json.dumps(
        {"mode": cfg["mode"], "chats": cfg.get("chats"), "index": {jid: v.get("name") for jid, v in idx.items()}},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if state.get("config_hash") != sel_hash:
        if state.get("config_hash") is not None:
            log("whatsapp: chat selection changed — rebuilding selected chats from the full spool")
        full = True
        state["config_hash"] = sel_hash

    # 1. Which spool days are new/changed since last run?
    changed = []
    for f in files:
        digest = snapshots[f.name][1]
        if full or state["files"].get(f.name) != digest:
            changed.append(f)
            state["files"][f.name] = digest
    if not changed:
        log("whatsapp: spool unchanged")
        if not dry_run:
            save_state(state)
        return

    # 2. Which (chat, month) pairs do the changed days touch (selected chats only)?
    affected: set[tuple[str, str]] = set()
    names: dict[str, str] = {}
    for f in changed:
        for m in snapshots[f.name][0]:
            jid = m.get("chat_jid")
            if not jid or m.get("ts") is None:
                continue
            name = chat_display_name(jid, idx, m.get("chat_name") or "")
            names[jid] = name
            sel, _ = is_selected(jid, name, cfg)
            if sel:
                affected.add((jid, month_of(m["ts"])))

    if not affected:
        log(f"whatsapp: {len(changed)} spool day(s) changed, but no selected chats touched "
            "(whitelist empty? edit the private configuration)")
        if not dry_run:
            save_state(state)
        return

    # 3. Regenerate each affected (chat, month) from the FULL spool (dedupe + sort).
    #    Reads every spool day once, buckets only what we need.
    buckets: dict[tuple[str, str], dict[str, dict]] = {k: {} for k in affected}
    for f in files:
        for m in snapshots[f.name][0]:
            jid, ts = m.get("chat_jid"), m.get("ts")
            if jid is None or ts is None:
                continue
            key = (jid, month_of(ts))
            if key in buckets:
                mid = m.get("msg_id") or f"ts-{ts}"
                # first write wins: 'live' rows precede 'history' resyncs in practice,
                # and identical ids carry identical content either way
                buckets[key].setdefault(mid, m)

    total = 0
    slug_owner = {cs["slug"]: j for j, cs in state["chats"].items() if cs.get("slug")}
    for (jid, month), by_id in sorted(buckets.items()):
        name = names.get(jid) or chat_display_name(jid, idx)
        _, alias = is_selected(jid, name, cfg)
        cstate = state["chats"].setdefault(jid, {})
        slug = cstate.get("slug") or alias or slugify(name)
        # month files are regenerated wholesale per (jid, month) — two jids on
        # one slug would alternately overwrite each other's mirror
        if slug_owner.get(slug, jid) != jid:
            base = slug
            suffix = re.sub(r"\D", "", jid.split("@")[0])[-6:] or slugify(jid.split("@")[0])
            slug = f"{slug}-{suffix}"
            warn(f"slug collision on {base!r} (owned by {slug_owner[base]}); using {slug!r} for {jid}")
        while slug_owner.get(slug, jid) != jid:
            slug += "-" + hashlib.sha256(jid.encode()).hexdigest()[:10]
        component(slug)
        slug_owner[slug] = jid
        cstate["slug"] = slug
        msgs = sorted(by_id.values(), key=lambda m: (m.get("ts") or 0, m.get("msg_id") or ""))
        chat_type = (idx.get(jid) or {}).get("type") or ("group" if jid.endswith("@g.us") else "dm")
        out = OUTPUT_DIR / slug / f"{month}.md"
        if not out.resolve().is_relative_to(OUTPUT_DIR.resolve()):
            raise ArchiveError("archive path escapes the output directory")
        rendered = render_month(jid, name, chat_type, month, msgs)
        if not out.exists() or out.read_text() != rendered:
            if not dry_run:
                atomic_write(out, rendered)
            log(f"  {slug}/{month}.md: {len(msgs)} messages")
            total += 1

    if not dry_run:
        save_state(state)
    log(f"whatsapp: done, {total} month file(s) regenerated")


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


def publication_output():
    try:
        relative = OUTPUT_DIR.relative_to(BASE_DIR)
    except ValueError:
        raise ArchiveError("publication output must be inside base_dir") from None
    if relative == Path("."):
        raise ArchiveError("publication output must be a subdirectory of base_dir")
    return relative


def cmd_publish(cfg, full=False):
    settings = publication_settings(cfg)
    relative = publication_output()
    values = {"base_dir": str(BASE_DIR), "output_dir": str(relative),
              "state_dir": str(STATE_FILE.parent),
              "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    command = []
    for argument in settings["command"]:
        for key, value in values.items():
            argument = argument.replace("{" + key + "}", value)
        command.append(argument)
    command += [sys.executable, "-B", str(Path(__file__).resolve()),
                "--config", str(CONFIG_FILE), "--base-dir", str(BASE_DIR),
                "--output-dir", str(OUTPUT_DIR), "--state-file", str(STATE_FILE),
                "--spool-dir", cfg["spool_dir"], "--transaction-writer"]
    if full:
        command.append("--full")
    return subprocess.run(command, env=COMMAND_ENV, check=False).returncode


def bridge_command(cfg, mode, seconds=45):
    settings = cfg.get("bridge", {})
    if not isinstance(settings, dict):
        raise ArchiveError("bridge must be a mapping")
    node = settings.get("node", "node")
    if not isinstance(node, str) or not node or "\x00" in node:
        raise ArchiveError("bridge.node must name an executable")
    environment = {**COMMAND_ENV, "WA_BRIDGE_DIR": str(spool_dir(cfg))}
    if settings.get("dependencies_dir"):
        environment["WHATSAPP_BRIDGE_DEPENDENCIES"] = str(path_setting(
            settings["dependencies_dir"], CONFIG_FILE.parent))
    command = [node, str(Path(__file__).resolve().parents[1] / "bridge/bridge.js"), mode]
    if mode == "drain":
        command += ["--seconds", str(seconds)]
    return command, environment


def refresh_bridge(cfg):
    command, environment = bridge_command(cfg, "drain")
    # The bridge normally exits after 45 seconds of quiescence (hard cap 480).
    # Give it time to flush on termination, including when a dependency stalls.
    with subprocess.Popen(command, env=environment) as process:
        try:
            status = process.wait(timeout=540)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise ArchiveError("bridge refresh timed out") from None
    if status:
        raise ArchiveError("bridge refresh failed; archive and state were not changed")


def main(argv=None):
    global OUTPUT_DIR, STATE_FILE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-dir")
    parser.add_argument("--output-dir")
    parser.add_argument("--state-file")
    parser.add_argument("--spool-dir")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-missing-spool", action="store_true",
                        help="allow a non-owner without a local spool to skip")
    parser.add_argument("--peek-limit", type=int, default=30)
    parser.add_argument("--seconds", type=int, default=45)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-chats", action="store_true")
    modes.add_argument("--peek", metavar="MATCH")
    modes.add_argument("--bridge", choices=["login", "daemon", "drain", "chats", "status"])
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--transaction-writer", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.peek_limit <= 0 or not 1 <= args.seconds < 480:
        parser.error("limits must be positive; drain seconds must be below 480")
    if (args.publish or args.transaction_writer) and (args.dry_run or args.allow_missing_spool):
        parser.error("publication requires a real run and an available spool")
    if args.bridge and (args.dry_run or args.full):
        parser.error("bridge cannot be combined with archive options")
    try:
        cfg = configure(args.config, base_dir=args.base_dir, output_dir=args.output_dir,
                        state_file=args.state_file, spool=args.spool_dir)
        if args.doctor:
            if cfg.get("publish"):
                publication_settings(cfg)
                publication_output()
            bridge_command(cfg, "status")
            log("whatsapp: configuration OK")
            return 0
        if args.bridge:
            command, environment = bridge_command(cfg, args.bridge, args.seconds)
            os.execvpe(command[0], command, environment)
        if not cfg.get("enabled", True):
            log("whatsapp: disabled in configuration")
            return 0
        if args.publish:
            return cmd_publish(cfg, args.full)
        if args.transaction_writer:
            settings = publication_settings(cfg)
            relative = publication_output()
            output = Path(os.environ.get(settings["base_env"], ""))
            staged = Path(os.environ.get(settings["state_env"], ""))
            if not output.is_absolute() or not staged.is_absolute():
                raise ArchiveError("publisher must provide absolute worktree and staged-state paths")
            if output.resolve() == BASE_DIR or staged.resolve() == STATE_FILE.parent:
                raise ArchiveError("publisher must isolate archive and synchronization state")
            OUTPUT_DIR = output / relative
            STATE_FILE = staged / STATE_FILE.name
        if args.list_chats:
            cmd_list_chats(cfg)
        elif args.peek:
            cmd_peek(cfg, args.peek, args.peek_limit)
        else:
            if cfg.get("refresh_before_sync", False) and not args.dry_run:
                if not (args.allow_missing_spool and not spool_files(spool_dir(cfg))):
                    refresh_bridge(cfg)
            cmd_sync(cfg, args.full, args.dry_run, args.allow_missing_spool)
        return 0
    except (ArchiveError, OSError, ValueError, TypeError, OverflowError) as exc:
        # Input can contain messages, identifiers, or credentials. Do not echo it.
        warn(str(exc) if isinstance(exc, ArchiveError) else "archive operation failed; check input integrity")
        return 1


if __name__ == "__main__":
    sys.exit(main())

