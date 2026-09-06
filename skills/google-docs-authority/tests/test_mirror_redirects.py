"""Exercise the standard redirect chain with a synthetic, network-free transport."""

import io
from email.message import Message
from urllib.error import HTTPError
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from urllib.response import addinfourl

import pytest

from google_docs_authority import mirror, oauth

EXPORT = "https://docs.google.com/document/d/synthetic/export?format=markdown"
ASSET = "https://synthetic.googleusercontent.com/export"


@pytest.fixture
def transport(monkeypatch):
    routes = {}
    requests = []

    class SyntheticTransport(HTTPHandler, HTTPSHandler):
        def http_open(self, request):
            requests.append(request)
            assert request.full_url in routes, "unexpected synthetic request"
            status, location, body = routes[request.full_url]
            headers = Message()
            if location is not None:
                headers["Location"] = location
            response = addinfourl(io.BytesIO(body), headers, request.full_url, status)
            response.msg = "synthetic response"
            return response

        https_open = http_open

    def opener(*handlers):
        return build_opener(ProxyHandler({}), *handlers, SyntheticTransport())

    monkeypatch.setattr(mirror, "build_opener", opener, raising=False)
    monkeypatch.setattr(mirror, "open_request", opener(oauth.NoRedirect()).open)
    monkeypatch.setattr(oauth, "open_request", opener(oauth.NoRedirect()).open)
    return routes, requests


def headers(request):
    return {key.lower(): value for key, value in request.header_items()}


def test_large_export_follows_valid_google_307(transport, monkeypatch):
    routes, requests = transport
    api = "https://www.googleapis.com/drive/v3/files/synthetic/export?mimeType=text%2Fmarkdown"
    body = b"# Synthetic document\n\nFull exported content.\n"
    routes[api] = (403, None, b'{"error":{"reason":"exportSizeLimitExceeded"}}')
    routes[EXPORT] = (307, ASSET, b"")
    routes[ASSET] = (200, None, body)
    monkeypatch.setattr(mirror, "get_access_token", lambda: "synthetic-access")

    def metadata(doc_id, fields):
        assert doc_id == "synthetic" and fields == "exportLinks"
        return {"exportLinks": {"text/markdown": EXPORT}}

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    assert mirror.fetch_export_authenticated("synthetic", "text/markdown") == body
    assert [request.full_url for request in requests] == [api, EXPORT, ASSET]
    assert all(request.get_method() == "GET" for request in requests)
    assert headers(requests[1])["authorization"] == "Bearer synthetic-access"
    assert "authorization" not in headers(requests[2])


def test_asset_redirect_removes_credentials_after_crossing_origin(transport):
    routes, requests = transport
    same_origin = "https://docs.google.com/second"
    return_origin = "https://docs.google.com/final"
    routes[EXPORT] = (307, "/second", b"")
    routes[same_origin] = (307, ASSET, b"")
    routes[ASSET] = (307, return_origin, b"")
    routes[return_origin] = (200, None, b"complete")
    with mirror.open_google_asset_get(
        EXPORT,
        headers={
            "Authorization": "Bearer synthetic-access",
            "Cookie": "synthetic-cookie",
        },
        timeout=1,
    ) as response:
        assert response.read() == b"complete"
    assert [request.full_url for request in requests] == [
        EXPORT,
        same_origin,
        ASSET,
        return_origin,
    ]
    for request in requests[:2]:
        assert headers(request)["authorization"] == "Bearer synthetic-access"
        assert headers(request)["cookie"] == "synthetic-cookie"
    for request in requests[2:]:
        assert "authorization" not in headers(request)
        assert "cookie" not in headers(request)


@pytest.mark.parametrize(
    "target",
    [
        "https://outside.invalid/export",
        "http://docs.google.com/export",
        "https://synthetic-user@docs.google.com/export",
        "https://@docs.google.com/export",
        "https://docs.google.com:444/export",
    ],
    ids=["outside-host", "http", "userinfo", "empty-userinfo", "port"],
)
def test_invalid_redirect_is_rejected_before_target_request(transport, target):
    routes, requests = transport
    routes[EXPORT] = (307, ASSET, b"")
    routes[ASSET] = (307, target, b"")
    routes[target] = (200, None, b"must not be requested")
    with pytest.raises(ValueError, match="mirror-google-asset-url-invalid"):
        mirror.open_google_asset_get(EXPORT, headers={}, timeout=1)
    assert [request.full_url for request in requests] == [EXPORT, ASSET]


def test_asset_redirect_loop_uses_standard_request_limit(transport):
    routes, requests = transport
    routes[EXPORT] = (307, EXPORT, b"")
    with pytest.raises(HTTPError):
        mirror.open_google_asset_get(EXPORT, headers={}, timeout=1)
    assert 1 < len(requests) <= 11


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_regular_api_and_oauth_post_still_refuse_redirects(transport, method):
    routes, requests = transport
    url = oauth.TOKEN_ENDPOINT if method == "POST" else mirror.DRIVE_API + "synthetic"
    routes[url] = (307, ASSET, b"")
    if method == "POST":
        with pytest.raises(oauth.OAuthError, match="token-exchange-http-307"):
            oauth.exchange({"refresh_token": "synthetic"})
    else:
        request = Request(url, headers={"Authorization": "Bearer synthetic-access"})
        with pytest.raises(HTTPError, match="redirect-refused"):
            mirror.open_request(request, timeout=1)
    assert [request.full_url for request in requests] == [url]
    assert requests[0].get_method() == method
