"""Output inventory and pre-publication contract checks."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .manifest import Manifest
from .model import PublicationPlan, Session
from .redact import Redactor
from .render import MANAGED_BY

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


def entry_from_content(
    relative_path: str,
    content: bytes,
    *,
    compatibility_hashes: frozenset[str] = frozenset(),
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
    if headers.get("Managed-By") != MANAGED_BY:
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
        identity = (headers["Tool"], headers["Host"], headers["Session"])
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
    for directory in (
        manifest.output.history_directory,
        manifest.output.prompt_directory,
    ):
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
            entries.append(
                entry_from_content(
                    relative, path.read_bytes(), compatibility_hashes=hashes
                )
            )
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
        for key, value in required.items():
            if headers.get(key) != value:
                raise AuditError(f"new output has an invalid {key} header")
        if Path(headers["Source"]).is_absolute():
            raise AuditError("new output exposes an absolute source path")
    for removal in plan.removals:
        _assert_owned(manifest, removal.relative_path)
