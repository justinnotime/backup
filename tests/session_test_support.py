from __future__ import annotations

import json
from pathlib import Path


def manifest_data(
    source: Path,
    output: Path,
    *,
    harness: str = "claude-code",
    source_id: str = "source-a",
    node: str = "node-a",
    discovery_mode: str = "glob",
    patterns: list[str] | None = None,
    snapshot: str = "stable-bytes",
    ownership: str = "owner",
    cleanup: str = "owner",
    layout: str = "monthly",
    publisher: str = "none",
    indexes: str = "none",
    decoder: dict | None = None,
) -> dict:
    if patterns is None:
        patterns = ["**/*.jsonl"] if discovery_mode == "glob" else []
    return {
        "schema_version": "agent-session-extraction-manifest/v1",
        "node_label": node,
        "sources": [
            {
                "id": source_id,
                "enabled": True,
                "harness": harness,
                "path": {"kind": "explicit", "value": str(source)},
                "authority": "owner",
                "required": True,
                "output_node": node,
                "root_policy": {
                    "allowed_lexical_roots": [str(source.parent)],
                    "allowed_resolved_roots": [str(source.parent.resolve())],
                    "forbidden_components": ["forbidden-profile"],
                    "required_suffixes": [],
                    "candidate_beneath_root": True,
                    "symlinks": "confined",
                },
                "snapshot": snapshot,
                "discovery": {"mode": discovery_mode, "patterns": patterns},
                "decoder": decoder or {},
                "allow_empty": False,
            }
        ],
        "ownership": {"mode": ownership},
        "event_policy": {
            "synthetic_prefixes": ["<synthetic>"],
            "peer_agent_prefixes": ["[peer from "],
            "peer_agent_exact": ["check messages"],
            "min_direct_user_events": 1,
            "min_user_chars": 1,
            "retain_conversational_subagents": False,
        },
        "project_policy": {
            "mode": "all",
            "unknown": "keep",
            "allowlist": [],
            "denylist": [],
            "aliases": {},
        },
        "output": {
            "repository_root": str(output),
            "history_directory": "History",
            "prompt_directory": "Prompts",
            "layout": layout,
            "prompt_max_chars": 256,
            "prompt_code_block_max_chars": 96,
            "encryption_attributes": {},
            "compatibility": {"rule_version": "none", "unchanged_sha256": []},
        },
        "transforms": {
            "redaction": {"required": True, "builtin_policy": "default", "patterns": []}
        },
        "cleanup": {"scope": cleanup},
        "indexes": {"mode": indexes},
        "publisher": {
            "strategy": publisher,
            "owned_subtrees": ["History", "Prompts"],
            "encryption": "none",
            "key_link": None,
            "ciphertext_checkout": "refuse",
        },
        "gates": {
            "source_failure": "abort-required",
            "require_redaction_self_test": True,
            "require_output_audit": True,
            "require_reconciliation": True,
            "require_prepublication_scan": True,
        },
    }


def write_manifest(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
