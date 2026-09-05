"""Authority-scoped cleanup planning over managed output only."""

from __future__ import annotations

from .audit import OutputInventory
from .manifest import LEGACY_AGENT_MARKDOWN_RULES, Manifest
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
    # A session is "known" when any of its slices is current; identities of
    # day slices are <session>@<day>, so compare on the undivided triple.
    known_sessions = {
        (session.harness, session.node_label, session.session_id) for session in sessions
    }
    legacy_rule = manifest.output.compatibility_rule in LEGACY_AGENT_MARKDOWN_RULES
    removals = []
    for entry in inventory.entries:
        if entry.identity is None:
            continue
        if entry.kind not in {"history", "prompts"}:
            continue
        if entry.identity[1] not in nodes or entry.identity in current:
            continue
        undivided = (entry.identity[0], entry.identity[1], entry.identity[2].split("@", 1)[0])
        if legacy_rule and entry.grandfathered and undivided not in known_sessions:
            # Orphan legacy output: the session no longer exists in any source,
            # so Raw/ is its only copy. Never removed under a legacy rule
            # (previously only the frozen rule protected it). A grandfathered
            # file whose session is current but written under another identity
            # (a whole-session file superseded by day files) is removable.
            continue
        removals.append(CleanupAction(entry.relative_path, entry.identity))
    return tuple(sorted(removals, key=lambda item: item.relative_path))
