"""Create a caller-selected OAuth credential with separate read and write grants."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from .config import default_config, load
from .oauth import OAuthError, exchange

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE_GROUPS = {
    "drive": ["https://www.googleapis.com/auth/drive.readonly"],
    "chat": [
        "https://www.googleapis.com/auth/chat.spaces.readonly",
        "https://www.googleapis.com/auth/chat.messages.readonly",
        "https://www.googleapis.com/auth/chat.memberships.readonly",
    ],
    "people": ["https://www.googleapis.com/auth/directory.readonly"],
    "docs-create": ["https://www.googleapis.com/auth/drive.file"],
    "docs-write": [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/presentations",
    ],
    "drive-share": ["https://www.googleapis.com/auth/drive"],
}
WRITE_GROUPS = {"docs-create", "docs-write", "drive-share"}
READ_GROUPS = {"drive", "chat", "people"}
WRITE_SCOPES = frozenset(s for group in WRITE_GROUPS for s in SCOPE_GROUPS[group])
READ_SCOPES = frozenset(s for group in READ_GROUPS for s in SCOPE_GROUPS[group])


def settings_for(path=None):
    selected = Path(os.path.expandvars(str(path or default_config()))).expanduser()
    if (
        path is not None
        or os.environ.get("GOOGLE_DOCS_AUTHORITY_CONFIG")
        or selected.exists()
    ):
        return load(selected)
    return {}


def path_value(value):
    expanded = os.path.expandvars(str(value))
    return Path(expanded).expanduser().absolute()


def same_file(a: Path, b: Path) -> bool:
    if a.resolve() == b.resolve():
        return True
    try:
        return a.samefile(b)
    except OSError:
        return False


def select_scopes(raw: str) -> tuple[set[str], list[str]]:
    groups = {group.strip().lower() for group in raw.split(",")}
    if not groups or groups - SCOPE_GROUPS.keys():
        raise ValueError("scope-group-invalid")
    if groups & READ_GROUPS and groups & WRITE_GROUPS:
        raise ValueError("read-and-write-scopes-must-use-separate-tokens")
    return groups, sorted({scope for group in groups for scope in SCOPE_GROUPS[group]})


def output_path(settings, explicit, groups, *, replace=False) -> Path:
    write = bool(groups & WRITE_GROUPS)
    selected = explicit or settings.get(
        "write_token_file" if write else "read_token_file"
    )
    if not selected:
        if write:
            raise ValueError("write-token-output-required")
        selected = "gdocs-token.json"
    path = path_value(selected)
    opposite = settings.get("read_token_file" if write else "write_token_file")
    protected = [path_value(opposite)] if opposite else []
    if write:
        protected.append(path_value("gdocs-token.json"))
    if any(same_file(path, other) for other in protected):
        raise ValueError("read-and-write-token-paths-must-be-distinct")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("token-output-must-be-regular-file")
    if path.exists():
        if not replace:
            raise ValueError("token-output-exists-use-replace")
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            scope = prior.get("scope", "")
            if not isinstance(scope, str):
                raise ValueError
            previous_scopes = set(scope.split())
        except (OSError, ValueError, AttributeError):
            raise ValueError("existing-token-invalid") from None
        if previous_scopes & (READ_SCOPES if write else WRITE_SCOPES):
            raise ValueError("existing-token-has-opposite-capability")
    return path


def read_client(path):
    try:
        blob = json.loads(Path(path).read_text(encoding="utf-8"))
        client = blob.get("installed") or blob.get("web") or blob
        result = {key: client[key] for key in ("client_id", "client_secret")}
        if any(not isinstance(value, str) or not value for value in result.values()):
            raise ValueError
        return result
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        raise ValueError("oauth-client-unreadable-or-invalid") from None


def code_from_callback(raw, state, redirect_uri):
    url = urllib.parse.urlsplit(raw)
    expected = urllib.parse.urlsplit(redirect_uri)
    if (url.scheme, url.netloc, url.path.rstrip("/")) != (
        expected.scheme,
        expected.netloc,
        expected.path.rstrip("/"),
    ):
        raise ValueError("oauth-callback-origin-invalid")
    query = urllib.parse.parse_qs(url.query)
    if query.get("state") != [state]:
        raise ValueError("oauth-state-mismatch")
    if "error" in query:
        raise ValueError("oauth-consent-declined")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise ValueError("oauth-code-missing")
    return codes[0]


def authorize(client, scopes, *, manual=False, port=0, timeout=600):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    done = threading.Event()
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                received["code"] = code_from_callback(
                    redirect_uri + self.path, state, redirect_uri
                )
            except ValueError as exc:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b"Invalid authorization callback. Return to the terminal."
                )
                if str(exc) == "oauth-consent-declined":
                    received["error"] = True
                    done.set()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. Return to the terminal.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = (
        AUTH_ENDPOINT
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    thread = None
    try:
        print("Open this URL in a browser to authorize the requested scopes:\n" + url)
        if manual:
            server.server_close()
            print(
                "Paste the full redirected loopback URL, including its state parameter."
            )
            received["code"] = code_from_callback(
                input("Redirected URL: ").strip(), state, redirect_uri
            )
        else:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            webbrowser.open(url)
            if not done.wait(timeout):
                raise ValueError("oauth-consent-timeout")
            if received.get("error"):
                raise ValueError("oauth-consent-declined")
    finally:
        if thread is not None:
            server.shutdown()
            thread.join(timeout=5)
        server.server_close()
    token = exchange(
        {
            **client,
            "code": received["code"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    )
    refresh = token.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise OAuthError("oauth-refresh-token-missing")
    granted = token.get("scope", " ".join(scopes))
    if not isinstance(granted, str) or set(scopes) != set(granted.split()):
        raise OAuthError("oauth-requested-scopes-not-granted")
    if set(granted.split()) & READ_SCOPES and set(granted.split()) & WRITE_SCOPES:
        raise OAuthError("oauth-mixed-capabilities-refused")
    return {
        "type": "authorized_user",
        **client,
        "refresh_token": refresh,
        "scope": granted,
    }


def write_token(path, token, *, replace=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(token, stream, indent=2)
            stream.write("\n")
        if replace:
            os.replace(temporary, path)
        else:
            # Linking the complete file refuses a target created while consent was open.
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_json", nargs="?", type=Path)
    parser.add_argument("--client-from-token", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scopes", default="drive,chat,people")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing same-capability token",
    )
    args = parser.parse_args(argv)
    try:
        if bool(args.client_json) == bool(args.client_from_token):
            raise ValueError("select-client-json-or-client-from-token")
        if not 0 <= args.port <= 65535:
            raise ValueError("oauth-port-invalid")
        groups, scopes = select_scopes(args.scopes)
        settings = settings_for(args.config)
        path = output_path(settings, args.output, groups, replace=args.replace)
        client = read_client(path_value(args.client_from_token or args.client_json))
        token = authorize(client, scopes, manual=args.manual, port=args.port)
        # Recheck capability paths after interactive consent and before replacement.
        output_path(settings, args.output, groups, replace=args.replace)
        write_token(path, token, replace=args.replace)
        print(
            f"OK {'write' if groups & WRITE_GROUPS else 'read'} token written to {path}"
        )
        return 0
    except (OSError, ValueError, EOFError):
        print(
            "FAIL oauth-configuration-consent-or-token-exchange-failed", file=sys.stderr
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
