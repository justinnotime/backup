"""Output inventory and pre-publication contract checks."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .manifest import LEGACY_AGENT_MARKDOWN_RULES, Manifest
from .model import PublicationPlan, Session
from .redact import Redactor
from .render import MANAGED_BY, metadata_headers

GIT_CRYPT_MAGIC = b"\x00GITCRYPT\x00"


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    relative_path: str
    digest: str
    identity: tuple[str, str, str] | None
    kind: str | None
    headers: Mapping[str, str]
    title: str
    grandfathered: bool = False
    semantic_digest: str | None = None


@dataclass(frozen=True, slots=True)
class OutputInventory:
    entries: tuple[InventoryEntry, ...]

    def by_path(self) -> dict[str, InventoryEntry]:
        return {entry.relative_path: entry for entry in self.entries}


def _headers(text: str) -> tuple[dict[str, str], str]:
    result: dict[str, str] = {}
    title = ""
    for index, line in enumerate(text.splitlines()[:80]):
        if index == 0 and line.startswith("# "):
            title = line[2:].strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        result[key.strip()] = value.strip()
    return result, title


def _legacy_value(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and "`" in value[1:]:
        return value[1:].split("`", 1)[0]
    return value.strip(" `")


def _semantic_digest(events: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for role, text in events:
        digest.update(role.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


_HISTORY_HEADING = re.compile(
    r"(?m)^### (?:~?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z|unknown) "
    r"(?:—|--|-) (user|assistant|peer-agent)\s*$"
)
_PROMPT_HEADING = re.compile(
    r"(?m)^### (?:~?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z|unknown)\s*$"
)
_OPENCLAW_HEADING = re.compile(
    r"(?m)^## (\U0001f464 User|\U0001f916 Assistant)(?: \(\d{2}:\d{2}\))?\s*$"
)


def _legacy_openclaw(text: str, node: str) -> tuple[dict[str, str], str]:
    """Read the old format only with caller-supplied ownership, never a hostname."""
    header, separator, _body = text.partition("\n---\n")
    if not separator or not header.startswith("# Claw Session "):
        raise AuditError("unrecognized legacy OpenClaw history")
    fields: dict[str, str] = {}
    for line in header.splitlines():
        match = re.fullmatch(r"- \*\*([^*]+):\*\*\s*(.*)", line)
        if match:
            if match.group(1) in fields:
                raise AuditError("duplicate legacy OpenClaw metadata")
            fields[match.group(1)] = _legacy_value(match.group(2))
    session_id = fields.get("Session ID", "")
    if not session_id or any(character in session_id for character in "\r\n\0"):
        raise AuditError("legacy OpenClaw output is missing its session identity")
    if "Host" in fields and fields["Host"] != node:
        raise AuditError("legacy OpenClaw ownership contradicts the configured node")
    fields.update({"Tool": "openclaw", "Host": node, "Session": session_id})
    return fields, session_id


def _legacy_openclaw_digest(text: str) -> str | None:
    matches = list(_OPENCLAW_HEADING.finditer(text))
    events = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            events.append(
                ("user" if match.group(1).endswith("User") else "assistant", body)
            )
    return _semantic_digest(events) if events else None


def _legacy_semantic_digest(text: str, kind: str) -> str | None:
    headings = _HISTORY_HEADING if kind == "history" else _PROMPT_HEADING
    matches = list(headings.finditer(text))
    if not matches:
        return None
    events: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if kind == "prompts":
            body = re.sub(r"\n+---\s*$", "", body).strip()
            role = "user"
        else:
            role = match.group(1)
        if role in {"user", "peer-agent"}:
            lines = body.splitlines()
            if lines and all(
                not line or line == ">" or line.startswith("> ") for line in lines
            ):
                body = "\n".join(
                    "" if line == ">" else line.removeprefix("> ") for line in lines
                ).strip()
        if body:
            events.append((role, body))
    return _semantic_digest(events) if events else None


def semantic_digest_for_session(
    session: Session, kind: str, redactor: Redactor | None = None
) -> str:
    roles = {"user"} if kind == "prompts" else {"user", "assistant", "peer-agent"}
    return _semantic_digest(
        [
            (
                event.role,
                redactor.apply(event.text)[0] if redactor is not None else event.text,
            )
            for event in session.events
            if event.role in roles
        ]
    )


_LEGACY_TOOL_NAMES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
    "dsh": "dsh",
    "cursor": "cursor",
    "openclaw": "openclaw",
}


def entry_from_content(
    relative_path: str,
    content: bytes,
    *,
    compatibility_hashes: frozenset[str] = frozenset(),
    compatibility_rule: str = "none",
    legacy_kind: str | None = None,
    legacy_harness: str | None = None,
    legacy_identity: tuple[str, str, str] | None = None,
    legacy_openclaw_node: str | None = None,
    preserve_static: bool = False,
) -> InventoryEntry:
    digest = hashlib.sha256(content).hexdigest()
    if content.startswith(GIT_CRYPT_MAGIC):
        raise AuditError("output contains a git-crypt ciphertext checkout")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        if digest in compatibility_hashes:
            return InventoryEntry(relative_path, digest, None, None, {}, "", True)
        raise AuditError("output is not UTF-8 and is not grandfathered") from exc
    headers, title = _headers(text)
    if preserve_static:
        if (
            headers.get("Managed-By") == MANAGED_BY
            or _HISTORY_HEADING.search(text)
            or _OPENCLAW_HEADING.search(text)
            or re.search(r"(?m)^- (?:\*\*)?Session(?: ID)?:", text)
            or text.startswith("# Claw Session ")
        ):
            raise AuditError("configured static output contains session records")
        return InventoryEntry(
            relative_path, digest, None, "static", headers, title, True
        )
    if headers.get("Managed-By") != MANAGED_BY:
        if compatibility_rule in LEGACY_AGENT_MARKDOWN_RULES and legacy_kind:
            if PurePosixPath(relative_path).name in {"README.md", "PROVENANCE.md"}:
                return InventoryEntry(
                    relative_path, digest, None, "static", headers, title, True
                )
            canonical = dict(headers)
            identity = legacy_identity
            harness = legacy_harness
            if (
                legacy_kind == "history"
                and harness == "openclaw"
                and text.startswith("# Claw Session ")
            ):
                if legacy_openclaw_node is None:
                    raise AuditError(
                        "legacy OpenClaw history requires configured ownership"
                    )
                if not re.fullmatch(
                    r"session-(?:\d{4}-\d{2}-\d{2}|unknown-date)_[^/]+\.md",
                    PurePosixPath(relative_path).name,
                ):
                    raise AuditError("unrecognized legacy OpenClaw filename")
                canonical, session_id = _legacy_openclaw(text, legacy_openclaw_node)
                semantic = _legacy_openclaw_digest(text)
                if semantic is None:
                    raise AuditError(
                        "legacy OpenClaw history contains no recognized events"
                    )
                return InventoryEntry(
                    relative_path,
                    digest,
                    (harness, legacy_openclaw_node, session_id),
                    legacy_kind,
                    canonical,
                    title,
                    True,
                    semantic,
                )
            if legacy_kind == "history":
                tool_value = _legacy_value(headers.get("Tool", ""))
                if tool_value:
                    harness = _LEGACY_TOOL_NAMES.get(tool_value)
                host = _legacy_value(headers.get("Host", ""))
                session_id = _legacy_value(headers.get("Session ID", ""))
                if not harness or not host or not session_id:
                    raise AuditError(
                        "legacy history output is missing identity headers"
                    )
                identity = (harness, host, session_id)
            elif legacy_kind == "prompts":
                tool_value = _legacy_value(headers.get("Tool", ""))
                harness = _LEGACY_TOOL_NAMES.get(tool_value)
                host = _legacy_value(headers.get("Host", ""))
                if not harness or not host:
                    raise AuditError(
                        "legacy prompt output is missing ownership headers"
                    )
            else:
                raise AuditError("invalid legacy output kind")
            if harness:
                canonical["Tool"] = harness
            if identity is not None:
                canonical["Host"] = identity[1]
                canonical["Session"] = identity[2]
            elif headers.get("Host"):
                canonical["Host"] = _legacy_value(headers["Host"])
            if headers.get("Project"):
                canonical["Project"] = _legacy_value(headers["Project"])
            return InventoryEntry(
                relative_path,
                digest,
                identity,
                legacy_kind,
                canonical,
                title,
                True,
                _legacy_semantic_digest(text, legacy_kind),
            )
        if digest in compatibility_hashes:
            return InventoryEntry(
                relative_path, digest, None, None, headers, title, True
            )
        raise AuditError(
            "unmanaged output is not covered by an unchanged-file compatibility rule"
        )
    kind = headers.get("View")
    identity = None
    if kind in {"history", "prompts"}:
        required = ("Tool", "Host", "Session")
        if any(not headers.get(key) for key in required):
            raise AuditError("managed output is missing identity headers")
        session_component = headers["Session"]
        day = headers.get("Day")
        if day:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise AuditError("managed output has an invalid Day header")
            session_component = f"{session_component}@{day}"
        identity = (headers["Tool"], headers["Host"], session_component)
    return InventoryEntry(relative_path, digest, identity, kind, headers, title)


def _safe_output_directory(root: Path, relative: str) -> Path:
    lexical_root = Path(os.path.abspath(root))
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise AuditError("output repository root is missing or unreadable") from exc
    target = lexical_root / relative
    if target.exists() or target.is_symlink():
        resolved = target.resolve(strict=True)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise AuditError(
                "output directory resolves outside its repository root"
            ) from exc
        if target.is_symlink():
            raise AuditError("output directories must not be symbolic links")
    return target


def scan_inventory(manifest: Manifest) -> OutputInventory:
    hashes = frozenset(manifest.output.compatibility_sha256)
    entries = []
    seen: set[str] = set()
    explicit_routes: dict[str, set[str]] = {}
    for harness, directory in manifest.output.history_directory_by_harness.items():
        explicit_routes.setdefault(directory, set()).add(harness)
    for directory in manifest.output.history_directories():
        target = _safe_output_directory(manifest.output.repository_root, directory)
        if not target.exists():
            continue
        if any(path.is_symlink() for path in target.rglob("*")):
            raise AuditError("output inventory contains a symbolic link")
        for path in sorted(target.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                raise AuditError(
                    "output inventory contains a symbolic link or non-file"
                )
            relative = path.relative_to(manifest.output.repository_root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            routed = explicit_routes.get(directory, set())
            legacy_harness = next(iter(routed)) if len(routed) == 1 else None
            entries.append(
                entry_from_content(
                    relative,
                    path.read_bytes(),
                    compatibility_hashes=hashes,
                    compatibility_rule=manifest.output.compatibility_rule,
                    legacy_kind="history",
                    legacy_harness=legacy_harness,
                    legacy_openclaw_node=manifest.output.legacy_openclaw_node,
                    preserve_static=manifest.output.preserve_static(relative),
                )
            )
    history_by_name: dict[tuple[str, str, str], InventoryEntry] = {}
    for entry in entries:
        if entry.kind != "history" or entry.identity is None:
            continue
        history_by_name[
            (
                entry.identity[0],
                entry.identity[1],
                PurePosixPath(entry.relative_path).name,
            )
        ] = entry

    directory = manifest.output.prompt_directory
    target = (
        _safe_output_directory(manifest.output.repository_root, directory)
        if directory is not None
        else None
    )
    if target is not None and target.exists():
        if any(path.is_symlink() for path in target.rglob("*")):
            raise AuditError("output inventory contains a symbolic link")
        for path in sorted(target.rglob("*.md")):
            if path.is_symlink() or not path.is_file():
                raise AuditError(
                    "output inventory contains a symbolic link or non-file"
                )
            relative = path.relative_to(manifest.output.repository_root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            content = path.read_bytes()
            probe_headers, _title = _headers(content.decode("utf-8", errors="replace"))
            harness = _LEGACY_TOOL_NAMES.get(
                _legacy_value(probe_headers.get("Tool", ""))
            )
            host = _legacy_value(probe_headers.get("Host", ""))
            name = path.name
            names = [name]
            if harness:
                tool = "claude" if harness == "claude-code" else harness
                names.append(re.sub(rf"-{re.escape(tool)}(?:-\d+)?(?=\.md$)", "", name))
            identity = None
            for candidate_name in names:
                match = history_by_name.get((harness or "", host, candidate_name))
                if match is not None:
                    identity = match.identity
                    break
            entries.append(
                entry_from_content(
                    relative,
                    content,
                    compatibility_hashes=hashes,
                    compatibility_rule=manifest.output.compatibility_rule,
                    legacy_kind="prompts",
                    legacy_harness=harness,
                    legacy_identity=identity,
                    preserve_static=manifest.output.preserve_static(relative),
                )
            )
    identities: dict[tuple[tuple[str, str, str], str], list[InventoryEntry]] = {}
    for entry in entries:
        if entry.identity is None or entry.kind not in {"history", "prompts"}:
            continue
        key = (entry.identity, entry.kind)
        identities.setdefault(key, []).append(entry)
    for (identity, kind), duplicates in identities.items():
        if len(duplicates) == 1:
            continue
        raise AuditError("output inventory contains duplicate session identities")
    return OutputInventory(tuple(entries))


def _assert_owned(manifest: Manifest, relative_path: str) -> None:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise AuditError("publication path escapes the output root")
    rendered = str(path)
    if not any(
        rendered == root or rendered.startswith(root + "/")
        for root in manifest.publisher.owned_subtrees
    ):
        raise AuditError("publication path is outside configured owned subtrees")


def audit_plan(
    manifest: Manifest,
    plan: PublicationPlan,
    sessions: tuple[Session, ...],
    redactor: Redactor,
) -> None:
    expected = {session.identity: session for session in sessions}
    paths: set[str] = set()
    for planned in plan.writes:
        _assert_owned(manifest, planned.relative_path)
        if planned.relative_path in paths:
            raise AuditError("publication plan writes one path more than once")
        paths.add(planned.relative_path)
        if planned.content.startswith(GIT_CRYPT_MAGIC):
            raise AuditError("publication plan contains ciphertext")
        try:
            text = planned.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuditError("new output must be UTF-8") from exc
        hits = redactor.scan(text)
        if hits:
            raise AuditError(
                "pre-publication scan found unredacted credential patterns"
            )
        headers, _title = _headers(text)
        if headers.get("Managed-By") != MANAGED_BY:
            raise AuditError("new output is missing the shared ownership marker")
        if planned.identity is None:
            if planned.kind != "index" or headers.get("View") != "index":
                raise AuditError("non-session output must be a managed index")
            continue
        session = expected.get(planned.identity)
        if session is None:
            raise AuditError("publication plan references an unknown session identity")
        expected_view = "history" if planned.kind == "history" else "prompts"
        if headers.get("View") != expected_view:
            raise AuditError("new output has an invalid View header")
        required = {
            "Schema": session.schema_version,
            "Tool": session.harness,
            "Host": session.node_label,
            "Session": session.session_id,
            "Source": session.source_ref,
            "Project": session.project,
        }
        required.update(manifest.output.encryption_attributes)
        required.update(
            {
                key: redactor.apply(value)[0]
                for key, value in metadata_headers(session, manifest.output).items()
            }
        )
        for key, value in required.items():
            if headers.get(key) != value:
                raise AuditError(f"new output has an invalid {key} header")
        if session.day is not None:
            if headers.get("Day") != session.day:
                raise AuditError("new output has an invalid Day header")
        elif "Day" in headers:
            raise AuditError("new output has an unexpected Day header")
        if Path(headers["Source"]).is_absolute():
            raise AuditError("new output exposes an absolute source path")
    for removal in plan.removals:
        _assert_owned(manifest, removal.relative_path)
