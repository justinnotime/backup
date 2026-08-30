"""Reusable extraction, planning, checking, and publication pipeline."""

from __future__ import annotations

import hashlib
import importlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .audit import (
    OutputInventory,
    audit_plan,
    scan_inventory,
    semantic_digest_for_session,
)
from .cleanup import plan_cleanup
from .harnesses.base import decoder_for
from .identity import allocate_filenames, identity_digest, relative_output_path
from .indexes import add_indexes
from .manifest import Manifest, SourceSpec
from .model import (
    RUN_REPORT_SCHEMA_VERSION,
    Diagnostic,
    ExtractionSnapshot,
    FormatObservations,
    PlannedFile,
    PublicationPlan,
    RunReport,
    SourceOutcome,
)
from .policies import (
    PolicyError,
    deduplicate_sessions,
    normalize_decoded,
    prompt_project_allowed,
)
from .publish import (
    PublishError,
    prepare_git_worktree,
    publish_filesystem,
    require_git_worktree_inventory_at_head,
)
from .reconcile import decoder_canary_self_test, reconcile_snapshot
from .redact import Redactor
from .render import render_history, render_prompts
from .sources import (
    SourceAccessError,
    discover_candidates,
    revalidate_snapshot,
    snapshot_candidate,
    validate_configured_path,
)


class PipelineError(RuntimeError):
    def __init__(self, code: str, *, diagnostics=(), checks=None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = tuple(diagnostics)
        self.checks = dict(checks or {})


def _load_decoders() -> None:
    for name in ("claude", "codex", "opencode", "dsh", "cursor", "openclaw"):
        importlib.import_module(f"agent_skills.sessions.harnesses.{name}")


def _merge_observations(values) -> FormatObservations:
    recognized: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    markers = 0
    accepted = 0
    for value in values:
        recognized.update(value.recognized_record_counts)
        unknown.update(value.unknown_record_counts)
        markers += value.recognizable_user_markers
        accepted += value.accepted_direct_user_events
    return FormatObservations(dict(recognized), dict(unknown), markers, accepted)


def _decode_snapshot(
    manifest: Manifest,
    source: SourceSpec,
    snapshot,
    *,
    validated_root=None,
):
    if (
        snapshot.source_id != source.source_id
        or snapshot.harness != source.harness
        or snapshot.node_label != source.output_node
    ):
        raise SourceAccessError("snapshot identity does not match its source")
    if snapshot.access_mode != "bytes" and validated_root is None:
        raise SourceAccessError("direct source snapshots require revalidation")
    decoder = decoder_for(source.harness)
    decoder_options = dict(snapshot.decoder_options)
    decoder_options["synthetic_prompt_prefixes"] = (
        manifest.event_policy.synthetic_prefixes
    )
    if source.harness == "claude-code":
        # Decode conversational subagents first; the shared event policy
        # makes the sole retention decision afterward.
        decoder_options["retain_conversational_subagents"] = True
    snapshot = replace(snapshot, decoder_options=decoder_options)
    batch = decoder.decode(snapshot)
    if validated_root is not None:
        # Direct immutable SQLite access is accepted only if the database
        # and its sidecar state are unchanged across the complete decode.
        revalidate_snapshot(snapshot, source, validated_root)
    diagnostics = list(batch.diagnostics)
    if (
        batch.observations.recognizable_user_markers
        and not batch.observations.accepted_direct_user_events
    ):
        diagnostics.append(
            Diagnostic(
                "RECOGNIZED_MARKER_WITHOUT_INPUT",
                source.source_id,
                count=batch.observations.recognizable_user_markers,
            )
        )
    if batch.completeness != "complete":
        return [], batch.observations, diagnostics, False
    sessions = []
    for decoded in batch.sessions:
        normalized = normalize_decoded(
            decoded,
            manifest=manifest,
            source=source,
            source_ref=snapshot.source_ref,
        )
        if normalized is not None:
            sessions.append(normalized)
    return sessions, batch.observations, diagnostics, True


def decode_source_snapshots(
    manifest: Manifest, source: SourceSpec, snapshots: tuple
) -> ExtractionSnapshot:
    """Decode caller-frozen bytes through the public normalized contract.

    This entry point exists for parity tools that must feed the exact same
    bytes to shared and legacy decoders. Direct-path snapshots are rejected;
    production extraction retains responsibility for source revalidation.
    """
    _load_decoders()
    if source not in manifest.sources:
        raise SourceAccessError("source is not part of the supplied manifest")
    if not snapshots and not source.allow_empty:
        raise SourceAccessError("source contains no snapshots")
    sessions = []
    observations = []
    diagnostics = []
    for snapshot in snapshots:
        if _is_superseded_snapshot(source, snapshot):
            diagnostics.append(
                Diagnostic("SOURCE_CANDIDATE_SUPERSEDED", source.source_id, count=1)
            )
            continue
        decoded, observed, snapshot_diagnostics, complete = _decode_snapshot(
            manifest, source, snapshot
        )
        if not complete:
            raise SourceAccessError("decoder did not produce a complete source view")
        sessions.extend(decoded)
        observations.append(observed)
        diagnostics.extend(snapshot_diagnostics)
    if snapshots and not observations and not source.allow_empty:
        raise SourceAccessError("all source snapshots are explicitly superseded")
    deduplicated, duplicate_diagnostics = deduplicate_sessions(sessions)
    diagnostics.extend(duplicate_diagnostics)
    outcome = SourceOutcome(
        source.source_id,
        source.output_node,
        "success",
        len(snapshots),
        len(deduplicated),
        tuple(diagnostics),
    )
    return ExtractionSnapshot(
        deduplicated,
        (outcome,),
        {source.source_id: _merge_observations(observations)},
        tuple(diagnostics),
    )


def _is_superseded_snapshot(source: SourceSpec, snapshot) -> bool:
    if not source.discovery.superseded_sha256:
        return False
    if snapshot.payload is None:
        raise SourceAccessError(
            "superseded candidate policy requires a byte-backed snapshot"
        )
    return (
        hashlib.sha256(snapshot.payload).hexdigest()
        in source.discovery.superseded_sha256
    )


def _extract_source(manifest: Manifest, source: SourceSpec):
    source_sessions = []
    observations = []
    diagnostics = []
    try:
        root = validate_configured_path(source)
        candidates = discover_candidates(source, root)
        if not candidates and not source.allow_empty:
            raise SourceAccessError("source contains no candidates")
        selected_candidates = 0
        for candidate in candidates:
            snapshot = snapshot_candidate(source, root, candidate)
            if _is_superseded_snapshot(source, snapshot):
                diagnostics.append(
                    Diagnostic(
                        "SOURCE_CANDIDATE_SUPERSEDED", source.source_id, count=1
                    )
                )
                continue
            selected_candidates += 1
            decoded, observed, snapshot_diagnostics, complete = _decode_snapshot(
                manifest, source, snapshot, validated_root=root
            )
            observations.append(observed)
            diagnostics.extend(snapshot_diagnostics)
            if not complete:
                raise SourceAccessError(
                    "decoder did not produce a complete source view"
                )
            source_sessions.extend(decoded)
        if candidates and not selected_candidates and not source.allow_empty:
            raise SourceAccessError("all source candidates are explicitly superseded")
        return (
            source_sessions,
            _merge_observations(observations),
            SourceOutcome(
                source.source_id,
                source.output_node,
                "success",
                len(candidates),
                len(source_sessions),
                tuple(diagnostics),
            ),
            diagnostics,
        )
    except (SourceAccessError, OSError, PolicyError, ValueError):
        diagnostic = Diagnostic("SOURCE_INVALID_OR_UNREADABLE", source.source_id)
        failure_diagnostics = (*diagnostics, diagnostic)
        return (
            [],
            _merge_observations(observations),
            SourceOutcome(
                source.source_id,
                source.output_node,
                "invalid",
                0,
                0,
                failure_diagnostics,
            ),
            list(failure_diagnostics),
        )
    # Decoder implementations are a plug-in boundary. Never relay arbitrary
    # exception text because it can contain source paths or transcript data.
    except Exception:  # noqa: BLE001
        diagnostic = Diagnostic("DECODER_FAILURE", source.source_id)
        return (
            [],
            _merge_observations(observations),
            SourceOutcome(
                source.source_id, source.output_node, "invalid", 0, 0, (diagnostic,)
            ),
            [diagnostic],
        )


def extract_sessions(
    manifest: Manifest, *, enforce_source_gate: bool = True
) -> ExtractionSnapshot:
    _load_decoders()
    sessions = []
    outcomes = []
    observations = {}
    diagnostics = []
    enabled = [source for source in manifest.sources if source.enabled]
    for source in enabled:
        extracted, observed, outcome, source_diagnostics = _extract_source(
            manifest, source
        )
        sessions.extend(extracted)
        outcomes.append(outcome)
        observations[source.source_id] = observed
        diagnostics.extend(source_diagnostics)
    deduplicated, duplicate_diagnostics = deduplicate_sessions(sessions)
    diagnostics.extend(duplicate_diagnostics)
    snapshot = ExtractionSnapshot(
        deduplicated,
        tuple(outcomes),
        observations,
        tuple(diagnostics),
    )
    if enforce_source_gate:
        _enforce_source_gate(manifest, snapshot)
    return snapshot


def _enforce_source_gate(manifest: Manifest, snapshot: ExtractionSnapshot) -> None:
    failed = {
        outcome.source_id
        for outcome in snapshot.source_outcomes
        if outcome.status != "success"
    }
    required = {
        source.source_id
        for source in manifest.sources
        if source.enabled and source.required
    }
    if manifest.gates.source_failure == "abort-any" and failed:
        raise PipelineError(
            "SOURCE_FAILURE",
            diagnostics=snapshot.diagnostics,
            checks={"failed_sources": len(failed)},
        )
    if manifest.gates.source_failure == "abort-required" and failed.intersection(
        required
    ):
        raise PipelineError(
            "REQUIRED_SOURCE_FAILURE",
            diagnostics=snapshot.diagnostics,
            checks={"failed_sources": len(failed)},
        )


def _existing_paths(manifest: Manifest, inventory: OutputInventory):
    result = {}
    for entry in sorted(inventory.entries, key=lambda item: item.relative_path):
        if entry.identity is not None and entry.kind in {"history", "prompts"}:
            key = (entry.identity, entry.kind)
            prior = result.get(key)
            if prior is None:
                result[key] = entry
                continue
            if (
                manifest.output.migration == "flat-to-monthly"
                and manifest.output.layout == "monthly"
            ):
                directory = (
                    manifest.output.history_directory_for(entry.identity[0])
                    if entry.kind == "history"
                    else manifest.output.prompt_directory
                )
                prior_is_flat = (
                    prior.relative_path.count("/") == directory.count("/") + 1
                )
                entry_is_flat = (
                    entry.relative_path.count("/") == directory.count("/") + 1
                )
                if prior_is_flat and not entry_is_flat:
                    result[key] = entry
    return result


def build_publication_plan(
    manifest: Manifest,
    snapshot: ExtractionSnapshot,
    inventory: OutputInventory,
    redactor: Redactor,
) -> PublicationPlan:
    strategies = {
        session.identity: manifest.output.filename_strategy_for(session.harness)
        for session in snapshot.sessions
    }
    history_destinations = {
        session.identity: manifest.output.history_directory_for(session.harness)
        for session in snapshot.sessions
    }
    prompt_destinations = {
        session.identity: manifest.output.prompt_directory
        for session in snapshot.sessions
    }
    history_allocated = allocate_filenames(
        snapshot.sessions,
        strategies=strategies,
        destinations=history_destinations,
    )
    prompt_allocated = allocate_filenames(
        snapshot.sessions,
        strategies=strategies,
        destinations=prompt_destinations,
    )
    existing = _existing_paths(manifest, inventory)
    duplicate_existing: dict[tuple[tuple[str, str, str], str], list] = {}
    for entry in inventory.entries:
        if entry.identity is not None and entry.kind in {"history", "prompts"}:
            duplicate_existing.setdefault((entry.identity, entry.kind), []).append(entry)
    legacy_prompt_candidates: dict[tuple[str, str, str], list] = {}
    for entry in inventory.entries:
        if (
            entry.kind == "prompts"
            and entry.identity is None
            and entry.semantic_digest is not None
        ):
            key = (
                entry.headers.get("Tool", ""),
                entry.headers.get("Host", ""),
                entry.semantic_digest,
            )
            legacy_prompt_candidates.setdefault(key, []).append(entry)
    for values in legacy_prompt_candidates.values():
        values.sort(key=lambda item: item.relative_path)
    claimed_legacy_prompts: set[str] = set()
    inventory_by_path = inventory.by_path()
    writes = []
    explicit_removals = []
    diagnostics = []
    occupied = {entry.relative_path: entry.identity for entry in inventory.entries}
    for session in snapshot.sessions:
        for entry_kind, planned_kind, directory, filename, renderer in (
            (
                "history",
                "history",
                manifest.output.history_directory_for(session.harness),
                history_allocated[session.identity],
                render_history,
            ),
            (
                "prompts",
                "prompt",
                manifest.output.prompt_directory,
                prompt_allocated[session.identity],
                render_prompts,
            ),
        ):
            prior = existing.get((session.identity, entry_kind))
            duplicates = duplicate_existing.get((session.identity, entry_kind), [])
            if len(duplicates) > 1:
                for duplicate in duplicates:
                    if duplicate is prior:
                        continue
                    from .model import CleanupAction

                    explicit_removals.append(
                        CleanupAction(duplicate.relative_path, session.identity)
                    )
            desired_semantic = semantic_digest_for_session(
                session, entry_kind, redactor
            )
            if prior is None and entry_kind == "prompts":
                candidates = legacy_prompt_candidates.get(
                    (session.harness, session.node_label, desired_semantic), []
                )
                available = [
                    item
                    for item in candidates
                    if item.relative_path not in claimed_legacy_prompts
                ]
                if len(available) == 1:
                    prior = available[0]
                    claimed_legacy_prompts.add(prior.relative_path)
            if entry_kind == "prompts" and not prompt_project_allowed(
                manifest, session
            ):
                if prior is not None:
                    from .model import CleanupAction

                    explicit_removals.append(
                        CleanupAction(prior.relative_path, session.identity)
                    )
                continue
            has_user_prompt = any(event.role == "user" for event in session.events)
            if entry_kind == "prompts" and not has_user_prompt:
                if prior is not None:
                    from .model import CleanupAction

                    explicit_removals.append(
                        CleanupAction(prior.relative_path, session.identity)
                    )
                continue
            desired = relative_output_path(
                directory, manifest.output.layout, session, filename
            )
            if prior is not None:
                is_flat = prior.relative_path.count("/") == directory.count("/") + 1
                migrate = (
                    manifest.output.migration == "flat-to-monthly"
                    and manifest.output.layout == "monthly"
                    and is_flat
                )
                if not migrate:
                    desired = prior.relative_path
                elif prior.relative_path != desired:
                    from .model import CleanupAction

                    explicit_removals.append(
                        CleanupAction(prior.relative_path, session.identity)
                    )
            conflicting = occupied.get(desired)
            prior_owns_desired = (
                prior is not None and prior.relative_path == desired
            )
            if (
                desired in occupied
                and not prior_owns_desired
                and conflicting != session.identity
            ):
                path = Path(desired)
                digest = identity_digest(session.identity, length=64)
                for length in range(12, 65, 4):
                    candidate = (
                        f"{path.parent.as_posix()}/{path.stem}--"
                        f"{digest[:length]}{path.suffix}"
                    )
                    if candidate not in occupied:
                        desired = candidate
                        break
                else:
                    raise PipelineError("OUTPUT_PATH_EXHAUSTED")
            raw = renderer(session, manifest.output)
            transformed, counts = redactor.apply(raw)
            if counts:
                diagnostics.append(
                    Diagnostic(
                        "REDACTION_APPLIED",
                        session.source_ref.split("/", 1)[0],
                        session.session_id,
                        sum(counts.values()),
                    )
                )
            content = transformed.encode("utf-8")
            if (
                prior is not None
                and desired == prior.relative_path
                and prior.semantic_digest is not None
                and prior.semantic_digest == desired_semantic
            ):
                continue
            old = inventory_by_path.get(desired)
            if old is not None and old.digest == hashlib.sha256(content).hexdigest():
                continue
            writes.append(PlannedFile(desired, content, session.identity, planned_kind))
            occupied[desired] = session.identity
    removals = list(
        plan_cleanup(manifest, inventory, snapshot.sessions, snapshot.source_outcomes)
    )
    removal_keys = {(item.relative_path, item.identity) for item in removals}
    for item in explicit_removals:
        if (item.relative_path, item.identity) not in removal_keys:
            removals.append(item)
    return PublicationPlan(
        tuple(sorted(writes, key=lambda item: item.relative_path)),
        tuple(sorted(removals, key=lambda item: item.relative_path)),
        tuple(diagnostics),
    )


def run_pipeline(
    manifest: Manifest,
    *,
    dry_run: bool,
    git_worktree_destination: Path | None = None,
) -> tuple[RunReport, ExtractionSnapshot, PublicationPlan]:
    snapshot, _inventory, plan, reconcile, _redactor = evaluate_pipeline(manifest)
    _enforce_source_gate(manifest, snapshot)
    if manifest.gates.require_reconciliation and not reconcile.ok:
        raise PipelineError(
            "RECONCILIATION_FAILURE",
            diagnostics=reconcile.diagnostics,
            checks=reconcile.checks,
        )
    if not dry_run:
        if manifest.publisher.strategy == "filesystem-atomic":
            publish_filesystem(manifest, plan)
        elif manifest.publisher.strategy == "git-worktree":
            if git_worktree_destination is None:
                raise PipelineError("GIT_WORKTREE_DESTINATION_REQUIRED")
            prepare_git_worktree(manifest, plan, git_worktree_destination)
        elif manifest.publisher.strategy != "none":
            raise PipelineError("UNSUPPORTED_PUBLISHER")
    report = RunReport(
        RUN_REPORT_SCHEMA_VERSION,
        "ok",
        dry_run,
        len([source for source in manifest.sources if source.enabled]),
        len(snapshot.sessions),
        len(plan.writes),
        len(plan.removals),
        tuple(
            sorted({item.code for item in (*snapshot.diagnostics, *plan.diagnostics)})
        ),
    )
    return report, snapshot, plan


def evaluate_pipeline(manifest: Manifest):
    """Read, decode, plan, and audit without mutating the filesystem."""
    # A required redactor proves itself before any source is opened.
    redactor = Redactor.from_spec(manifest.redaction)
    _load_decoders()
    canary_failures = decoder_canary_self_test(manifest, redactor)
    if canary_failures:
        raise PipelineError("DECODER_CANARY_FAILED", diagnostics=canary_failures)
    # Keep failed-source observations available to reconciliation. Source
    # gates are enforced by the mutating run path before publication.
    snapshot = extract_sessions(manifest, enforce_source_gate=False)
    try:
        require_git_worktree_inventory_at_head(manifest)
        inventory = scan_inventory(manifest)
        require_git_worktree_inventory_at_head(manifest)
    except PublishError as exc:
        raise PipelineError("GIT_WORKTREE_OUTPUT_NOT_AT_HEAD") from exc
    plan = build_publication_plan(manifest, snapshot, inventory, redactor)
    reconcile = reconcile_snapshot(snapshot, inventory, plan)
    indexed_plan = add_indexes(manifest, inventory, plan)
    if (
        manifest.gates.require_output_audit
        or manifest.gates.require_prepublication_scan
    ):
        audit_plan(manifest, indexed_plan, snapshot.sessions, redactor)
    return snapshot, inventory, indexed_plan, reconcile, redactor
