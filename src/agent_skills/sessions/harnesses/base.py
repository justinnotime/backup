"""Small decoder interface and registry."""

from __future__ import annotations

from typing import Protocol

from ..model import DecodeBatch, Harness, SourceSnapshot


class Decoder(Protocol):
    harness: Harness

    def capabilities(self) -> tuple[str, ...]: ...

    def decode(self, snapshot: SourceSnapshot) -> DecodeBatch: ...


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
