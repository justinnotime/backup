from __future__ import annotations

import re
from collections.abc import Iterable

DEFAULT_REPO = ""
_REPO = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
_CANONICAL_RE = re.compile(f"^({_REPO})#([1-9]\\d*)$")
_GITHUB_LINK_RE = re.compile(
    f"https?://github\\.com/({_REPO})/(?:issues|pull)/([1-9]\\d*)(?![A-Za-z0-9_.-])", re.IGNORECASE
)
_FULL_REF_TOKEN_RE = re.compile(f"(?<![A-Za-z0-9_.-])({_REPO}#[1-9]\\d*)(?!\\d)", re.IGNORECASE)
_SHORT_REF_TOKEN_RE = re.compile(
    "(?<![A-Za-z0-9_.\\-/])([A-Za-z0-9_.-]+#[1-9]\\d*)(?!\\d)", re.IGNORECASE
)
_BRACKETED_REF_TOKEN_RE = re.compile(
    f"\\[((?:{_REPO}|[A-Za-z0-9_.-]+)#[1-9]\\d*)\\]", re.IGNORECASE
)


def canonical(repo: str, number: object) -> str:
    normalized_repo = str(repo).strip().strip("/").lower()
    if not re.fullmatch(_REPO, normalized_repo):
        raise ValueError(f"invalid GitHub repository: {repo!r}")
    try:
        normalized_number = int(str(number).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid issue/PR number: {number!r}") from exc
    if normalized_number <= 0:
        raise ValueError(f"invalid issue/PR number: {number!r}")
    return f"{normalized_repo}#{normalized_number}"


def split_ref(ref: str) -> tuple[str, int]:
    match = _CANONICAL_RE.fullmatch(str(ref).strip())
    if not match:
        raise ValueError(f"invalid canonical issue/PR identity: {ref!r}")
    return (match.group(1).lower(), int(match.group(2)))


def sort_refs(refs: Iterable[str]) -> list[str]:
    normalized = {canonical(*split_ref(ref)) for ref in refs}
    return sorted(normalized, key=split_ref)


def repo_and_number_from_url(url: str) -> tuple[str, int] | None:
    match = _GITHUB_LINK_RE.search(str(url))
    if not match:
        return None
    ref = canonical(match.group(1), match.group(2))
    return split_ref(ref)


def summary_link_refs(text: str) -> list[str]:
    return [canonical(match.group(1), match.group(2)) for match in _GITHUB_LINK_RE.finditer(text)]


def normalize_explicit_ref_token(token: str) -> str:
    name, number = str(token).rsplit("#", 1)
    return f"{name.lower()}#{int(number)}"


def allowed_ref_tokens(refs: Iterable[str]) -> set[str]:
    allowed = set()
    for ref in refs:
        repo, number = split_ref(ref)
        allowed.add(f"{repo}#{number}")
        allowed.add(f"{repo.rsplit('/', 1)[-1]}#{number}")
    return allowed


def explicit_ref_tokens(text: str) -> list[str]:
    matches = []
    for pattern in (_FULL_REF_TOKEN_RE, _SHORT_REF_TOKEN_RE):
        for match in pattern.finditer(text):
            matches.append((match.start(), normalize_explicit_ref_token(match.group(1))))
    return [token for _start, token in sorted(matches)]


def sanitize_explicit_ref_tokens(text: str, refs: Iterable[str], replacement: str) -> str:
    allowed = allowed_ref_tokens(refs)

    def replace(match: re.Match[str]) -> str:
        token = normalize_explicit_ref_token(match.group(1))
        return match.group(0) if token in allowed else replacement

    text = _BRACKETED_REF_TOKEN_RE.sub(replace, text)
    text = _FULL_REF_TOKEN_RE.sub(replace, text)
    return _SHORT_REF_TOKEN_RE.sub(replace, text)


def facts_touched_refs(facts: dict) -> list[str]:
    touched = facts.get("gh_touched_today", {})
    refs: list[str] = []
    if isinstance(touched, dict):
        items = touched.items()
    elif isinstance(touched, list):
        items = ((None, item) for item in touched)
    else:
        items = ()
    for key, value in items:
        try:
            if isinstance(key, str) and _CANONICAL_RE.fullmatch(key.strip()):
                refs.append(canonical(*split_ref(key)))
                continue
            if isinstance(value, dict) and value.get("repo") and (value.get("number") is not None):
                refs.append(canonical(value["repo"], value["number"]))
                continue
            if isinstance(value, dict) and value.get("number") is not None:
                refs.append(canonical(DEFAULT_REPO, value["number"]))
                continue
            if key is not None:
                refs.append(canonical(DEFAULT_REPO, key))
        except ValueError:
            continue
    return sort_refs(refs)


def required_refs(facts: dict) -> list[str]:
    return facts_touched_refs(facts)
