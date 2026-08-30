"""Authority-scoped cleanup planning over managed output only."""

from __future__ import annotations

from .audit import OutputInventory
from .manifest import Manifest
from .model import CleanupAction, Session, SourceOutcome


def authoritative_nodes(
    manifest: Manifest, outcomes: tuple[SourceOutcome, ...]
) -> frozenset[str]:
    if manifest.cleanup.scope == "none":
        return frozenset()
    by_id = {outcome.source_id: outcome for outcome in outcomes}
    sources_by_node: dict[str, list[str]] = {}
    for source in manifest.sources:
        if source.enabled:
            sources_by_node.setdefault(source.output_node, []).append(source.source_id)
    successful: set[str] = set()
    for node, source_ids in sources_by_node.items():
        if all(
            by_id.get(source_id) is not None and by_id[source_id].status == "success"
            for source_id in source_ids
        ):
            successful.add(node)
    if manifest.cleanup.scope == "owner":
        successful.intersection_update({manifest.node_label})
    return frozenset(successful)


def plan_cleanup(
    manifest: Manifest,
    inventory: OutputInventory,
    sessions: tuple[Session, ...],
    outcomes: tuple[SourceOutcome, ...],
) -> tuple[CleanupAction, ...]:
    nodes = authoritative_nodes(manifest, outcomes)
    current = {session.identity for session in sessions}
    removals = []
    for entry in inventory.entries:
        if entry.identity is None:
            continue
        if entry.kind not in {"history", "prompts"}:
            continue
        if entry.identity[1] in nodes and entry.identity not in current:
            removals.append(CleanupAction(entry.relative_path, entry.identity))
    return tuple(sorted(removals, key=lambda item: item.relative_path))
