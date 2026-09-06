"""Shared OAuth exchange with fixed endpoints and safe diagnostics."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


class OAuthError(ValueError):
    """An input or remote failure whose message contains no credential material."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect-refused", headers, fp
        )


def open_request(*args, **kwargs):
    return urllib.request.build_opener(NoRedirect()).open(*args, **kwargs)


def exchange(fields: dict[str, str]) -> dict:
    """Send credentials only to Google's token endpoint, never to a redirect."""
    try:
        data = urllib.parse.urlencode(fields).encode()
        with open_request(TOKEN_ENDPOINT, data=data, timeout=60) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("error"):
            raise OAuthError("token-response-invalid")
        return result
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"token-exchange-http-{exc.code}") from None
    except (OSError, ValueError, TypeError):
        raise OAuthError("token-response-unreadable-or-invalid") from None


def refresh_access_token(
    path: Path, *, required_any_scopes=(), forbidden_scopes=()
) -> str:
    try:
        token = json.loads(Path(path).read_text(encoding="utf-8"))
        fields = {
            name: token[name]
            for name in ("client_id", "client_secret", "refresh_token")
        }
        if any(not isinstance(value, str) or not value for value in fields.values()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise OAuthError("token-file-unreadable-or-invalid") from None
    result = exchange({**fields, "grant_type": "refresh_token"})
    scope = result.get("scope", "")
    if not isinstance(scope, str):
        raise OAuthError("token-scope-invalid")
    scopes = set(scope.split())
    if required_any_scopes and not scopes.intersection(required_any_scopes):
        raise OAuthError("token-required-scope-missing")
    if scopes.intersection(forbidden_scopes):
        raise OAuthError("read-token-has-write-scope")
    access = result.get("access_token")
    if not isinstance(access, str) or not access or any(c.isspace() for c in access):
        raise OAuthError("token-access-token-invalid")
    return access
