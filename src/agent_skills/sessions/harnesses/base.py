"""Small decoder interface and registry."""

from __future__ import annotations

import json
from typing import Protocol

from ..model import DecodeBatch, Harness, SourceSnapshot


class Decoder(Protocol):
    harness: Harness

    def capabilities(self) -> tuple[str, ...]: ...

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch: ...


def jsonl_lines(payload: bytes) -> list[bytes]:
    """Split JSONL bytes into the lines that were complete when they were read.

    Live harness logs are append-only, so a byte snapshot may end inside the
    line that is still being written. An unterminated final line that does not
    parse is that in-progress line: it is dropped and read whole by the next
    run. An unterminated final line that does parse is a finished record.
    """
    lines = payload.splitlines()
    if lines and not payload.endswith((b"\n", b"\r")):
        try:
            json.loads(lines[-1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            lines.pop()
    return lines


_DECODERS: dict[str, Decoder] = {}


def register_decoder(decoder: Decoder) -> Decoder:
    if decoder.harness in _DECODERS:
        raise ValueError(f"decoder already registered: {decoder.harness}")
    _DECODERS[decoder.harness] = decoder
    return decoder


def decoder_for(harness: str) -> Decoder:
    try:
        return _DECODERS[harness]
    except KeyError as exc:
        raise ValueError(f"no decoder registered for harness: {harness}") from exc
