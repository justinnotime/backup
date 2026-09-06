"""Explicit Anthropic-compatible provider; no implicit account discovery."""

import json
import os
import time

from .config import ExtractionError


def make_client(settings):
    config = settings.llm
    if not config.get("model") or not config.get("base_url"):
        raise ExtractionError("LLM modes require llm.model and llm.base_url")
    key = os.environ.get(config.get("api_key_env", ""))
    if not key and config.get("credential_file"):
        try:
            value = json.loads(config["credential_file"].read_text(encoding="utf-8"))
            key = value.get(config.get("credential_key", "api_key"))
        except (OSError, ValueError, AttributeError):
            raise ExtractionError("cannot read configured credential") from None
    if not isinstance(key, str) or not key.strip():
        if not config.get("required", True):
            return None
        raise ExtractionError("configured credential is missing")
    from anthropic import Anthropic
    from httpx import Client

    # Never follow a provider redirect with an API key, or discover a proxy
    # from another program's environment. Transport retry ownership is here.
    return Anthropic(
        api_key=key,
        base_url=config["base_url"],
        max_retries=0,
        timeout=config["timeout_seconds"],
        http_client=Client(
            follow_redirects=False, trust_env=False, timeout=config["timeout_seconds"]
        ),
    )


def call_llm(settings, client, system_prompt, user_prompt, max_tokens):
    for attempt in range(settings.llm["max_attempts"]):
        try:
            response = client.messages.create(
                model=settings.llm["model"],
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
            )
            if getattr(response, "stop_reason", None) in {"max_tokens", "refusal"}:
                raise ExtractionError("provider output incomplete")
            text = "\n".join(
                part.text
                for part in response.content
                if getattr(part, "type", "text") == "text"
            ).strip()
            if not text or text.lower().startswith(("<!doctype", "<html")):
                raise ExtractionError("provider returned no usable text")
            usage = {
                key: getattr(response.usage, key, 0)
                for key in ("input_tokens", "output_tokens")
            }
            usage.update(
                cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0),
                cache_write_tokens=getattr(
                    response.usage, "cache_creation_input_tokens", 0
                ),
            )
            return text, usage
        except Exception:
            if attempt + 1 == settings.llm["max_attempts"]:
                # SDK exception strings can include response bodies or credentials.
                raise ExtractionError(
                    "provider request failed or returned incomplete output"
                ) from None
            time.sleep(2**attempt)
