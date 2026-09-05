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
    keep_template: str | None = None

    @property
    def marker(self) -> str:
        return f"[REDACTED:{self.name}]"

    def replacement(self, match: re.Match[str]) -> str:
        if self.keep_template is None:
            return self.marker
        groups = [match.group(0), *match.groups()]
        return self.keep_template.format(*groups) + self.marker


_VALUE = r"[A-Za-z0-9_\-./+=]{12,}"


_BUILTINS = (
    (
        "private-key-block",
        (
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nSYNTHETICCANARY\n"
            "-----END PRIVATE KEY-----"
        ),
        re.DOTALL,
        None,
    ),
    (
        "known-key-prefix",
        (
            r"(?<![A-Za-z0-9_-])"
            r"(?:sk-|gsk-|e2b_|gh_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-)"
            r"[A-Za-z0-9_-]{20,}"
        ),
        "gsk-SYNTHETIC000000CANARY",
        0,
        None,
    ),
    (
        "slack-token",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        "xoxb-000000-SYNTHETICCANARY",
        0,
        None,
    ),
    (
        "aws-access-key-id",
        r"\bAKIA[0-9A-Z]{16}\b",
        "AKIASYNTHETIC0000000",
        0,
        None,
    ),
    (
        "google-api-key",
        r"\bAIza[0-9A-Za-z_-]{20,}",
        "AIzaSYNTHETIC000000CANARY",
        0,
        None,
    ),
    (
        "google-oauth-token",
        r"\bya29\.[0-9A-Za-z_-]{20,}",
        "ya29.SYNTHETIC000000CANARY",
        0,
        None,
    ),
    (
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}",
        "eyJSYNTHETIC0.eyJSYNTHETIC0.SYNTHETIC",
        0,
        None,
    ),
    (
        "x-access-token-url",
        r"(x-access-token:)[^@\s/\[]{4,}(?=@)",
        "https://x-access-token:SYNTHETICCANARY@example.invalid/repository.git",
        0,
        "{1}",
    ),
    (
        "url-userinfo-password",
        r"(://[^/\s:@\[]{1,64}:)[^@\s/\[]{4,}(?=@)",
        "https://service:SYNTHETICCANARY@example.invalid/path",
        0,
        "{1}",
    ),
    (
        "bearer",
        (
            r"\b(bearer\s+|authorization:\s*token\s+)"
            r"[A-Za-z0-9_\-./+=]{16,}"
        ),
        "Authorization: Bearer SYNTHETIC000000CANARY",
        re.IGNORECASE,
        "{1}",
    ),
    (
        "named-secret-field",
        r"\b((?:[A-Za-z0-9]+[_-])*"
        r"(?:api[_-]?key|apikey|access[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|client[_-]?secret|auth[_-]?token|token|"
        r"secret|password|passwd))"
        r"(['\"]?\s*[:=]\s*['\"]?)"
        + _VALUE,
        "API_KEY=SYNTHETIC000000CANARY",
        re.IGNORECASE,
        "{1}{2}",
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
            (item["name"], item["regex"], item["canary"], 0, None)
            for item in spec.patterns
        )
        patterns = []
        names = set()
        for name, expression, canary, flags, keep_template in raw:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
                raise RedactionError("redaction pattern has an invalid name")
            if name in names:
                raise RedactionError("redaction pattern names must be unique")
            try:
                compiled = re.compile(expression, flags)
            except re.error as exc:
                raise RedactionError(f"redaction pattern {name} is invalid") from exc
            if compiled.search("") is not None:
                raise RedactionError(f"redaction pattern {name} matches empty text")
            patterns.append(Pattern(name, compiled, canary, keep_template))
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
            transformed, count = pattern.regex.subn(pattern.replacement, transformed)
            if count:
                counts[pattern.name] = count
        return transformed, counts

    def scan(self, text: str) -> tuple[str, ...]:
        return tuple(
            pattern.name for pattern in self.patterns if pattern.regex.search(text)
        )
