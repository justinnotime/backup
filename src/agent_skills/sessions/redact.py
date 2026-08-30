"""Visible, idempotent, self-testing credential redaction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .manifest import RedactionSpec


class RedactionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Pattern:
    name: str
    regex: re.Pattern[str]
    canary: str

    @property
    def marker(self) -> str:
        return f"[REDACTED:{self.name}]"


_BUILTINS = (
    (
        "known-key-prefix",
        r"(?<![A-Za-z0-9_-])(?:gsk-|ghp_|github_pat_|e2b_|sk-)[A-Za-z0-9_./+=-]{12,}",
        "gsk-SYNTHETIC000000CANARY",
    ),
    (
        "credential-assignment",
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}['\"]?",
        "api_key=SYNTHETIC000000CANARY",
    ),
    (
        "bearer-token",
        r"(?i)\bBearer\s+[A-Za-z0-9_./+=-]{12,}",
        "Bearer SYNTHETIC000000CANARY",
    ),
    (
        "jwt",
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        "eyJSYNTHETIC0.SYNTHETIC000.SYNTHETIC000",
    ),
)


class Redactor:
    def __init__(self, patterns: tuple[Pattern, ...], *, required: bool) -> None:
        self.patterns = patterns
        self.required = required

    @classmethod
    def from_spec(cls, spec: RedactionSpec) -> Redactor:
        raw = list(_BUILTINS if spec.builtin_policy == "default" else ())
        raw.extend(
            (item["name"], item["regex"], item["canary"]) for item in spec.patterns
        )
        patterns = []
        names = set()
        for name, expression, canary in raw:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
                raise RedactionError("redaction pattern has an invalid name")
            if name in names:
                raise RedactionError("redaction pattern names must be unique")
            try:
                compiled = re.compile(expression)
            except re.error as exc:
                raise RedactionError(f"redaction pattern {name} is invalid") from exc
            if compiled.search("") is not None:
                raise RedactionError(f"redaction pattern {name} matches empty text")
            patterns.append(Pattern(name, compiled, canary))
            names.add(name)
        redactor = cls(tuple(patterns), required=spec.required)
        redactor.self_test()
        return redactor

    def self_test(self) -> None:
        if self.required and not self.patterns:
            raise RedactionError("required redaction has no patterns")
        for pattern in self.patterns:
            transformed, counts = self.apply(pattern.canary)
            if pattern.canary in transformed or counts.get(pattern.name, 0) < 1:
                raise RedactionError(f"redaction self-test failed for {pattern.name}")
            again, _ = self.apply(transformed)
            if again != transformed:
                raise RedactionError(f"redaction is not idempotent for {pattern.name}")

    def apply(self, text: str) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}
        transformed = text
        for pattern in self.patterns:
            transformed, count = pattern.regex.subn(pattern.marker, transformed)
            if count:
                counts[pattern.name] = count
        return transformed, counts

    def scan(self, text: str) -> tuple[str, ...]:
        return tuple(
            pattern.name for pattern in self.patterns if pattern.regex.search(text)
        )
