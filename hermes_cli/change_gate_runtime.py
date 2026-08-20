"""Default-off runtime composition for the accepted Change Gate contract.

This module is intentionally a thin reader/projector.  Kanban continues to
own transactions, task/run/event state, dispatch, retries, and review
transport.  The runtime only classifies explicitly marked tasks, loads their
immutable artifacts, revalidates current source/artifact state, and projects
strict review evidence for the existing ChangeGateAdapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence, cast

from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    EVIDENCE_PACKET_SCHEMA,
    FROZEN_HANDOFF_SCHEMA,
    MAX_RELEASE_LIFETIME_SECONDS,
    MAX_RUNTIME_ARTIFACT_BYTES,
    REVIEW_RESULT_SCHEMA,
    ArchitectureInventoryReader,
    ArchitectureInventoryRecord,
    ArtifactBinding,
    ChangeGateAdapter,
    ChangeGateReason,
    ChangeGateRequest,
    ChangeGateResult,
    DurableReleaseArtifact,
    EvidencePacket,
    FrozenHandoff,
    GateDecision,
    GatePhase,
    ReleasePurpose,
    ReviewResult,
    ReviewerClass,
    ReviewVerdict,
    SourceIdentity,
    UpstreamRouteSelector,
    canonical_sha256,
    evaluate_reviews,
)
from hermes_cli.change_gate_codec import (
    ArtifactCodecReason,
    decode_artifact,
    encode_artifact,
    read_artifact,
)


EVIDENCE_ATTACHMENT_FILENAME = "change-gate-evidence.json"
HANDOFF_ATTACHMENT_FILENAME = "change-gate-frozen-handoff.json"
REVIEW_METADATA_KEY = "change_gate_review_result"
REVIEW_CLAIM_KEY = "change_gate_review_claim"
DEFAULT_RELEASE_TTL_SECONDS = 300
DEFAULT_MAX_CORRECTIONS = 1
_MAX_GIT_OUTPUT_BYTES = 4096
_INVENTORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FINDING_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_CONFIG_KEYS = frozenset(
    {
        "enabled",
        "inventory_root",
        "release_ttl_seconds",
        "ungated_policy",
        "planner_assignee",
        "max_corrections",
    }
)


@dataclass(frozen=True, slots=True)
class ChangeGateRuntimePolicy:
    enabled: bool = False
    valid: bool = True
    inventory_root: Path | None = None
    release_ttl_seconds: int = DEFAULT_RELEASE_TTL_SECONDS
    ungated_policy: str = "passthrough"
    planner_assignee: str = "planner"
    max_corrections: int = DEFAULT_MAX_CORRECTIONS


@dataclass(frozen=True, slots=True)
class TaskGateArtifacts:
    evidence: EvidencePacket
    handoff: FrozenHandoff
    inventory: ArchitectureInventoryRecord
    actual_source: SourceIdentity
    actual_route: UpstreamRouteSelector
    workspace_observation: WorkspaceObservation
    evidence_attachment_sha256: str
    handoff_attachment_sha256: str


@dataclass(frozen=True, slots=True)
class TaskGateLoad:
    applicable: bool
    reason: ChangeGateReason
    artifacts: TaskGateArtifacts | None = None

    @property
    def ok(self) -> bool:
        return self.applicable and self.reason is ChangeGateReason.ALLOWED and self.artifacts is not None


@dataclass(frozen=True, slots=True)
class ReviewProjection:
    reviews: tuple[ReviewResult, ...]
    reason: ChangeGateReason = ChangeGateReason.ALLOWED

    @property
    def ok(self) -> bool:
        return self.reason is ChangeGateReason.ALLOWED


@dataclass(frozen=True, slots=True)
class RuntimeEvaluation:
    applicable: bool
    result: ChangeGateResult
    artifacts: TaskGateArtifacts | None = None
    release: DurableReleaseArtifact | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceChangedPath:
    path: str
    staged: bool
    unstaged: bool
    untracked: bool


@dataclass(frozen=True, slots=True)
class WorkspaceObservation:
    reason: ChangeGateReason = ChangeGateReason.ALLOWED
    changed_paths: tuple[str, ...] = ()
    changes: tuple[WorkspaceChangedPath, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewClaimEvaluation:
    applicable: bool
    allowed: bool
    reason: ChangeGateReason
    artifacts: TaskGateArtifacts | None = None
    reviewer_class: ReviewerClass | None = None


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    reviewer_class: ReviewerClass
    verdict: ReviewVerdict
    finding_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileArchitectureInventoryReader(ArchitectureInventoryReader):
    """Strict file-per-record read adapter for an externally owned SSOT."""

    owner_root: Path
    bindings: tuple[tuple[str, str], ...]

    def read(self, inventory_id: str) -> ArchitectureInventoryRecord | None:
        matches = tuple(binding for binding in self.bindings if binding[0] == inventory_id)
        if len(matches) != 1 or _INVENTORY_ID_RE.fullmatch(inventory_id) is None:
            return None
        result = read_artifact(
            self.owner_root,
            f"{inventory_id}.json",
            expected_schema=ARCHITECTURE_INVENTORY_SCHEMA,
            expected_sha256=matches[0][1],
        )
        return result.value if result.ok and type(result.value) is ArchitectureInventoryRecord else None


def runtime_policy_from_mapping(config: object) -> ChangeGateRuntimePolicy:
    """Parse the exact-True, default-off feature block without coercion."""

    if type(config) is not dict:
        return ChangeGateRuntimePolicy()
    config_data = cast(dict[str, object], config)
    block = config_data.get("change_gate")
    if type(block) is not dict:
        return ChangeGateRuntimePolicy()
    block_data = cast(dict[str, object], block)
    if block_data.get("enabled") is not True:
        return ChangeGateRuntimePolicy()
    if set(block_data) - _CONFIG_KEYS:
        return ChangeGateRuntimePolicy(enabled=True, valid=False)

    root_raw = block_data.get("inventory_root")
    ttl = block_data.get("release_ttl_seconds", DEFAULT_RELEASE_TTL_SECONDS)
    ungated = block_data.get("ungated_policy", "passthrough")
    planner = block_data.get("planner_assignee", "planner")
    max_corrections = block_data.get("max_corrections", DEFAULT_MAX_CORRECTIONS)
    if (
        type(root_raw) is not str
        or not root_raw.strip()
        or "\0" in root_raw
        or not Path(root_raw).is_absolute()
        or type(ttl) is not int
        or not 1 <= ttl <= MAX_RELEASE_LIFETIME_SECONDS
        or ungated != "passthrough"
        or type(planner) is not str
        or not planner.strip()
        or planner != planner.strip()
        or "\0" in planner
        or type(max_corrections) is not int
        or not 0 <= max_corrections <= 10
    ):
        return ChangeGateRuntimePolicy(enabled=True, valid=False)
    root = root_raw
    release_ttl_seconds = ttl
    ungated_policy = cast(str, ungated)
    planner_assignee = planner
    correction_limit = max_corrections
    return ChangeGateRuntimePolicy(
        enabled=True,
        valid=True,
        inventory_root=Path(root),
        release_ttl_seconds=release_ttl_seconds,
        ungated_policy=ungated_policy,
        planner_assignee=planner_assignee,
        max_corrections=correction_limit,
    )


def load_runtime_policy(config: object | None = None) -> ChangeGateRuntimePolicy:
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            return ChangeGateRuntimePolicy()
    return runtime_policy_from_mapping(config)


def load_task_gate_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    policy: ChangeGateRuntimePolicy,
    attachment_root: Path,
) -> TaskGateLoad:
    """Load an explicitly marked task without mutating task/run/event state."""

    if policy.enabled is not True:
        return TaskGateLoad(False, ChangeGateReason.DISABLED)
    rows = conn.execute(
        "SELECT filename, stored_path, size FROM task_attachments WHERE task_id = ? "
        "AND filename IN (?, ?) ORDER BY id ASC",
        (task_id, EVIDENCE_ATTACHMENT_FILENAME, HANDOFF_ATTACHMENT_FILENAME),
    ).fetchall()
    if not rows:
        return TaskGateLoad(False, ChangeGateReason.DISABLED)

    evidence_rows = tuple(row for row in rows if row["filename"] == EVIDENCE_ATTACHMENT_FILENAME)
    handoff_rows = tuple(row for row in rows if row["filename"] == HANDOFF_ATTACHMENT_FILENAME)
    if len(evidence_rows) != 1 or len(handoff_rows) != 1:
        return TaskGateLoad(True, ChangeGateReason.ARTIFACT_AMBIGUOUS)
    if not policy.valid or policy.inventory_root is None:
        return TaskGateLoad(True, ChangeGateReason.RUNTIME_CONFIG_INVALID)

    handoff_read = _read_attachment(
        handoff_rows[0],
        attachment_root=attachment_root,
        expected_schema=FROZEN_HANDOFF_SCHEMA,
    )
    if not handoff_read.ok or type(handoff_read.value) is not FrozenHandoff:
        return TaskGateLoad(True, _codec_reason(handoff_read.reason))
    handoff = handoff_read.value
    evidence_read = _read_attachment(
        evidence_rows[0],
        attachment_root=attachment_root,
        expected_schema=EVIDENCE_PACKET_SCHEMA,
        expected_sha256=handoff.evidence_sha256,
    )
    if not evidence_read.ok or type(evidence_read.value) is not EvidencePacket:
        return TaskGateLoad(True, _codec_reason(evidence_read.reason))
    evidence = evidence_read.value

    inventory_reader = FileArchitectureInventoryReader(
        policy.inventory_root,
        ((handoff.inventory_id, handoff.inventory_sha256),),
    )
    try:
        inventory = inventory_reader.read(handoff.inventory_id)
    except Exception:
        inventory = None
    if inventory is None:
        return TaskGateLoad(True, ChangeGateReason.INVENTORY_READ_FAILED)

    task_row = conn.execute(
        "SELECT assignee, model_override, provider_override, reasoning_effort, "
        "workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task_row is None:
        return TaskGateLoad(True, ChangeGateReason.TASK_ID_MISMATCH)
    try:
        actual_route = UpstreamRouteSelector(
            assignee=task_row["assignee"],
            model_override=task_row["model_override"],
            provider_override=task_row["provider_override"],
            reasoning_effort=task_row["reasoning_effort"],
        )
    except Exception:
        return TaskGateLoad(True, ChangeGateReason.ROUTE_PROJECTION_MISMATCH)

    workspace_path = task_row["workspace_path"]
    source_result = read_current_source_identity(workspace_path, evidence.source.repository)
    if isinstance(source_result, ChangeGateReason):
        return TaskGateLoad(True, source_result)
    try:
        source_root = Path(str(workspace_path)).resolve(strict=True)
    except OSError:
        return TaskGateLoad(True, ChangeGateReason.SOURCE_READ_FAILED)
    observation = observe_workspace_changes(source_root)
    artifacts = TaskGateArtifacts(
        evidence=evidence,
        handoff=handoff,
        inventory=inventory,
        actual_source=source_result,
        actual_route=actual_route,
        workspace_observation=observation,
        evidence_attachment_sha256=evidence_read.sha256 or "",
        handoff_attachment_sha256=handoff_read.sha256 or "",
    )
    if observation.reason is not ChangeGateReason.ALLOWED:
        return TaskGateLoad(True, observation.reason, artifacts)
    binding_reason = validate_observed_artifact_bindings(
        evidence,
        observation.changed_paths,
    )
    if binding_reason is not ChangeGateReason.ALLOWED:
        return TaskGateLoad(True, binding_reason, artifacts)
    artifact_reason = validate_bound_artifacts(
        source_root,
        evidence.required_inputs + evidence.produced_artifacts,
    )
    if artifact_reason is not ChangeGateReason.ALLOWED:
        return TaskGateLoad(True, artifact_reason, artifacts)
    return TaskGateLoad(
        True,
        ChangeGateReason.ALLOWED,
        artifacts,
    )


def evaluate_loaded_runtime(
    load: TaskGateLoad,
    *,
    purpose: ReleasePurpose,
    release: DurableReleaseArtifact | None,
    now_epoch: int,
    reviews: Sequence[ReviewResult] = (),
    release_reason: ChangeGateReason = ChangeGateReason.RELEASE_MISSING,
) -> RuntimeEvaluation:
    if not load.applicable:
        return RuntimeEvaluation(False, _allow(ChangeGateReason.DISABLED, GatePhase.G0_EVIDENCE))
    if not load.ok or load.artifacts is None:
        return RuntimeEvaluation(True, _failure(load.reason, purpose), load.artifacts)
    if release is None:
        return RuntimeEvaluation(True, _failure(release_reason, purpose), load.artifacts)

    artifacts = load.artifacts
    request = ChangeGateRequest(
        evidence=artifacts.evidence,
        frozen_handoff=artifacts.handoff,
        release_receipt=None,
        source=artifacts.actual_source,
        work=artifacts.evidence.work,
        requested_paths=artifacts.workspace_observation.changed_paths,
        observed_inputs=artifacts.evidence.required_inputs,
        observed_outputs=artifacts.evidence.produced_artifacts,
        reviews=tuple(reviews),
        purpose=purpose,
        durable_release=release,
    )
    # The current record has already been loaded through the external reader.
    # Use an immutable one-record view for the pure contract evaluation.
    from hermes_cli.change_gate import StaticArchitectureInventoryReader

    adapter = ChangeGateAdapter(
        enabled=True,
        inventory_reader=StaticArchitectureInventoryReader((artifacts.inventory,)),
        clock=lambda: now_epoch,
    )
    result = adapter.evaluate(
        request,
        actual_task_id=artifacts.evidence.work.task_id,
        actual_route=artifacts.actual_route,
    )
    return RuntimeEvaluation(True, result, artifacts, release)


def evaluate_review_claim_runtime(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    policy: ChangeGateRuntimePolicy,
    attachment_root: Path,
) -> ReviewClaimEvaluation:
    """Select exactly the next frozen reviewer slot without mutating state."""

    load = load_task_gate_artifacts(
        conn,
        task_id,
        policy=policy,
        attachment_root=attachment_root,
    )
    if not load.applicable:
        return ReviewClaimEvaluation(False, True, ChangeGateReason.DISABLED)
    if not load.ok or load.artifacts is None:
        return ReviewClaimEvaluation(True, False, load.reason)

    artifacts = load.artifacts
    projection = project_upstream_reviews(
        conn,
        task_id,
        handoff=artifacts.handoff,
    )
    if not projection.ok:
        return ReviewClaimEvaluation(True, False, projection.reason, artifacts)
    aggregate = evaluate_reviews(
        projection.reviews,
        bundle_sha256=artifacts.handoff.review_bundle_sha256(),
        required_reviewers=artifacts.handoff.route.required_reviewers,
    )
    if aggregate.allowed:
        return ReviewClaimEvaluation(
            True,
            False,
            ChangeGateReason.REVIEW_CONVERGED_AWAITING_G4,
            artifacts,
        )
    if aggregate.reason not in {
        ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS,
        ChangeGateReason.REVIEW_REQUEST_CHANGES,
    }:
        return ReviewClaimEvaluation(True, False, aggregate.reason, artifacts)

    # A bounded correction keeps the frozen bundle and its older result
    # history.  Only a current PASS satisfies a reviewer slot; the latest
    # REQUEST_CHANGES result leaves that exact class eligible for a fresh
    # upstream review attempt.  REPLAN_REQUIRED remains denied above.
    completed = {
        review.reviewer_class
        for review in projection.reviews
        if review.verdict is ReviewVerdict.PASS
    }
    next_routes = tuple(
        route
        for route in artifacts.handoff.route.reviews
        if route.reviewer_class not in completed
    )
    if not next_routes:
        return ReviewClaimEvaluation(
            True,
            False,
            ChangeGateReason.REVIEW_CONVERGED_AWAITING_G4,
            artifacts,
        )
    expected = next_routes[0]
    if artifacts.actual_route != expected.selector:
        return ReviewClaimEvaluation(
            True,
            False,
            ChangeGateReason.ROUTE_PROJECTION_MISMATCH,
            artifacts,
            expected.reviewer_class,
        )
    return ReviewClaimEvaluation(
        True,
        True,
        ChangeGateReason.ALLOWED,
        artifacts,
        expected.reviewer_class,
    )


def build_review_claim_payload(evaluation: ReviewClaimEvaluation) -> dict[str, str] | None:
    if (
        not evaluation.applicable
        or not evaluation.allowed
        or evaluation.artifacts is None
        or evaluation.reviewer_class is None
    ):
        return None
    return {
        REVIEW_CLAIM_KEY: "v1",
        "bundle_sha256": evaluation.artifacts.handoff.review_bundle_sha256(),
        "reviewer_class": evaluation.reviewer_class.value,
        "route_sha256": canonical_sha256(evaluation.artifacts.actual_route),
    }


def parse_review_submission(
    value: object,
    *,
    expected_verdict: ReviewVerdict | tuple[ReviewVerdict, ...],
) -> ReviewSubmission | None:
    if type(value) is not dict or set(value) != {
        "reviewer_class",
        "verdict",
        "finding_codes",
    }:
        return None
    data = cast(dict[str, object], value)
    try:
        reviewer_class = ReviewerClass(data["reviewer_class"])
        verdict = ReviewVerdict(data["verdict"])
    except (TypeError, ValueError):
        return None
    raw_codes = data["finding_codes"]
    if type(raw_codes) is not list or len(raw_codes) > 32:
        return None
    if any(type(code) is not str or _FINDING_CODE_RE.fullmatch(code) is None for code in raw_codes):
        return None
    finding_codes = tuple(cast(list[str], raw_codes))
    allowed_verdicts = (
        expected_verdict
        if type(expected_verdict) is tuple
        else (expected_verdict,)
    )
    if (
        not allowed_verdicts
        or any(type(item) is not ReviewVerdict for item in allowed_verdicts)
        or verdict not in allowed_verdicts
        or tuple(sorted(set(finding_codes))) != finding_codes
        or (verdict is ReviewVerdict.PASS and finding_codes)
        or (verdict is not ReviewVerdict.PASS and not finding_codes)
    ):
        return None
    return ReviewSubmission(reviewer_class, verdict, finding_codes)


def build_current_review_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    handoff: FrozenHandoff,
    submission: ReviewSubmission,
    completed_at_epoch: int,
) -> ReviewResult | None:
    """Bind a bounded reviewer submission to the live upstream review run."""

    row = conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "running"
        or row["current_run_id"] is None
        or type(completed_at_epoch) is not int
    ):
        return None
    run_id = int(row["current_run_id"])
    run = conn.execute(
        "SELECT profile FROM task_runs WHERE id = ? AND task_id = ? "
        "AND ended_at IS NULL AND status = 'running'",
        (run_id, task_id),
    ).fetchone()
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
        (task_id, run_id),
    ).fetchone()
    try:
        payload = json.loads(event["payload"]) if event and event["payload"] else {}
    except (TypeError, json.JSONDecodeError):
        return None
    routes = tuple(
        route
        for route in handoff.route.reviews
        if route.reviewer_class is submission.reviewer_class
    )
    if (
        run is None
        or type(payload) is not dict
        or payload.get("source_status") != "review"
        or payload.get(REVIEW_CLAIM_KEY) != "v1"
        or payload.get("bundle_sha256") != handoff.review_bundle_sha256()
        or payload.get("reviewer_class") != submission.reviewer_class.value
        or len(routes) != 1
        or payload.get("route_sha256") != canonical_sha256(routes[0].selector)
        or run["profile"] != row["assignee"]
        or run["profile"] != routes[0].selector.assignee
    ):
        return None
    review = ReviewResult(
        bundle_sha256=handoff.review_bundle_sha256(),
        reviewer_class=submission.reviewer_class,
        reviewer_identity=str(run["profile"]),
        attempt_id=str(run_id),
        verdict=submission.verdict,
        finding_codes=submission.finding_codes,
        completed_at_epoch=completed_at_epoch,
    )
    decoded = decode_artifact(
        encode_artifact(review),
        expected_schema=REVIEW_RESULT_SCHEMA,
    )
    return review if decoded.ok else None


def project_upstream_reviews(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    handoff: FrozenHandoff,
) -> ReviewProjection:
    """Project the latest current result per required reviewer class."""

    if type(handoff) is not FrozenHandoff:
        return ReviewProjection((), ChangeGateReason.FROZEN_HANDOFF_MALFORMED)
    bundle_sha256 = handoff.review_bundle_sha256()
    required = handoff.route.required_reviewers
    if required not in {
        (ReviewerClass.REVIEWER,),
        (ReviewerClass.NORMAL, ReviewerClass.DEEP),
    }:
        return ReviewProjection((), ChangeGateReason.ROUTE_POLICY_CONFLICT)

    rows = conn.execute(
        "SELECT id, profile, ended_at, outcome, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL AND metadata IS NOT NULL "
        "ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    projected_by_class: dict[ReviewerClass, ReviewResult] = {}
    selected_attempts: set[str] = set()
    for row in rows:
        have_required = set(projected_by_class) == set(required)
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            if have_required:
                continue
            if REVIEW_METADATA_KEY in str(row["metadata"]):
                return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
            continue
        if type(metadata) is not dict or REVIEW_METADATA_KEY not in metadata:
            continue
        raw = metadata[REVIEW_METADATA_KEY]
        if type(raw) is not str:
            if have_required:
                continue
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        decoded = decode_artifact(raw.encode("utf-8"), expected_schema=REVIEW_RESULT_SCHEMA)
        if not decoded.ok or type(decoded.value) is not ReviewResult:
            if have_required:
                continue
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        review = decoded.value
        if review.attempt_id in selected_attempts:
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        if have_required:
            continue
        if review.reviewer_class not in required:
            return ReviewProjection((), ChangeGateReason.REVIEW_CLASS_UNEXPECTED)
        if review.reviewer_class in projected_by_class:
            continue
        if review.bundle_sha256 != bundle_sha256:
            return ReviewProjection((), ChangeGateReason.REVIEW_BUNDLE_MISMATCH)
        expected_event = {
            ReviewVerdict.PASS: "review_requested",
            ReviewVerdict.REQUEST_CHANGES: "changes_requested",
            ReviewVerdict.REPLAN_REQUIRED: "changes_requested",
        }[review.verdict]
        expected_outcome = {
            ReviewVerdict.PASS: "review_requested",
            ReviewVerdict.REQUEST_CHANGES: "changes_requested",
            ReviewVerdict.REPLAN_REQUIRED: "changes_requested",
        }[review.verdict]
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, int(row["id"]), expected_event),
        ).fetchone()
        claimed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
            "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
            (task_id, int(row["id"])),
        ).fetchone()
        try:
            claimed_payload = (
                json.loads(claimed["payload"])
                if claimed is not None and claimed["payload"]
                else {}
            )
        except (TypeError, json.JSONDecodeError):
            claimed_payload = {}
        routes = tuple(
            route
            for route in handoff.route.reviews
            if route.reviewer_class is review.reviewer_class
        )
        if (
            review.attempt_id != str(row["id"])
            or review.reviewer_identity != row["profile"]
            or review.completed_at_epoch != row["ended_at"]
            or row["outcome"] != expected_outcome
            or event is None
            or type(claimed_payload) is not dict
            or claimed_payload.get("source_status") != "review"
            or claimed_payload.get(REVIEW_CLAIM_KEY) != "v1"
            or claimed_payload.get("bundle_sha256") != bundle_sha256
            or claimed_payload.get("reviewer_class") != review.reviewer_class.value
            or len(routes) != 1
            or claimed_payload.get("route_sha256") != canonical_sha256(routes[0].selector)
            or row["profile"] != routes[0].selector.assignee
        ):
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        projected_by_class[review.reviewer_class] = review
        selected_attempts.add(review.attempt_id)
    projected = tuple(
        projected_by_class[reviewer]
        for reviewer in required
        if reviewer in projected_by_class
    )
    return ReviewProjection(projected)


def build_review_result_metadata(review: ReviewResult) -> dict[str, str]:
    """Return the bounded payload stored on the existing upstream run row."""

    return {REVIEW_METADATA_KEY: encode_artifact(review).decode("utf-8")}


def count_change_gate_corrections(
    conn: sqlite3.Connection,
    task_id: str,
) -> int | None:
    """Count strict REQUEST_CHANGES results across this task's lifecycle.

    ``None`` means a row claiming to be Change Gate metadata was malformed;
    callers must fail closed instead of treating it as an absent correction.
    Results for older frozen bundles still count toward the bounded correction
    budget, while ordinary upstream metadata remains outside this contract.
    """

    rows = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            if REVIEW_METADATA_KEY in str(row["metadata"]):
                return None
            continue
        if type(metadata) is not dict or REVIEW_METADATA_KEY not in metadata:
            continue
        raw = metadata[REVIEW_METADATA_KEY]
        if type(raw) is not str:
            return None
        decoded = decode_artifact(
            raw.encode("utf-8"),
            expected_schema=REVIEW_RESULT_SCHEMA,
        )
        if not decoded.ok or type(decoded.value) is not ReviewResult:
            return None
        if decoded.value.verdict is ReviewVerdict.REQUEST_CHANGES:
            count += 1
    return count


def read_current_source_identity(
    workspace_path: object,
    expected_repository: str,
) -> SourceIdentity | ChangeGateReason:
    if (
        type(workspace_path) is not str
        or not workspace_path.strip()
        or "\0" in workspace_path
        or not Path(workspace_path).is_absolute()
    ):
        return ChangeGateReason.SOURCE_READ_FAILED
    workspace = Path(workspace_path)
    try:
        if workspace.is_symlink() or not workspace.resolve(strict=True).is_dir():
            return ChangeGateReason.SOURCE_READ_FAILED
        root = workspace.resolve(strict=True)
    except OSError:
        return ChangeGateReason.SOURCE_READ_FAILED

    commit = _git_read(root, "rev-parse", "HEAD")
    tree = _git_read(root, "rev-parse", "HEAD^{tree}")
    branch = _git_read(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    remote = _git_read(root, "config", "--get", "remote.origin.url")
    repository = _normalize_repository(remote)
    if commit is None or tree is None or branch is None or repository is None:
        return ChangeGateReason.SOURCE_READ_FAILED
    if repository != expected_repository:
        return ChangeGateReason.SOURCE_READ_FAILED
    return SourceIdentity(repository=repository, branch=branch, commit=commit, tree=tree)


def validate_bound_artifacts(
    source_root: Path,
    bindings: Sequence[ArtifactBinding],
) -> ChangeGateReason:
    try:
        root = source_root.resolve(strict=True)
    except OSError:
        return ChangeGateReason.ARTIFACT_READ_FAILED
    for binding in bindings:
        path = PurePosixPath(binding.path)
        if path.is_absolute() or ".." in path.parts or "\\" in binding.path:
            return ChangeGateReason.ARTIFACT_PATH_INVALID
        candidate = root.joinpath(*path.parts)
        try:
            cursor = root
            for part in path.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return ChangeGateReason.ARTIFACT_SYMLINK
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            stat = resolved.stat()
            if not resolved.is_file():
                return ChangeGateReason.ARTIFACT_READ_FAILED
        except (OSError, ValueError):
            return ChangeGateReason.ARTIFACT_READ_FAILED
        if stat.st_size > MAX_RUNTIME_ARTIFACT_BYTES:
            return ChangeGateReason.ARTIFACT_TOO_LARGE
        try:
            data = resolved.read_bytes()
        except OSError:
            return ChangeGateReason.ARTIFACT_READ_FAILED
        if hashlib.sha256(data).hexdigest() != binding.sha256:
            return ChangeGateReason.ARTIFACT_DIGEST_MISMATCH
        if binding.git_oid is not None:
            oid = _git_read(root, "hash-object", "--no-filters", str(resolved))
            if oid != binding.git_oid:
                return ChangeGateReason.ARTIFACT_BINDING_MISMATCH
    return ChangeGateReason.ALLOWED


@dataclass(frozen=True, slots=True)
class _BindingIndex:
    path_to_binding: dict[str, ArtifactBinding]
    ambiguous: bool = False


def validate_observed_artifact_bindings(
    evidence: EvidencePacket,
    observed_paths: Sequence[str],
) -> ChangeGateReason:
    if type(evidence) is not EvidencePacket or type(observed_paths) not in {tuple, list}:
        return ChangeGateReason.ARTIFACT_BINDING_MISMATCH
    if not set(observed_paths).issubset(evidence.allowed_paths):
        return ChangeGateReason.SCOPE_DEVIATION
    index = _binding_by_path(evidence)
    if index.ambiguous:
        return ChangeGateReason.ARTIFACT_AMBIGUOUS
    for path in observed_paths:
        if path not in index.path_to_binding:
            return ChangeGateReason.ARTIFACT_BINDING_MISMATCH
    return ChangeGateReason.ALLOWED


def observe_workspace_changes(source_root: Path) -> WorkspaceObservation:
    """Return actual Git-visible changed paths without exposing file content."""

    if not source_root.is_absolute():
        return WorkspaceObservation(ChangeGateReason.SOURCE_READ_FAILED)
    staged = _git_name_status(
        source_root,
        ("diff", "--name-status", "-z", "--find-renames", "-C", "-C", "--cached"),
        staged=True,
    )
    unstaged = _git_name_status(
        source_root,
        ("diff", "--name-status", "-z", "--find-renames", "-C", "-C"),
        unstaged=True,
    )
    untracked = _git_untracked(source_root)
    for result in (staged, unstaged, untracked):
        if result.reason is not ChangeGateReason.ALLOWED:
            return result
    by_path: dict[str, WorkspaceChangedPath] = {}
    for result in (staged, unstaged, untracked):
        for change in result.changes:
            current = by_path.get(change.path)
            by_path[change.path] = (
                change
                if current is None
                else WorkspaceChangedPath(
                    path=change.path,
                    staged=current.staged or change.staged,
                    unstaged=current.unstaged or change.unstaged,
                    untracked=current.untracked or change.untracked,
                )
            )
    paths = tuple(sorted(by_path))
    if paths:
        submodule_check = _has_submodule_path(source_root, paths)
        if submodule_check is not False:
            return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    changes = tuple(by_path[path] for path in paths)
    return WorkspaceObservation(ChangeGateReason.ALLOWED, paths, changes)


def _binding_by_path(evidence: EvidencePacket) -> _BindingIndex:
    path_to_binding: dict[str, ArtifactBinding] = {}
    ambiguous = False
    for binding in evidence.required_inputs + evidence.produced_artifacts:
        if binding.path in path_to_binding:
            ambiguous = True
            continue
        path_to_binding[binding.path] = binding
    return _BindingIndex(path_to_binding, ambiguous)


def _git_name_status(
    root: Path,
    args: tuple[str, ...],
    *,
    staged: bool = False,
    unstaged: bool = False,
) -> WorkspaceObservation:
    data = _git_read_bytes(root, *args)
    if data is None:
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    if not data:
        return WorkspaceObservation(ChangeGateReason.ALLOWED)
    if not data.endswith(b"\0"):
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    try:
        tokens = data[:-1].decode("utf-8", "strict").split("\0")
    except UnicodeDecodeError:
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    changes: list[WorkspaceChangedPath] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
        if status[0] in {"R", "C"}:
            return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
        if status not in {"M", "A"}:
            return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
        if index >= len(tokens) or not _valid_observed_path(tokens[index]):
            return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
        changes.append(
            WorkspaceChangedPath(
                path=tokens[index],
                staged=staged,
                unstaged=unstaged,
                untracked=False,
            )
        )
        index += 1
    paths = tuple(change.path for change in changes)
    return WorkspaceObservation(ChangeGateReason.ALLOWED, paths, tuple(changes))


def _git_untracked(root: Path) -> WorkspaceObservation:
    data = _git_read_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    if data is None:
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    if not data:
        return WorkspaceObservation(ChangeGateReason.ALLOWED)
    if not data.endswith(b"\0"):
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    try:
        paths = tuple(data[:-1].decode("utf-8", "strict").split("\0"))
    except UnicodeDecodeError:
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    if any(not _valid_observed_path(path) for path in paths):
        return WorkspaceObservation(ChangeGateReason.ARTIFACT_AMBIGUOUS)
    changes = tuple(
        WorkspaceChangedPath(path=path, staged=False, unstaged=False, untracked=True)
        for path in paths
    )
    return WorkspaceObservation(ChangeGateReason.ALLOWED, paths, changes)


def _has_submodule_path(root: Path, paths: Sequence[str]) -> bool | None:
    data = _git_read_bytes(root, "ls-files", "--stage", "-z", "--", *paths)
    if data is None:
        return None
    if not data:
        return False
    if not data.endswith(b"\0"):
        return True
    try:
        records = data[:-1].decode("utf-8", "strict").split("\0")
    except UnicodeDecodeError:
        return True
    for record in records:
        if "\t" not in record:
            return True
        metadata, _path = record.split("\t", 1)
        parts = metadata.split(" ")
        if len(parts) != 3 or not parts[0].isdigit() or not _GIT_OID_RE.fullmatch(parts[1]):
            return True
        if parts[0] == "160000":
            return True
    return False


def _valid_observed_path(value: str) -> bool:
    if not value or "\0" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "." not in path.parts


def _read_attachment(
    row: sqlite3.Row,
    *,
    attachment_root: Path,
    expected_schema: str,
    expected_sha256: str | None = None,
):
    try:
        root = attachment_root.resolve(strict=True)
        stored = Path(row["stored_path"])
        if not stored.is_absolute() or stored.name != row["filename"]:
            raise ValueError
        relative = stored.relative_to(root).as_posix()
        if type(row["size"]) is not int or row["size"] > MAX_RUNTIME_ARTIFACT_BYTES:
            from hermes_cli.change_gate_codec import ArtifactCodecResult

            return ArtifactCodecResult(None, ArtifactCodecReason.OVERSIZED, byte_count=row["size"])
    except (OSError, ValueError):
        from hermes_cli.change_gate_codec import ArtifactCodecResult

        return ArtifactCodecResult(None, ArtifactCodecReason.PATH_TRAVERSAL)
    return read_artifact(
        root,
        relative,
        expected_schema=expected_schema,
        expected_sha256=expected_sha256,
    )


def _git_read(root: Path, *args: str) -> str | None:
    data = _git_read_bytes(root, *args)
    if data is None:
        return None
    try:
        value = data.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return None
    return value if value and "\0" not in value else None


def _git_read_bytes(root: Path, *args: str) -> bytes | None:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return None
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    overflow = False

    def _reader(stream, parts: list[bytes]) -> None:
        nonlocal overflow
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                parts.append(chunk)
                if sum(len(part) for part in parts) > _MAX_GIT_OUTPUT_BYTES:
                    overflow = True
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
        finally:
            try:
                stream.close()
            except OSError:
                pass

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(target=_reader, args=(process.stdout, stdout_parts))
    stderr_thread = threading.Thread(target=_reader, args=(process.stderr, stderr_parts))
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
        overflow = True
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        try:
            process.kill()
        except OSError:
            pass
        return None
    stdout = b"".join(stdout_parts)
    stderr = b"".join(stderr_parts)
    if (
        returncode != 0
        or overflow
        or len(stdout) > _MAX_GIT_OUTPUT_BYTES
        or len(stderr) > _MAX_GIT_OUTPUT_BYTES
    ):
        return None
    return stdout


def _normalize_repository(remote: str | None) -> str | None:
    if remote is None:
        return None
    value = remote.strip().removesuffix(".git").rstrip("/")
    if value.startswith("git@github.com:"):
        value = value[len("git@github.com:") :]
    elif "github.com/" in value:
        value = value.split("github.com/", 1)[1]
    else:
        return None
    parts = value.split("/")
    return "/".join(parts) if len(parts) == 2 and all(parts) else None


def _codec_reason(reason: ArtifactCodecReason) -> ChangeGateReason:
    if reason is ArtifactCodecReason.DIGEST_MISMATCH:
        return ChangeGateReason.ARTIFACT_DIGEST_MISMATCH
    if reason is ArtifactCodecReason.OVERSIZED:
        return ChangeGateReason.ARTIFACT_TOO_LARGE
    if reason in {ArtifactCodecReason.PATH_INVALID, ArtifactCodecReason.PATH_TRAVERSAL}:
        return ChangeGateReason.ARTIFACT_PATH_INVALID
    if reason is ArtifactCodecReason.FILE_NOT_REGULAR:
        return ChangeGateReason.ARTIFACT_SYMLINK
    if reason is ArtifactCodecReason.FILE_READ_FAILED:
        return ChangeGateReason.ARTIFACT_READ_FAILED
    return ChangeGateReason.ARTIFACT_BINDING_MISMATCH


def _failure(reason: ChangeGateReason, purpose: ReleasePurpose) -> ChangeGateResult:
    phase = GatePhase.G4_RELEASE if purpose is ReleasePurpose.G4 else GatePhase.G2_CLAIM
    if reason is ChangeGateReason.SCOPE_DEVIATION:
        return ChangeGateResult(GateDecision.SCOPE_DEVIATION, reason, phase)
    decision = (
        GateDecision.REPLAN_REQUIRED
        if reason in {
            ChangeGateReason.RUNTIME_CONFIG_INVALID,
            ChangeGateReason.INVENTORY_BINDING_MISSING,
            ChangeGateReason.INVENTORY_BINDING_MISMATCH,
            ChangeGateReason.INVENTORY_READ_FAILED,
            ChangeGateReason.ROUTE_POLICY_CONFLICT,
            ChangeGateReason.ARTIFACT_AMBIGUOUS,
        }
        else GateDecision.DENY
    )
    return ChangeGateResult(decision, reason, phase)


def _allow(reason: ChangeGateReason, phase: GatePhase) -> ChangeGateResult:
    return ChangeGateResult(GateDecision.ALLOW, reason, phase)


__all__ = [
    "ChangeGateRuntimePolicy",
    "EVIDENCE_ATTACHMENT_FILENAME",
    "FileArchitectureInventoryReader",
    "HANDOFF_ATTACHMENT_FILENAME",
    "REVIEW_METADATA_KEY",
    "ReviewClaimEvaluation",
    "ReviewProjection",
    "ReviewSubmission",
    "RuntimeEvaluation",
    "TaskGateArtifacts",
    "TaskGateLoad",
    "WorkspaceChangedPath",
    "WorkspaceObservation",
    "build_current_review_result",
    "build_review_claim_payload",
    "build_review_result_metadata",
    "count_change_gate_corrections",
    "evaluate_review_claim_runtime",
    "evaluate_loaded_runtime",
    "load_runtime_policy",
    "load_task_gate_artifacts",
    "observe_workspace_changes",
    "parse_review_submission",
    "project_upstream_reviews",
    "read_current_source_identity",
    "runtime_policy_from_mapping",
    "validate_bound_artifacts",
]
