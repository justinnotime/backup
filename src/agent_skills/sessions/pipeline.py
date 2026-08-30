"""Reusable extraction, planning, checking, and publication pipeline."""

from __future__ import annotations

import hashlib
import importlib
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .audit import OutputInventory, audit_plan, scan_inventory
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
from .policies import PolicyError, deduplicate_sessions, normalize_decoded
from .publish import prepare_git_worktree, publish_filesystem
from .reconcile import decoder_canary_self_test, reconcile_snapshot
from .redact import Redactor
from .render import render_history, render_prompts
from .sources import (
    SourceAccessError,
    discover_candidates,
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


def _extract_source(manifest: Manifest, source: SourceSpec):
    source_sessions = []
    observations = []
    diagnostics = []
    try:
        root = validate_configured_path(source)
        candidates = discover_candidates(source, root)
        if not candidates and not source.allow_empty:
            raise SourceAccessError("source contains no candidates")
        decoder = decoder_for(source.harness)
        for candidate in candidates:
            snapshot = snapshot_candidate(source, root, candidate)
            decoder_options = dict(snapshot.decoder_options)
            decoder_options["synthetic_prompt_prefixes"] = (
                manifest.event_policy.synthetic_prefixes
            )
            if source.harness == "claude-code":
                # Decode conversational subagents first; the shared event
                # policy makes the sole retention decision afterward.
                decoder_options["retain_conversational_subagents"] = True
            snapshot = replace(snapshot, decoder_options=decoder_options)
            batch = decoder.decode(snapshot)
            observations.append(batch.observations)
            diagnostics.extend(batch.diagnostics)
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
                raise SourceAccessError(
                    "decoder did not produce a complete source view"
                )
            for decoded in batch.sessions:
                normalized = normalize_decoded(
                    decoded,
                    manifest=manifest,
                    source=source,
                    source_ref=snapshot.source_ref,
                )
                if normalized is not None:
                    source_sessions.append(normalized)
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


def _existing_paths(inventory: OutputInventory):
    result = {}
    for entry in sorted(inventory.entries, key=lambda item: item.relative_path):
        if entry.identity is not None and entry.kind in {"history", "prompts"}:
            result.setdefault((entry.identity, entry.kind), entry)
    return result


def build_publication_plan(
    manifest: Manifest,
    snapshot: ExtractionSnapshot,
    inventory: OutputInventory,
    redactor: Redactor,
) -> PublicationPlan:
    allocated = allocate_filenames(snapshot.sessions)
    existing = _existing_paths(inventory)
    inventory_by_path = inventory.by_path()
    writes = []
    explicit_removals = []
    diagnostics = []
    occupied = {
        entry.relative_path: entry.identity
        for entry in inventory.entries
        if entry.identity is not None
    }
    for session in snapshot.sessions:
        filename = allocated[session.identity]
        for entry_kind, planned_kind, directory, renderer in (
            ("history", "history", manifest.output.history_directory, render_history),
            ("prompts", "prompt", manifest.output.prompt_directory, render_prompts),
        ):
            prior = existing.get((session.identity, entry_kind))
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
            if conflicting is not None and conflicting != session.identity:
                path = Path(desired)
                desired = f"{path.parent.as_posix()}/{path.stem}--{identity_digest(session.identity)}{path.suffix}"
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
    inventory = scan_inventory(manifest)
    plan = build_publication_plan(manifest, snapshot, inventory, redactor)
    reconcile = reconcile_snapshot(snapshot, inventory, plan)
    indexed_plan = add_indexes(manifest, inventory, plan)
    if (
        manifest.gates.require_output_audit
        or manifest.gates.require_prepublication_scan
    ):
        audit_plan(manifest, indexed_plan, snapshot.sessions, redactor)
    return snapshot, inventory, indexed_plan, reconcile, redactor
