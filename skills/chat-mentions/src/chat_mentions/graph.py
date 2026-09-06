"""Minimal read-only Microsoft Graph transport owned by this package."""

import base64
import json
from urllib.parse import quote, urlsplit

from .config import atomic_write
from .events import parse_ts

BASE = "https://graph.microsoft.com/v1.0"


class Graph:
    def __init__(self, settings):
        self.settings = settings
        self.token = None

    def authenticate(self):
        import msal

        path = self.settings["read_token_file"]
        cache = msal.SerializableTokenCache()
        if path.exists():
            cache.deserialize(path.read_text())
        client = msal.PublicClientApplication(
            self.settings["client_id"],
            token_cache=cache,
            authority=self.settings.get(
                "authority", "https://login.microsoftonline.com/organizations"
            ),
        )
        accounts = client.get_accounts(username=self.settings.get("login_hint"))
        if len(accounts) != 1:
            raise ValueError("read-cache-must-select-one-account")
        result = client.acquire_token_silent(["Chat.Read"], account=accounts[0])
        if cache.has_state_changed:
            atomic_write(path, cache.serialize())
        if not result or not result.get("access_token"):
            raise ValueError(
                "read-token-unavailable; configure authentication separately"
            )
        self.token = result["access_token"]

    def get(self, url, params=None):
        import requests

        url = BASE + url if url.startswith("/") else url
        base, target = urlsplit(BASE), urlsplit(url)
        if (target.scheme, target.netloc) != (
            base.scheme,
            base.netloc,
        ) or not target.path.startswith(base.path + "/"):
            raise ValueError("graph-url-outside-service")
        try:
            response = requests.get(
                url,
                params=params,
                headers={"Authorization": "Bearer " + self.token},
                timeout=60,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ValueError("graph-read-failed") from None
        if response.status_code != 200:
            raise ValueError("graph-http-" + str(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise ValueError("graph-invalid-response") from None
        if not isinstance(data, dict):
            raise ValueError("graph-invalid-response")
        return data

    def own_id(self):
        if self.settings.get("own_user_id"):
            return self.settings["own_user_id"]
        try:
            payload = self.token.split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            if isinstance(claims.get("oid"), str) and claims["oid"]:
                return claims["oid"]
        except (ValueError, IndexError, AttributeError):
            pass
        identifier = self.get("/me").get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("own-account-id-unavailable")
        return identifier

    def pages(self, url, params, limit):
        seen = set()
        for _ in range(limit):
            if url in seen:
                raise ValueError("graph-pagination-cycle")
            seen.add(url)
            data = self.get(url, params)
            values = data.get("value")
            if not isinstance(values, list) or any(
                not isinstance(x, dict) for x in values
            ):
                raise ValueError("graph-invalid-collection")
            yield values
            url, params = data.get("@odata.nextLink"), None
            if not url:
                return
            if not isinstance(url, str):
                raise ValueError("graph-invalid-next-link")
        raise ValueError("graph-page-limit-reached; increase the configured limit")

    def active_chats(self, since):
        result = []
        params = {
            "$top": 50,
            "$expand": "members,lastMessagePreview",
            "$orderby": "lastMessagePreview/createdDateTime desc",
        }
        for page in self.pages("/me/chats", params, self.settings["list_page_limit"]):
            for chat in page:
                stamp = parse_ts(
                    (chat.get("lastMessagePreview") or {}).get("createdDateTime")
                )
                if stamp is None:
                    continue
                if stamp <= since:
                    return result
                result.append(chat)
        return result

    def messages(self, chat_id, since):
        result = []
        params = {"$top": 50, "$orderby": "createdDateTime desc"}
        for page in self.pages(
            "/chats/" + quote(chat_id, safe="") + "/messages",
            params,
            self.settings["message_page_limit"],
        ):
            for message in page:
                stamp = parse_ts(message.get("createdDateTime"))
                if stamp is None:
                    raise ValueError("message-timestamp-invalid")
                if stamp < since:
                    return result
                result.append(message)
        return result


def chat_label(chat):
    if chat.get("topic"):
        return str(chat["topic"])
    return " & ".join(
        str(x.get("displayName") or "?") for x in chat.get("members", [])
    ) or str(chat["id"])
