"""Strict JSON manifest parsing for session extraction schema v1."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .model import MANIFEST_SCHEMA_VERSION, SUPPORTED_HARNESSES


class ManifestError(ValueError):
    """Configuration is absent, ambiguous, or unsafe."""


_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOP_KEYS = {
    "schema_version",
    "node_label",
    "sources",
    "ownership",
    "event_policy",
    "project_policy",
    "output",
    "transforms",
    "cleanup",
    "indexes",
    "publisher",
    "gates",
}
_SOURCE_KEYS = {
    "id",
    "enabled",
    "harness",
    "path",
    "authority",
    "required",
    "output_node",
    "root_policy",
    "snapshot",
    "discovery",
    "decoder",
    "allow_empty",
}
_NATIVE_DEFAULTS = {
    "claude-code": ".claude/projects",
    "codex": ".codex/sessions",
    "opencode": ".local/share/opencode/opencode.db",
    "dsh": ".dsh/sessions",
    "cursor": ".cursor/projects",
    "openclaw": ".openclaw/agents/main/sessions",
}
_COMMON_DECODER_KEYS = {"session_id", "project_hint"}
_HARNESS_DECODER_KEYS = {
    "claude-code": _COMMON_DECODER_KEYS
    | {
        "conversation_kind",
        "conversational_subagent_min_user_events",
    },
    "codex": _COMMON_DECODER_KEYS,
    "opencode": {"minimum_user_events", "excluded_cwd_prefixes"},
    "dsh": {"compression", "allow_torn_current_frame"},
    "cursor": _COMMON_DECODER_KEYS | {"minimum_user_events"},
    "openclaw": _COMMON_DECODER_KEYS
    | {
        "minimum_user_events",
        "is_cron_session",
        "operational_notification_prefixes",
        "channel_forward_prefixes",
        "exclude_operational_notifications",
        "retain_channel_forwarded",
        "exclude_cron_sessions",
        "include_channel_metadata",
        "include_session_metadata",
        "session_metadata_fields",
        "channel",
    },
}
_RESERVED_OUTPUT_HEADERS = {
    "Managed-By",
    "Schema",
    "View",
    "Tool",
    "Host",
    "Session",
    "Source",
    "Project",
    "Cwd",
    "Started",
    "Ended",
}


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    return value


def _required(mapping: Mapping[str, Any], keys: set[str], name: str) -> None:
    missing = sorted(keys - mapping.keys())
    if missing:
        raise ManifestError(f"{name} is missing required fields: {', '.join(missing)}")


def _only(mapping: Mapping[str, Any], keys: set[str], name: str) -> None:
    extra = sorted(mapping.keys() - keys)
    if extra:
        raise ManifestError(f"{name} has unknown fields: {', '.join(extra)}")


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ManifestError(f"{name} must be an opaque label of at most 64 characters")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{name} must be boolean")
    return value


def _enum(value: Any, choices: set[str], name: str) -> str:
    if value not in choices:
        raise ManifestError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return str(value)


def _string_list(value: Any, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ManifestError(f"{name} must be an array of strings")
    if not allow_empty and not value:
        raise ManifestError(f"{name} must not be empty")
    return tuple(value)


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ManifestError(f"{name} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ManifestError(f"{name} must be an absolute path")
    return path


def _relative(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ManifestError(f"{name} must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path == PurePosixPath(".")
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or value.startswith("./")
    ):
        raise ManifestError(f"{name} must stay below the configured output root")
    return str(path)


@dataclass(frozen=True, slots=True)
class RootPolicy:
    allowed_lexical_roots: tuple[Path, ...]
    allowed_resolved_roots: tuple[Path, ...]
    forbidden_components: tuple[str, ...]
    required_suffixes: tuple[str, ...]
    candidate_beneath_root: bool
    symlinks: str


@dataclass(frozen=True, slots=True)
class Discovery:
    mode: str
    patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    source_id: str
    enabled: bool
    harness: str
    path: Path
    path_kind: str
    authority: str
    required: bool
    output_node: str
    root_policy: RootPolicy
    snapshot: str
    discovery: Discovery
    decoder: Mapping[str, Any]
    allow_empty: bool


@dataclass(frozen=True, slots=True)
class EventPolicy:
    synthetic_prefixes: tuple[str, ...]
    peer_agent_prefixes: tuple[str, ...]
    peer_agent_exact: tuple[str, ...]
    min_direct_user_events: int
    min_user_chars: int
    retain_conversational_subagents: bool


@dataclass(frozen=True, slots=True)
class ProjectPolicy:
    mode: str
    unknown: str
    allowlist: tuple[str, ...]
    denylist: tuple[str, ...]
    aliases: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class OutputSpec:
    repository_root: Path
    history_directory: str
    prompt_directory: str
    layout: str
    migration: str
    prompt_max_chars: int
    prompt_code_block_max_chars: int
    encryption_attributes: Mapping[str, str]
    compatibility_rule: str
    compatibility_sha256: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RedactionSpec:
    required: bool
    builtin_policy: str
    patterns: tuple[Mapping[str, str], ...]


@dataclass(frozen=True, slots=True)
class CleanupSpec:
    scope: str


@dataclass(frozen=True, slots=True)
class PublisherSpec:
    strategy: str
    owned_subtrees: tuple[str, ...]
    encryption: str
    key_link_source: Path | None
    key_link_target: str | None
    ciphertext_checkout: str


@dataclass(frozen=True, slots=True)
class GateSpec:
    source_failure: str
    require_redaction_self_test: bool
    require_output_audit: bool
    require_reconciliation: bool
    require_prepublication_scan: bool


@dataclass(frozen=True, slots=True)
class Manifest:
    schema_version: str
    node_label: str
    sources: tuple[SourceSpec, ...]
    ownership_mode: str
    event_policy: EventPolicy
    project_policy: ProjectPolicy
    output: OutputSpec
    redaction: RedactionSpec
    cleanup: CleanupSpec
    indexes_mode: str
    publisher: PublisherSpec
    gates: GateSpec


def _path_value(
    path_cfg: dict[str, Any], harness: str, environ: Mapping[str, str]
) -> tuple[str, Path]:
    _required(path_cfg, {"kind"}, "source.path")
    _only(path_cfg, {"kind", "value"}, "source.path")
    kind = _enum(path_cfg["kind"], {"explicit", "native-default"}, "source.path.kind")
    if kind == "explicit":
        if "value" not in path_cfg:
            raise ManifestError("source.path.value is required for an explicit path")
        return kind, _absolute(path_cfg["value"], "source.path.value")
    if "value" in path_cfg:
        raise ManifestError("native-default paths must not override their value")
    home = environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise ManifestError("HOME must be supplied explicitly for native-default paths")
    return kind, Path(home) / _NATIVE_DEFAULTS[harness]


def _root_policy(value: Any) -> RootPolicy:
    cfg = _mapping(value, "source.root_policy")
    keys = {
        "allowed_lexical_roots",
        "allowed_resolved_roots",
        "forbidden_components",
        "required_suffixes",
        "candidate_beneath_root",
        "symlinks",
    }
    _required(cfg, keys, "source.root_policy")
    _only(cfg, keys, "source.root_policy")
    lexical = tuple(
        _absolute(item, "source.root_policy.allowed_lexical_roots[]")
        for item in _string_list(
            cfg["allowed_lexical_roots"], "allowed_lexical_roots", allow_empty=False
        )
    )
    resolved = tuple(
        _absolute(item, "source.root_policy.allowed_resolved_roots[]")
        for item in _string_list(
            cfg["allowed_resolved_roots"], "allowed_resolved_roots", allow_empty=False
        )
    )
    forbidden = _string_list(cfg["forbidden_components"], "forbidden_components")
    if any(not item or "/" in item for item in forbidden):
        raise ManifestError("forbidden_components entries must be path components")
    suffixes = _string_list(cfg["required_suffixes"], "required_suffixes")
    if any(not item for item in suffixes):
        raise ManifestError("required_suffixes entries must not be empty")
    return RootPolicy(
        lexical,
        resolved,
        forbidden,
        suffixes,
        _bool(cfg["candidate_beneath_root"], "candidate_beneath_root"),
        _enum(cfg["symlinks"], {"reject", "confined"}, "source.root_policy.symlinks"),
    )


def _decoder_options(harness: str, value: Any) -> Mapping[str, Any]:
    cfg = _mapping(value, "source.decoder")
    allowed = _HARNESS_DECODER_KEYS[harness]
    _only(cfg, allowed, f"source.decoder for {harness}")
    for key in _COMMON_DECODER_KEYS & cfg.keys():
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ManifestError(f"source.decoder.{key} must be a non-empty string")
    for key in {
        "minimum_user_events",
        "conversational_subagent_min_user_events",
    } & cfg.keys():
        _nonnegative_int(cfg[key], f"source.decoder.{key}", minimum=1)
    for key in {
        "is_cron_session",
        "exclude_operational_notifications",
        "retain_channel_forwarded",
        "exclude_cron_sessions",
        "include_channel_metadata",
        "include_session_metadata",
        "allow_torn_current_frame",
    } & cfg.keys():
        _bool(cfg[key], f"source.decoder.{key}")
    for key in {
        "excluded_cwd_prefixes",
        "operational_notification_prefixes",
        "channel_forward_prefixes",
        "session_metadata_fields",
    } & cfg.keys():
        values = _string_list(cfg[key], f"source.decoder.{key}")
        if any(not item for item in values):
            raise ManifestError(f"source.decoder.{key} entries must not be empty")
    if "conversation_kind" in cfg:
        _enum(
            cfg["conversation_kind"],
            {"main", "conversational-subagent"},
            "source.decoder.conversation_kind",
        )
    if "compression" in cfg:
        _enum(
            cfg["compression"],
            {"auto", "plain", "zstd"},
            "source.decoder.compression",
        )
    if "channel" in cfg and (
        not isinstance(cfg["channel"], str) or not cfg["channel"].strip()
    ):
        raise ManifestError("source.decoder.channel must be a non-empty string")
    return cfg


def _source(value: Any, environ: Mapping[str, str]) -> SourceSpec:
    cfg = _mapping(value, "source")
    _required(cfg, _SOURCE_KEYS, "source")
    _only(cfg, _SOURCE_KEYS, "source")
    source_id = _label(cfg["id"], "source.id")
    enabled = _bool(cfg["enabled"], "source.enabled")
    harness = _enum(cfg["harness"], set(SUPPORTED_HARNESSES), "source.harness")
    path_kind, path = _path_value(
        _mapping(cfg["path"], "source.path"), harness, environ
    )
    discovery_cfg = _mapping(cfg["discovery"], "source.discovery")
    _required(discovery_cfg, {"mode", "patterns"}, "source.discovery")
    _only(discovery_cfg, {"mode", "patterns"}, "source.discovery")
    discovery_mode = _enum(
        discovery_cfg["mode"], {"file", "glob"}, "source.discovery.mode"
    )
    patterns = _string_list(discovery_cfg["patterns"], "source.discovery.patterns")
    if discovery_mode == "file" and patterns:
        raise ManifestError("file discovery must use an empty patterns list")
    if discovery_mode == "glob" and not patterns:
        raise ManifestError("glob discovery requires at least one pattern")
    if any(
        not pattern
        or "\0" in pattern
        or Path(pattern).is_absolute()
        or ".." in Path(pattern).parts
        for pattern in patterns
    ):
        raise ManifestError("candidate glob patterns must stay below their source root")
    decoder = _decoder_options(harness, cfg["decoder"])
    snapshot = _enum(
        cfg["snapshot"], {"stable-bytes", "sqlite-readonly"}, "source.snapshot"
    )
    if harness == "opencode" and snapshot != "sqlite-readonly":
        raise ManifestError("OpenCode sources require sqlite-readonly snapshots")
    if harness != "opencode" and snapshot != "stable-bytes":
        raise ManifestError("only OpenCode sources may use sqlite-readonly snapshots")
    return SourceSpec(
        source_id,
        enabled,
        harness,
        path,
        path_kind,
        _enum(cfg["authority"], {"owner", "mirror"}, "source.authority"),
        _bool(cfg["required"], "source.required"),
        _label(cfg["output_node"], "source.output_node"),
        _root_policy(cfg["root_policy"]),
        snapshot,
        Discovery(discovery_mode, patterns),
        decoder,
        _bool(cfg["allow_empty"], "source.allow_empty"),
    )


def _nonnegative_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ManifestError(f"{name} must be an integer >= {minimum}")
    return value


def _parse(data: Any, environ: Mapping[str, str]) -> Manifest:
    cfg = _mapping(data, "manifest")
    _required(cfg, _TOP_KEYS, "manifest")
    _only(cfg, _TOP_KEYS, "manifest")
    if cfg["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"unsupported manifest schema: {cfg['schema_version']!r}")
    node_label = _label(cfg["node_label"], "node_label")
    raw_sources = cfg["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ManifestError("sources must contain at least one explicit source")
    sources = tuple(_source(item, environ) for item in raw_sources)
    ids = [source.source_id for source in sources]
    if len(ids) != len(set(ids)):
        raise ManifestError("source ids must be unique")
    if not any(source.enabled for source in sources):
        raise ManifestError("at least one source must explicitly set enabled=true")

    ownership = _mapping(cfg["ownership"], "ownership")
    _required(ownership, {"mode"}, "ownership")
    _only(ownership, {"mode"}, "ownership")
    ownership_mode = _enum(ownership["mode"], {"owner", "aggregator"}, "ownership.mode")

    event = _mapping(cfg["event_policy"], "event_policy")
    event_keys = {
        "synthetic_prefixes",
        "peer_agent_prefixes",
        "peer_agent_exact",
        "min_direct_user_events",
        "min_user_chars",
        "retain_conversational_subagents",
    }
    _required(event, event_keys, "event_policy")
    _only(event, event_keys, "event_policy")
    event_policy = EventPolicy(
        _string_list(event["synthetic_prefixes"], "synthetic_prefixes"),
        _string_list(event["peer_agent_prefixes"], "peer_agent_prefixes"),
        _string_list(event["peer_agent_exact"], "peer_agent_exact"),
        _nonnegative_int(
            event["min_direct_user_events"], "min_direct_user_events", minimum=1
        ),
        _nonnegative_int(event["min_user_chars"], "min_user_chars", minimum=1),
        _bool(
            event["retain_conversational_subagents"], "retain_conversational_subagents"
        ),
    )
    if any(
        not item
        for item in (
            *event_policy.synthetic_prefixes,
            *event_policy.peer_agent_prefixes,
            *event_policy.peer_agent_exact,
        )
    ):
        raise ManifestError("event policy prefix and exact values must not be empty")

    project = _mapping(cfg["project_policy"], "project_policy")
    project_keys = {"mode", "unknown", "allowlist", "denylist", "aliases"}
    _required(project, project_keys, "project_policy")
    _only(project, project_keys, "project_policy")
    aliases = _mapping(project["aliases"], "project_policy.aliases")
    if any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in aliases.items()
    ):
        raise ManifestError("project aliases must map strings to strings")
    project_policy = ProjectPolicy(
        _enum(project["mode"], {"all", "allowlist", "denylist"}, "project_policy.mode"),
        _enum(project["unknown"], {"keep", "drop", "fail"}, "project_policy.unknown"),
        _string_list(project["allowlist"], "project_policy.allowlist"),
        _string_list(project["denylist"], "project_policy.denylist"),
        aliases,
    )
    if any(not item for item in (*project_policy.allowlist, *project_policy.denylist)):
        raise ManifestError("project allowlist and denylist values must not be empty")

    output = _mapping(cfg["output"], "output")
    output_keys = {
        "repository_root",
        "history_directory",
        "prompt_directory",
        "layout",
        "migration",
        "prompt_max_chars",
        "prompt_code_block_max_chars",
        "encryption_attributes",
        "compatibility",
    }
    _required(output, output_keys, "output")
    _only(output, output_keys, "output")
    encryption_attributes = _mapping(
        output["encryption_attributes"], "output.encryption_attributes"
    )
    if any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", key)
        or not isinstance(value, str)
        or not value
        for key, value in encryption_attributes.items()
    ):
        raise ManifestError("output encryption attributes must map strings to strings")
    reserved_headers = _RESERVED_OUTPUT_HEADERS.intersection(encryption_attributes)
    if reserved_headers:
        raise ManifestError("output encryption attributes use reserved header names")
    compatibility = _mapping(output["compatibility"], "output.compatibility")
    _required(
        compatibility, {"rule_version", "unchanged_sha256"}, "output.compatibility"
    )
    _only(compatibility, {"rule_version", "unchanged_sha256"}, "output.compatibility")
    compatibility_rule = _enum(
        compatibility["rule_version"],
        {"none", "legacy-output/v1"},
        "output.compatibility.rule_version",
    )
    compatibility_hashes = _string_list(
        compatibility["unchanged_sha256"], "output.compatibility.unchanged_sha256"
    )
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in compatibility_hashes):
        raise ManifestError("compatibility hashes must be lowercase SHA-256 values")
    if compatibility_rule == "none" and compatibility_hashes:
        raise ManifestError("compatibility hashes require legacy-output/v1")
    prompt_max = _nonnegative_int(
        output["prompt_max_chars"], "prompt_max_chars", minimum=64
    )
    code_max = _nonnegative_int(
        output["prompt_code_block_max_chars"], "prompt_code_block_max_chars", minimum=16
    )
    if code_max > prompt_max:
        raise ManifestError(
            "prompt_code_block_max_chars must not exceed prompt_max_chars"
        )
    output_spec = OutputSpec(
        _absolute(output["repository_root"], "output.repository_root"),
        _relative(output["history_directory"], "output.history_directory"),
        _relative(output["prompt_directory"], "output.prompt_directory"),
        _enum(output["layout"], {"flat", "monthly"}, "output.layout"),
        _enum(output["migration"], {"none", "flat-to-monthly"}, "output.migration"),
        prompt_max,
        code_max,
        encryption_attributes,
        compatibility_rule,
        compatibility_hashes,
    )
    if output_spec.history_directory == output_spec.prompt_directory:
        raise ManifestError("history and prompt output directories must be distinct")

    transforms = _mapping(cfg["transforms"], "transforms")
    _required(transforms, {"redaction"}, "transforms")
    _only(transforms, {"redaction"}, "transforms")
    redaction = _mapping(transforms["redaction"], "transforms.redaction")
    redaction_keys = {"required", "builtin_policy", "patterns"}
    _required(redaction, redaction_keys, "transforms.redaction")
    _only(redaction, redaction_keys, "transforms.redaction")
    raw_patterns = redaction["patterns"]
    if not isinstance(raw_patterns, list) or any(
        not isinstance(item, dict) for item in raw_patterns
    ):
        raise ManifestError("redaction patterns must be an array of objects")
    patterns = []
    for index, pattern in enumerate(raw_patterns):
        _required(pattern, {"name", "regex", "canary"}, f"redaction pattern {index}")
        _only(pattern, {"name", "regex", "canary"}, f"redaction pattern {index}")
        if any(
            not isinstance(pattern[key], str) or not pattern[key] for key in pattern
        ):
            raise ManifestError("redaction pattern fields must be non-empty strings")
        patterns.append(pattern)
    redaction_spec = RedactionSpec(
        _bool(redaction["required"], "transforms.redaction.required"),
        _enum(redaction["builtin_policy"], {"default", "none"}, "builtin_policy"),
        tuple(patterns),
    )

    cleanup = _mapping(cfg["cleanup"], "cleanup")
    _required(cleanup, {"scope"}, "cleanup")
    _only(cleanup, {"scope"}, "cleanup")
    cleanup_spec = CleanupSpec(
        _enum(cleanup["scope"], {"none", "owner", "aggregator"}, "cleanup.scope"),
    )
    if cleanup_spec.scope != "none" and cleanup_spec.scope != ownership_mode:
        raise ManifestError("cleanup scope must match the ownership mode")

    indexes = _mapping(cfg["indexes"], "indexes")
    _required(indexes, {"mode"}, "indexes")
    _only(indexes, {"mode"}, "indexes")
    indexes_mode = _enum(
        indexes["mode"],
        {"none", "owner", "every-node", "aggregator-only"},
        "indexes.mode",
    )

    publisher = _mapping(cfg["publisher"], "publisher")
    publisher_keys = {
        "strategy",
        "owned_subtrees",
        "encryption",
        "key_link",
        "ciphertext_checkout",
    }
    _required(publisher, publisher_keys, "publisher")
    _only(publisher, publisher_keys, "publisher")
    owned = tuple(
        _relative(item, "publisher.owned_subtrees[]")
        for item in _string_list(
            publisher["owned_subtrees"], "publisher.owned_subtrees", allow_empty=False
        )
    )
    if len(owned) != len(set(owned)):
        raise ManifestError("publisher owned_subtrees must be unique")
    for index, left in enumerate(owned):
        for right in owned[index + 1 :]:
            if left.startswith(right + "/") or right.startswith(left + "/"):
                raise ManifestError("publisher owned_subtrees must not overlap")
    key_link = publisher["key_link"]
    key_source: Path | None = None
    key_target: str | None = None
    if key_link is not None:
        key_cfg = _mapping(key_link, "publisher.key_link")
        _required(key_cfg, {"source", "target"}, "publisher.key_link")
        _only(key_cfg, {"source", "target"}, "publisher.key_link")
        key_source = _absolute(key_cfg["source"], "publisher.key_link.source")
        key_target = _relative(key_cfg["target"], "publisher.key_link.target")
        if any(
            key_target == item or key_target.startswith(item + "/") for item in owned
        ):
            raise ManifestError("publisher key link must be outside owned subtrees")
    publisher_spec = PublisherSpec(
        _enum(
            publisher["strategy"],
            {"none", "filesystem-atomic", "git-worktree"},
            "publisher.strategy",
        ),
        owned,
        _enum(publisher["encryption"], {"none", "git-crypt"}, "publisher.encryption"),
        key_source,
        key_target,
        _enum(
            publisher["ciphertext_checkout"],
            {"refuse"},
            "publisher.ciphertext_checkout",
        ),
    )
    if publisher_spec.encryption == "git-crypt" and key_source is None:
        raise ManifestError("git-crypt publication requires a key link")
    if publisher_spec.encryption == "none" and key_source is not None:
        raise ManifestError("an unencrypted publisher must not configure a key link")
    for directory in (output_spec.history_directory, output_spec.prompt_directory):
        if not any(
            directory == item or directory.startswith(item + "/") for item in owned
        ):
            raise ManifestError(
                "publisher owned_subtrees must contain both output directories"
            )

    gates = _mapping(cfg["gates"], "gates")
    gate_keys = {
        "source_failure",
        "require_redaction_self_test",
        "require_output_audit",
        "require_reconciliation",
        "require_prepublication_scan",
    }
    _required(gates, gate_keys, "gates")
    _only(gates, gate_keys, "gates")
    gate_spec = GateSpec(
        _enum(
            gates["source_failure"],
            {"abort-required", "abort-any"},
            "gates.source_failure",
        ),
        _bool(gates["require_redaction_self_test"], "require_redaction_self_test"),
        _bool(gates["require_output_audit"], "require_output_audit"),
        _bool(gates["require_reconciliation"], "require_reconciliation"),
        _bool(gates["require_prepublication_scan"], "require_prepublication_scan"),
    )
    if redaction_spec.required and not gate_spec.require_redaction_self_test:
        raise ManifestError("required redaction also requires its self-test gate")
    if redaction_spec.required and not gate_spec.require_prepublication_scan:
        raise ManifestError("required redaction also requires pre-publication scanning")
    if publisher_spec.strategy != "none" and not (
        gate_spec.require_output_audit
        and gate_spec.require_reconciliation
        and gate_spec.require_prepublication_scan
    ):
        raise ManifestError("a mutating publisher requires every publication gate")
    return Manifest(
        MANIFEST_SCHEMA_VERSION,
        node_label,
        sources,
        ownership_mode,
        event_policy,
        project_policy,
        output_spec,
        redaction_spec,
        cleanup_spec,
        indexes_mode,
        publisher_spec,
        gate_spec,
    )


def load_manifest(
    path: str | os.PathLike[str], *, environ: Mapping[str, str]
) -> Manifest:
    """Read a manifest without consulting ambient profile configuration."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError("manifest is missing or unreadable") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ManifestError("manifest is not valid UTF-8 JSON") from exc
    return _parse(data, environ)
