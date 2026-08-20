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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

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
    block = config.get("change_gate")
    if type(block) is not dict or block.get("enabled") is not True:
        return ChangeGateRuntimePolicy()
    if set(block) - _CONFIG_KEYS:
        return ChangeGateRuntimePolicy(enabled=True, valid=False)

    root_raw = block.get("inventory_root")
    ttl = block.get("release_ttl_seconds", DEFAULT_RELEASE_TTL_SECONDS)
    ungated = block.get("ungated_policy", "passthrough")
    planner = block.get("planner_assignee", "planner")
    max_corrections = block.get("max_corrections", DEFAULT_MAX_CORRECTIONS)
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
    return ChangeGateRuntimePolicy(
        enabled=True,
        valid=True,
        inventory_root=Path(root_raw),
        release_ttl_seconds=ttl,
        ungated_policy=ungated,
        planner_assignee=planner,
        max_corrections=max_corrections,
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

    source_result = read_current_source_identity(task_row["workspace_path"], evidence.source.repository)
    if isinstance(source_result, ChangeGateReason):
        return TaskGateLoad(True, source_result)
    artifact_reason = validate_bound_artifacts(
        Path(task_row["workspace_path"]),
        evidence.required_inputs + evidence.produced_artifacts,
    )
    if artifact_reason is not ChangeGateReason.ALLOWED:
        return TaskGateLoad(True, artifact_reason)
    return TaskGateLoad(
        True,
        ChangeGateReason.ALLOWED,
        TaskGateArtifacts(
            evidence=evidence,
            handoff=handoff,
            inventory=inventory,
            actual_source=source_result,
            actual_route=actual_route,
            evidence_attachment_sha256=evidence_read.sha256 or "",
            handoff_attachment_sha256=handoff_read.sha256 or "",
        ),
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
        return RuntimeEvaluation(True, _failure(load.reason, purpose))
    if release is None:
        return RuntimeEvaluation(True, _failure(release_reason, purpose), load.artifacts)

    artifacts = load.artifacts
    request = ChangeGateRequest(
        evidence=artifacts.evidence,
        frozen_handoff=artifacts.handoff,
        release_receipt=None,
        source=artifacts.actual_source,
        work=artifacts.evidence.work,
        requested_paths=artifacts.evidence.allowed_paths,
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
    if aggregate.reason is not ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS:
        return ReviewClaimEvaluation(True, False, aggregate.reason, artifacts)

    completed = {review.reviewer_class for review in projection.reviews}
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
    try:
        reviewer_class = ReviewerClass(value["reviewer_class"])
        verdict = ReviewVerdict(value["verdict"])
    except (TypeError, ValueError):
        return None
    raw_codes = value["finding_codes"]
    if type(raw_codes) is not list or len(raw_codes) > 32:
        return None
    if any(type(code) is not str or _FINDING_CODE_RE.fullmatch(code) is None for code in raw_codes):
        return None
    finding_codes = tuple(raw_codes)
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
    """Project only exact metadata linked to completed upstream review runs."""

    if type(handoff) is not FrozenHandoff:
        return ReviewProjection((), ChangeGateReason.FROZEN_HANDOFF_MALFORMED)
    bundle_sha256 = handoff.review_bundle_sha256()

    rows = conn.execute(
        "SELECT id, profile, ended_at, outcome, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL AND metadata IS NOT NULL "
        "ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    projected: list[ReviewResult] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            if REVIEW_METADATA_KEY in str(row["metadata"]):
                return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
            continue
        if type(metadata) is not dict or REVIEW_METADATA_KEY not in metadata:
            continue
        raw = metadata[REVIEW_METADATA_KEY]
        if type(raw) is not str:
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        decoded = decode_artifact(raw.encode("utf-8"), expected_schema=REVIEW_RESULT_SCHEMA)
        if not decoded.ok or type(decoded.value) is not ReviewResult:
            return ReviewProjection((), ChangeGateReason.REVIEW_RESULT_MALFORMED)
        review = decoded.value
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
        if review.bundle_sha256 == bundle_sha256:
            projected.append(review)
    return ReviewProjection(tuple(projected))


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
    if None in {commit, tree, branch, repository} or repository != expected_repository:
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
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES:
        return None
    try:
        value = completed.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        return None
    return value if value and "\0" not in value else None


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
    decision = (
        GateDecision.REPLAN_REQUIRED
        if reason in {
            ChangeGateReason.RUNTIME_CONFIG_INVALID,
            ChangeGateReason.INVENTORY_BINDING_MISSING,
            ChangeGateReason.INVENTORY_BINDING_MISMATCH,
            ChangeGateReason.INVENTORY_READ_FAILED,
            ChangeGateReason.ROUTE_POLICY_CONFLICT,
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
    "ReviewProjection",
    "RuntimeEvaluation",
    "TaskGateArtifacts",
    "TaskGateLoad",
    "build_review_result_metadata",
    "evaluate_loaded_runtime",
    "load_runtime_policy",
    "load_task_gate_artifacts",
    "project_upstream_reviews",
    "read_current_source_identity",
    "runtime_policy_from_mapping",
    "validate_bound_artifacts",
]
