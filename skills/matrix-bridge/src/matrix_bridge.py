"""Text and file transfer through one explicitly configured Matrix room."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class BridgeError(Exception):
    pass


class HTTPFailure(BridgeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Matrix request returned HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPFailure(code)


def path_value(value, base):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BridgeError("invalid configured path")
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else base / path


@dataclass(frozen=True)
class Config:
    homeserver: str
    room_id: str
    user_id: str
    auth_file: Path
    state_file: Path
    inbox_dir: Path
    max_file_bytes: int
    timeline_limit: int

    @classmethod
    def load(cls, filename=None):
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        default = config_root / "matrix-bridge/config.json"
        if not os.path.lexists(default) and os.path.lexists(config_root / "phone-bridge/config.json"):
            default = config_root / "phone-bridge/config.json"
        path = Path(filename or os.environ.get("MATRIX_BRIDGE_CONFIG")
                    or os.environ.get("PHONE_BRIDGE_CONFIG") or default).expanduser().resolve()
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            raise BridgeError("cannot read matrix-bridge configuration; use --config or MATRIX_BRIDGE_CONFIG") from None
        if not isinstance(value, dict) or value.get("schema") not in ("matrix-bridge/v1", "phone-bridge/v1"):
            raise BridgeError("configuration requires schema matrix-bridge/v1")
        fields = {"schema", "homeserver", "room_id", "user_id", "auth_file", "state_file", "inbox_dir", "max_file_bytes", "timeline_limit"}
        if value.keys() - fields:
            raise BridgeError("configuration contains unknown fields")
        for key in ("homeserver", "room_id", "user_id"):
            if not isinstance(value.get(key), str) or not value[key] or any(c.isspace() for c in value[key]):
                raise BridgeError(f"configuration requires a valid {key}")
        hs = urllib.parse.urlsplit(value["homeserver"])
        if (not hs.hostname or hs.username or hs.password or hs.query or hs.fragment
                or hs.scheme not in ("https", "http")
                or (hs.scheme == "http" and hs.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise BridgeError("homeserver must use HTTPS (HTTP is permitted only on loopback)")
        if not value["room_id"].startswith("!") or not value["user_id"].startswith("@"):
            raise BridgeError("configure an explicit Matrix room ID and user ID")
        maximum = value.get("max_file_bytes", 50 * 1024 * 1024)
        limit = value.get("timeline_limit", 100)
        if type(maximum) is not int or maximum <= 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise BridgeError("max_file_bytes must be positive; timeline_limit must be 1..1000")
        storage_name = value["schema"].split("/")[0]
        state = path_value(value.get("state_file", f"~/.local/state/{storage_name}/since"), path.parent)
        inbox = path_value(value.get("inbox_dir", f"~/.cache/{storage_name}/inbox"), path.parent)
        auth = path_value(value.get("auth_file"), path.parent)
        if state.resolve() == auth.resolve():
            raise BridgeError("state_file and auth_file must be different")
        return cls(value["homeserver"].rstrip("/"), value["room_id"], value["user_id"], auth, state, inbox, maximum, limit)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BridgeError("refusing to replace a symbolic link")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".matrix-bridge-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def receive_lock(state):
    state.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(state) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("another receiver owns this cursor") from None
        yield
    finally:
        os.close(descriptor)


class Client:
    def __init__(self, config):
        self.config = config
        try:
            header = config.auth_file.read_text().strip()
        except OSError:
            raise BridgeError("cannot read configured auth_file") from None
        if not re.fullmatch(r"Authorization:\s*Bearer [^\s]+", header, re.IGNORECASE):
            raise BridgeError("auth_file must contain one Authorization: Bearer header")
        self.authorization = header.split(":", 1)[1].strip()
        self.opener = urllib.request.build_opener(NoRedirect())
        self.room = urllib.parse.quote(config.room_id, safe="")

    def request(self, method, path, *, data=None, content_type="application/json", binary=False, timeout=60):
        request = urllib.request.Request(self.config.homeserver + path, data=data, method=method,
                                        headers={"Authorization": self.authorization, "Content-Type": content_type})
        maximum = self.config.max_file_bytes if binary else 16 * 1024 * 1024
        try:
            with self.opener.open(request, timeout=timeout) as response:
                payload = response.read(maximum + 1)
        except urllib.error.HTTPError as error:
            raise HTTPFailure(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError("Matrix network request failed") from None
        if len(payload) > maximum:
            raise BridgeError("Matrix response exceeds the configured size limit")
        if binary:
            return payload
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise BridgeError("Matrix returned invalid JSON") from None
        if not isinstance(result, dict):
            raise BridgeError("Matrix returned an invalid response")
        return result

    def doctor(self):
        identity = self.request("GET", "/_matrix/client/v3/account/whoami")
        if identity.get("user_id") != self.config.user_id:
            raise BridgeError("authenticated user does not match configured user_id")
        # Read room state in one request: failure is not evidence of plaintext.
        path = f"/_matrix/client/v3/rooms/{self.room}/state/m.room.encryption"
        try:
            self.request("GET", path)
        except HTTPFailure as error:
            if error.status != 404:
                raise
        else:
            raise BridgeError("encrypted rooms are not supported")
        membership = self.request("GET", f"/_matrix/client/v3/rooms/{self.room}/state/m.room.member/{urllib.parse.quote(self.config.user_id, safe='')}")
        if membership.get("membership") != "join":
            raise BridgeError("configured user is not joined to the room")

    def send_event(self, content):
        transaction = uuid.uuid4().hex
        result = self.request("PUT", f"/_matrix/client/v3/rooms/{self.room}/send/m.room.message/{transaction}",
                              data=json.dumps(content).encode())
        event = result.get("event_id")
        if not isinstance(event, str) or not event:
            raise BridgeError("Matrix did not confirm an event ID; check the room before retrying")
        return event

    def send(self, values, mode):
        paths = [Path(value).expanduser() for value in values]
        files = mode == "file" or (mode == "auto" and all(path.exists() for path in paths))
        if files:
            # Validate all local inputs before the first upload.
            for path in paths:
                if not path.is_file() or path.stat().st_size > self.config.max_file_bytes:
                    raise BridgeError("file is missing, not regular, or exceeds max_file_bytes")
        self.doctor()
        if not files:
            event = self.send_event({"msgtype": "m.text", "body": " ".join(values)})
            print(f"sent text {event}")
            return
        for path in paths:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            if len(data) > self.config.max_file_bytes:
                raise BridgeError("file grew beyond max_file_bytes")
            query = urllib.parse.urlencode({"filename": path.name})
            result = self.request("POST", "/_matrix/media/v3/upload?" + query, data=data, content_type=mime)
            uri = result.get("content_uri")
            if not isinstance(uri, str) or not uri.startswith("mxc://"):
                raise BridgeError("Matrix did not confirm an uploaded media URI")
            kind = "m.image" if mime.startswith("image/") else "m.file"
            event = self.send_event({"msgtype": kind, "body": path.name, "url": uri,
                                     "info": {"mimetype": mime, "size": len(data)}})
            print(f"sent {kind} {path.name} ({len(data):,}B) {event}")

    def sync(self, since, timeout_ms):
        filter_value = {"room": {"rooms": [self.config.room_id], "timeline": {"limit": self.config.timeline_limit}},
                        "presence": {"types": []}, "account_data": {"types": []}}
        params = {"filter": json.dumps(filter_value), "timeout": timeout_ms, "set_presence": "offline"}
        if since is not None:
            params["since"] = since
        result = self.request("GET", "/_matrix/client/v3/sync?" + urllib.parse.urlencode(params), timeout=timeout_ms / 1000 + 30)
        if not isinstance(result.get("next_batch"), str) or not result["next_batch"]:
            raise BridgeError("Matrix sync omitted next_batch; cursor unchanged")
        return result

    def download(self, uri, name):
        parsed = urllib.parse.urlsplit(uri)
        media = parsed.path.removeprefix("/")
        if parsed.scheme != "mxc" or not parsed.netloc or not media or "/" in media or parsed.query or parsed.fragment:
            raise BridgeError("invalid Matrix media URI; cursor unchanged")
        suffix = urllib.parse.quote(parsed.netloc, safe="") + "/" + urllib.parse.quote(media, safe="")
        try:
            data = self.request("GET", "/_matrix/client/v1/media/download/" + suffix, binary=True)
        except HTTPFailure as error:
            if error.status not in (404, 405):
                raise
            data = self.request("GET", "/_matrix/media/v3/download/" + suffix, binary=True)
        safe_name = re.sub(r"[^\w. -]", "_", str(name).replace("\\", "/").rsplit("/", 1)[-1])[:160] or "file"
        path = self.config.inbox_dir / (hashlib.sha256(uri.encode()).hexdigest()[:16] + "-" + safe_name)
        atomic_write(path, data)
        return path

    def receive(self, wait=False, wait_seconds=600):
        self.doctor()
        with receive_lock(self.config.state_file):
            try:
                since = self.config.state_file.read_text().strip()
            except FileNotFoundError:
                since = None
            if since == "":
                raise BridgeError("cursor is empty; refusing to silently discard history")
            if since is None:
                result = self.sync(None, 0)
                atomic_write(self.config.state_file, result["next_batch"].encode())
                print("initialized; existing history was not replayed")
                return
            deadline = time.monotonic() + wait_seconds
            while True:
                timeout_ms = min(30000, max(0, int((deadline - time.monotonic()) * 1000))) if wait else 0
                result = self.sync(since, timeout_ms)
                room = result.get("rooms", {}).get("join", {}).get(self.config.room_id, {})
                timeline = room.get("timeline", {})
                if timeline.get("limited"):
                    raise BridgeError("sync timeline is incomplete; cursor unchanged (increase timeline_limit or recover history in a Matrix client)")
                lines = []
                for event in timeline.get("events", []):
                    if event.get("sender") == self.config.user_id:
                        continue
                    if event.get("type") == "m.room.encrypted":
                        raise BridgeError("encrypted event cannot be read; cursor unchanged")
                    if event.get("type") != "m.room.message":
                        continue
                    content = event.get("content") or {}
                    stamp = time.strftime("%H:%M", time.localtime(event.get("origin_server_ts", 0) / 1000))
                    if content.get("msgtype") in ("m.image", "m.file", "m.audio", "m.video"):
                        if "file" in content or not isinstance(content.get("url"), str):
                            raise BridgeError("attachment is encrypted or missing its URL; cursor unchanged")
                        path = self.download(content["url"], content.get("body", "file"))
                        lines.append(f"[{stamp}] {content['msgtype']}: {content.get('body', '')} -> {path}")
                    else:
                        lines.append(f"[{stamp}] text: {content.get('body', '')}")
                if lines or not wait or time.monotonic() >= deadline:
                    for line in lines or ["(no new messages)"]:
                        print(line, flush=True)
                    atomic_write(self.config.state_file, result["next_batch"].encode())
                    return
                since = result["next_batch"]


def main(kind, argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="private JSON configuration (or MATRIX_BRIDGE_CONFIG)")
    parser.add_argument("--doctor", action="store_true", help="read-only account and room verification")
    if kind == "send":
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--text", action="store_true", help="treat arguments as text even if filenames match")
        modes.add_argument("--file", action="store_true", help="require all arguments to be existing files")
        parser.add_argument("values", nargs="*")
    else:
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if kind == "send" and not args.doctor and not args.values:
        parser.error("provide text or files")
    if kind != "send" and not 1 <= args.wait_seconds <= 600:
        parser.error("--wait-seconds must be 1..600")
    try:
        client = Client(Config.load(args.config))
        if args.doctor:
            client.doctor()
            print("OK configured account and plaintext room are accessible; cursor unchanged")
        elif kind == "send":
            client.send(args.values, "file" if args.file else "text" if args.text else "auto")
        else:
            client.receive(args.wait, args.wait_seconds)
    except (BridgeError, OSError, ValueError, TypeError, KeyError) as error:
        message = str(error) if isinstance(error, BridgeError) else "local file or response processing failed"
        print("FAIL " + message, file=sys.stderr)
        return 1
    return 0


def send_main():
    return main("send")


def recv_main():
    return main("recv")
