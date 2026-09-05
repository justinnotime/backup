"""Harness decoder registry."""

from .base import Decoder, decoder_for, register_decoder
from .claude import DECODER as CLAUDE_DECODER
from .claude import ClaudeDecoder
from .codex import DECODER as CODEX_DECODER
from .codex import CodexDecoder
from .cursor import DECODER as CURSOR_DECODER
from .cursor import CursorDecoder
from .dsh import DECODER as DSH_DECODER
from .dsh import DshDecoder
from .openclaw import DECODER as OPENCLAW_DECODER
from .openclaw import OpenClawDecoder
from .opencode import DECODER as OPENCODE_DECODER
from .opencode import OpenCodeDecoder

for _decoder in (
    CLAUDE_DECODER,
    CODEX_DECODER,
    OPENCODE_DECODER,
    DSH_DECODER,
    CURSOR_DECODER,
    OPENCLAW_DECODER,
):
    register_decoder(_decoder)

__all__ = [
    "ClaudeDecoder",
    "CodexDecoder",
    "CursorDecoder",
    "Decoder",
    "DshDecoder",
    "OpenClawDecoder",
    "OpenCodeDecoder",
    "decoder_for",
    "register_decoder",
]
