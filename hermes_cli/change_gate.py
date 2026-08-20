"""Default-off Change Gate authority contracts.

The module owns only Change Gate-specific, deterministic validation. Existing
Kanban code continues to own task claiming, runs, events, retries, review
transport, and worker dispatch. The adapter is inert unless an internal caller
explicitly supplies an enabled instance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Mapping, Protocol, Sequence


EVIDENCE_PACKET_SCHEMA = "hermes.change-gate.evidence-packet/v1"
FROZEN_HANDOFF_SCHEMA = "hermes.change-gate.frozen-handoff/v1"
ARCHITECTURE_INVENTORY_SCHEMA = "hermes.change-gate.architecture-inventory/v1"
REVIEW_RESULT_SCHEMA = "hermes.change-gate.review-result/v1"
REVIEW_BUNDLE_SCHEMA = "hermes.change-gate.review-bundle/v1"
RELEASE_RECEIPT_SCHEMA = "hermes.change-gate.current-turn-release/v1"
CONVERGENCE_RULE = "exact-required-classes-pass-same-frozen-bundle/v1"
MAX_EVIDENCE_LIFETIME_SECONDS = 600


def _system_epoch_seconds() -> int:
    return int(time.time())


class RiskLevel(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class ReviewerClass(StrEnum):
    REVIEWER = "REVIEWER"
    NORMAL = "NORMAL"
    DEEP = "DEEP"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"


class ReleasePurpose(StrEnum):
    CLAIM = "CLAIM"
    G4 = "G4"


class GatePhase(StrEnum):
    G0_EVIDENCE = "G0_EVIDENCE"
    G1_FROZEN_HANDOFF = "G1_FROZEN_HANDOFF"
    G2_CLAIM = "G2_CLAIM"
    G3_REVIEW = "G3_REVIEW"
    G4_RELEASE = "G4_RELEASE"


class GateDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    SCOPE_DEVIATION = "SCOPE_DEVIATION"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"


class ChangeGateReason(StrEnum):
    DISABLED = "disabled"
    ALLOWED = "allowed"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_MALFORMED = "evidence_malformed"
    EVIDENCE_VERSION_UNSUPPORTED = "evidence_version_unsupported"
    EVIDENCE_EXPIRED = "evidence_expired"
    FROZEN_HANDOFF_MISSING = "frozen_handoff_missing"
    FROZEN_HANDOFF_MALFORMED = "frozen_handoff_malformed"
    FROZEN_HANDOFF_VERSION_UNSUPPORTED = "frozen_handoff_version_unsupported"
    EVIDENCE_DIGEST_MISMATCH = "evidence_digest_mismatch"
    HANDOFF_BINDING_MISMATCH = "handoff_binding_mismatch"
    REPOSITORY_MISMATCH = "repository_mismatch"
    BRANCH_MISMATCH = "branch_mismatch"
    COMMIT_MISMATCH = "commit_mismatch"
    TREE_MISMATCH = "tree_mismatch"
    TASK_ID_MISMATCH = "task_id_mismatch"
    RUN_ID_MISMATCH = "run_id_mismatch"
    WORK_ID_MISMATCH = "work_id_mismatch"
    OPERATION_MISMATCH = "operation_mismatch"
    EFFECT_MISMATCH = "effect_mismatch"
    ALLOWED_PATH_VIOLATION = "allowed_path_violation"
    ARTIFACT_BINDING_MISMATCH = "artifact_binding_mismatch"
    SCOPE_DEVIATION = "scope_deviation"
    ROUTE_POLICY_CONFLICT = "route_policy_conflict"
    ROUTE_PROJECTION_MISMATCH = "route_projection_mismatch"
    INVENTORY_BINDING_MISSING = "inventory_binding_missing"
    INVENTORY_BINDING_MISMATCH = "inventory_binding_mismatch"
    INVENTORY_READ_FAILED = "inventory_read_failed"
    RELEASE_RECEIPT_MISSING = "release_receipt_missing"
    RELEASE_RECEIPT_STALE = "release_receipt_stale"
    RELEASE_PURPOSE_UNSUPPORTED = "release_purpose_unsupported"
    REVIEW_RESULT_MALFORMED = "review_result_malformed"
    REVIEW_BUNDLE_MISMATCH = "review_bundle_mismatch"
    REVIEW_CLASS_UNEXPECTED = "review_class_unexpected"
    REVIEW_DUPLICATE_CLASS = "review_duplicate_class"
    REVIEW_IDENTITY_COLLISION = "review_identity_collision"
    REVIEW_MISSING_REQUIRED_CLASS = "review_missing_required_class"
    REVIEW_REQUEST_CHANGES = "review_request_changes"
    REVIEW_REPLAN_REQUIRED = "review_replan_required"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    repository: str
    branch: str
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class WorkIdentity:
    task_id: str
    run_id: str
    work_id: str
    operation: str
    effect: str


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    path: str
    sha256: str
    role: str
    git_oid: str | None = None


@dataclass(frozen=True, slots=True)
class UpstreamRouteSelector:
    """Existing Kanban task selector fields; this is not a model router."""

    assignee: str
    model_override: str | None = None
    provider_override: str | None = None
    reasoning_effort: str | None = None

    def normalized(self) -> UpstreamRouteSelector:
        model = _optional_text(self.model_override)
        provider = _optional_text(self.provider_override)
        if provider is not None and model is None:
            raise ValueError("provider_override_requires_model_override")

        # Reuse the upstream validator so this adapter does not create a second
        # reasoning-effort catalog.
        from hermes_cli.kanban_db import normalize_reasoning_effort

        return UpstreamRouteSelector(
            assignee=_required_text(self.assignee, "assignee"),
            model_override=model,
            provider_override=provider,
            reasoning_effort=normalize_reasoning_effort(self.reasoning_effort),
        )


@dataclass(frozen=True, slots=True)
class ReviewRoute:
    reviewer_class: ReviewerClass
    selector: UpstreamRouteSelector


@dataclass(frozen=True, slots=True)
class RouteProjection:
    risk: RiskLevel
    executor: UpstreamRouteSelector
    reviews: tuple[ReviewRoute, ...]

    @property
    def required_reviewers(self) -> tuple[ReviewerClass, ...]:
        return tuple(route.reviewer_class for route in self.reviews)


@dataclass(frozen=True, slots=True)
class ArchitectureInventoryRecord:
    schema: str
    inventory_id: str
    capability: str
    owner: str
    consumers: tuple[str, ...]
    authority_contract: str
    activation_state: str
    source_paths: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    risk: RiskLevel
    blast_radius: tuple[str, ...]

    def digest(self) -> str:
        return canonical_sha256(self)


class ArchitectureInventoryReader(Protocol):
    def read(self, inventory_id: str) -> ArchitectureInventoryRecord | None:
        """Read one externally owned inventory record without mutating it."""


@dataclass(frozen=True, slots=True)
class StaticArchitectureInventoryReader:
    """Immutable synthetic fixture; not a live inventory or registry."""

    records: tuple[ArchitectureInventoryRecord, ...]

    def read(self, inventory_id: str) -> ArchitectureInventoryRecord | None:
        matches = tuple(record for record in self.records if record.inventory_id == inventory_id)
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    source: SourceIdentity
    work: WorkIdentity
    risk: RiskLevel
    route: RouteProjection
    allowed_paths: tuple[str, ...]
    required_inputs: tuple[ArtifactBinding, ...]
    produced_artifacts: tuple[ArtifactBinding, ...]
    inventory_id: str
    created_at_epoch: int
    expires_at_epoch: int
    schema: str = EVIDENCE_PACKET_SCHEMA

    def digest(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class FrozenHandoff:
    evidence_sha256: str
    source: SourceIdentity
    work: WorkIdentity
    allowed_paths: tuple[str, ...]
    required_inputs: tuple[ArtifactBinding, ...]
    produced_artifacts: tuple[ArtifactBinding, ...]
    route: RouteProjection
    scope: tuple[str, ...]
    forbidden_effects: tuple[str, ...]
    inventory_id: str
    inventory_sha256: str
    inventory_owner: str
    inventory_consumer: str
    convergence_rule: str = CONVERGENCE_RULE
    claim_release_required: bool = True
    g4_release_required: bool = True
    schema: str = FROZEN_HANDOFF_SCHEMA

    def digest(self) -> str:
        return canonical_sha256(self)

    def review_bundle_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema": REVIEW_BUNDLE_SCHEMA,
                "frozen_handoff_sha256": self.digest(),
            }
        )


@dataclass(frozen=True, slots=True)
class ReviewResult:
    bundle_sha256: str
    reviewer_class: ReviewerClass
    reviewer_identity: str
    attempt_id: str
    verdict: ReviewVerdict
    finding_codes: tuple[str, ...]
    completed_at_epoch: int
    schema: str = REVIEW_RESULT_SCHEMA


@dataclass(frozen=True, slots=True)
class HumanReleaseReceipt:
    purpose: ReleasePurpose
    handoff_sha256: str
    action_fingerprint: str
    turn_id_sha256: str
    session_scope_sha256: str
    platform_scope_sha256: str
    user_message_index: int
    source_role: str
    schema: str = RELEASE_RECEIPT_SCHEMA

    def digest(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ChangeGateRequest:
    evidence: EvidencePacket | None
    frozen_handoff: FrozenHandoff | None
    release_receipt: HumanReleaseReceipt | None
    source: SourceIdentity
    work: WorkIdentity
    requested_paths: tuple[str, ...]
    observed_inputs: tuple[ArtifactBinding, ...]
    observed_outputs: tuple[ArtifactBinding, ...]
    reviews: tuple[ReviewResult, ...]
    purpose: ReleasePurpose = ReleasePurpose.CLAIM


@dataclass(frozen=True, slots=True)
class ChangeGateResult:
    decision: GateDecision
    reason: ChangeGateReason
    phase: GatePhase
    route: RouteProjection | None = None
    required_reviewers: tuple[ReviewerClass, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is GateDecision.ALLOW


@dataclass(frozen=True, slots=True)
class ChangeGateAdapter:
    enabled: bool = False
    inventory_reader: ArchitectureInventoryReader | None = None
    clock: Callable[[], int] = _system_epoch_seconds

    def evaluate(
        self,
        request: ChangeGateRequest | None,
        *,
        actual_task_id: str | None = None,
        actual_route: UpstreamRouteSelector | None = None,
    ) -> ChangeGateResult:
        """Evaluate a claim or G4 boundary without mutating domain state."""

        if self.enabled is not True:
            return _allow(ChangeGateReason.DISABLED, GatePhase.G0_EVIDENCE)
        if request is None or type(request) is not ChangeGateRequest:
            return _deny(ChangeGateReason.EVIDENCE_MISSING, GatePhase.G0_EVIDENCE)
        if request.evidence is None or type(request.evidence) is not EvidencePacket:
            return _deny(ChangeGateReason.EVIDENCE_MISSING, GatePhase.G0_EVIDENCE)
        if request.frozen_handoff is None or type(request.frozen_handoff) is not FrozenHandoff:
            return _deny(ChangeGateReason.FROZEN_HANDOFF_MISSING, GatePhase.G1_FROZEN_HANDOFF)

        evidence = request.evidence
        handoff = request.frozen_handoff
        if evidence.schema != EVIDENCE_PACKET_SCHEMA:
            return _deny(ChangeGateReason.EVIDENCE_VERSION_UNSUPPORTED, GatePhase.G0_EVIDENCE)
        if handoff.schema != FROZEN_HANDOFF_SCHEMA:
            return _deny(
                ChangeGateReason.FROZEN_HANDOFF_VERSION_UNSUPPORTED,
                GatePhase.G1_FROZEN_HANDOFF,
            )

        try:
            now_epoch = self.clock()
        except Exception:
            return _deny(ChangeGateReason.EVIDENCE_MALFORMED, GatePhase.G0_EVIDENCE)
        evidence_reason = _validate_evidence(evidence, now_epoch=now_epoch)
        if evidence_reason is not None:
            if evidence_reason is ChangeGateReason.ROUTE_POLICY_CONFLICT:
                return _replan(evidence_reason, GatePhase.G0_EVIDENCE)
            return _deny(evidence_reason, GatePhase.G0_EVIDENCE)
        handoff_reason = _validate_handoff(handoff)
        if handoff_reason is not None:
            return _deny(handoff_reason, GatePhase.G1_FROZEN_HANDOFF)

        try:
            evidence_sha256 = evidence.digest()
        except (TypeError, ValueError):
            return _deny(ChangeGateReason.EVIDENCE_MALFORMED, GatePhase.G0_EVIDENCE)
        if not hmac.compare_digest(handoff.evidence_sha256, evidence_sha256):
            return _deny(
                ChangeGateReason.EVIDENCE_DIGEST_MISMATCH,
                GatePhase.G1_FROZEN_HANDOFF,
            )

        binding_reason = _compare_handoff_to_evidence(handoff, evidence)
        if binding_reason is not None:
            return _deny(binding_reason, GatePhase.G1_FROZEN_HANDOFF)

        source_reason = _compare_source(request.source, evidence.source)
        if source_reason is not None:
            return _deny(source_reason, GatePhase.G2_CLAIM)
        work_reason = _compare_work(request.work, evidence.work)
        if work_reason is not None:
            return _deny(work_reason, GatePhase.G2_CLAIM)
        if type(actual_task_id) is not str or actual_task_id != evidence.work.task_id:
            return _deny(ChangeGateReason.TASK_ID_MISMATCH, GatePhase.G2_CLAIM)

        if not _valid_paths(request.requested_paths, allow_empty=False):
            return _scope_deviation(ChangeGateReason.ALLOWED_PATH_VIOLATION)
        if not set(request.requested_paths).issubset(evidence.allowed_paths):
            return _scope_deviation(ChangeGateReason.ALLOWED_PATH_VIOLATION)
        if request.observed_inputs != evidence.required_inputs:
            return _deny(ChangeGateReason.ARTIFACT_BINDING_MISMATCH, GatePhase.G2_CLAIM)
        if request.observed_outputs != evidence.produced_artifacts:
            return _deny(ChangeGateReason.ARTIFACT_BINDING_MISMATCH, GatePhase.G2_CLAIM)
        if request.work.effect not in handoff.scope:
            return _scope_deviation()
        if request.work.effect in handoff.forbidden_effects:
            return _scope_deviation()

        try:
            route = project_route(
                evidence.risk,
                evidence.route.executor,
                evidence.route.reviews,
            )
        except (TypeError, ValueError):
            return _replan(ChangeGateReason.ROUTE_POLICY_CONFLICT, GatePhase.G1_FROZEN_HANDOFF)
        if handoff.route != route:
            return _replan(
                ChangeGateReason.ROUTE_PROJECTION_MISMATCH,
                GatePhase.G1_FROZEN_HANDOFF,
                route=route,
            )
        try:
            normalized_actual_route = (
                actual_route.normalized()
                if type(actual_route) is UpstreamRouteSelector
                else None
            )
        except (TypeError, ValueError):
            normalized_actual_route = None
        if normalized_actual_route != route.executor:
            return _deny(ChangeGateReason.ROUTE_PROJECTION_MISMATCH, GatePhase.G2_CLAIM, route=route)

        inventory_result = self._validate_inventory(evidence, handoff)
        if inventory_result is not None:
            return inventory_result

        if request.purpose is ReleasePurpose.CLAIM:
            if request.release_receipt is None:
                return _deny(ChangeGateReason.RELEASE_RECEIPT_MISSING, GatePhase.G2_CLAIM, route=route)
            if not validate_current_turn_release_receipt(
                request.release_receipt,
                handoff_sha256=handoff.digest(),
                purpose=ReleasePurpose.CLAIM,
            ):
                return _deny(ChangeGateReason.RELEASE_RECEIPT_STALE, GatePhase.G2_CLAIM, route=route)
            return _allow(ChangeGateReason.ALLOWED, GatePhase.G2_CLAIM, route=route)

        if request.purpose is not ReleasePurpose.G4:
            return _deny(ChangeGateReason.RELEASE_PURPOSE_UNSUPPORTED, GatePhase.G2_CLAIM, route=route)

        review_result = evaluate_reviews(
            request.reviews,
            bundle_sha256=handoff.review_bundle_sha256(),
            required_reviewers=route.required_reviewers,
        )
        if not review_result.allowed:
            return ChangeGateResult(
                review_result.decision,
                review_result.reason,
                review_result.phase,
                route=route,
                required_reviewers=route.required_reviewers,
            )
        if request.release_receipt is None:
            return _deny(ChangeGateReason.RELEASE_RECEIPT_MISSING, GatePhase.G4_RELEASE, route=route)
        if not validate_current_turn_release_receipt(
            request.release_receipt,
            handoff_sha256=handoff.digest(),
            purpose=ReleasePurpose.G4,
        ):
            return _deny(ChangeGateReason.RELEASE_RECEIPT_STALE, GatePhase.G4_RELEASE, route=route)
        return _allow(ChangeGateReason.ALLOWED, GatePhase.G4_RELEASE, route=route)

    def _validate_inventory(
        self,
        evidence: EvidencePacket,
        handoff: FrozenHandoff,
    ) -> ChangeGateResult | None:
        if self.inventory_reader is None:
            return _replan(
                ChangeGateReason.INVENTORY_BINDING_MISSING,
                GatePhase.G1_FROZEN_HANDOFF,
                route=handoff.route,
            )
        try:
            record = self.inventory_reader.read(evidence.inventory_id)
        except Exception:
            return _replan(
                ChangeGateReason.INVENTORY_READ_FAILED,
                GatePhase.G1_FROZEN_HANDOFF,
                route=handoff.route,
            )
        if record is None or _validate_inventory_record(record) is not None:
            return _replan(
                ChangeGateReason.INVENTORY_BINDING_MISSING,
                GatePhase.G1_FROZEN_HANDOFF,
                route=handoff.route,
            )
        if (
            record.inventory_id != handoff.inventory_id
            or not hmac.compare_digest(record.digest(), handoff.inventory_sha256)
            or record.owner != handoff.inventory_owner
            or handoff.inventory_consumer not in record.consumers
            or record.risk is not evidence.risk
        ):
            return _replan(
                ChangeGateReason.INVENTORY_BINDING_MISMATCH,
                GatePhase.G1_FROZEN_HANDOFF,
                route=handoff.route,
            )
        return None


def project_route(
    risk: RiskLevel,
    executor: UpstreamRouteSelector,
    reviews: Sequence[ReviewRoute],
) -> RouteProjection:
    """Validate explicit policy-fixture values against upstream selectors."""

    if type(risk) is not RiskLevel or type(executor) is not UpstreamRouteSelector:
        raise ValueError("route_projection_invalid")
    if type(reviews) not in {tuple, list}:
        raise ValueError("review_routes_invalid")
    if any(type(route) is not ReviewRoute for route in reviews):
        raise ValueError("review_route_invalid")
    expected = _required_reviewer_classes(risk)
    if tuple(route.reviewer_class for route in reviews) != expected:
        raise ValueError("review_route_cardinality_invalid")
    return RouteProjection(
        risk=risk,
        executor=executor.normalized(),
        reviews=tuple(
            ReviewRoute(route.reviewer_class, route.selector.normalized()) for route in reviews
        ),
    )


def freeze_handoff(
    evidence: EvidencePacket,
    *,
    inventory: ArchitectureInventoryRecord,
    inventory_consumer: str,
    scope: Sequence[str],
    forbidden_effects: Sequence[str],
) -> FrozenHandoff:
    """Freeze authority inputs; later reviews and receipts are intentionally absent."""

    return FrozenHandoff(
        evidence_sha256=evidence.digest(),
        source=evidence.source,
        work=evidence.work,
        allowed_paths=evidence.allowed_paths,
        required_inputs=evidence.required_inputs,
        produced_artifacts=evidence.produced_artifacts,
        route=evidence.route,
        scope=tuple(scope),
        forbidden_effects=tuple(forbidden_effects),
        inventory_id=inventory.inventory_id,
        inventory_sha256=inventory.digest(),
        inventory_owner=inventory.owner,
        inventory_consumer=_required_text(inventory_consumer, "inventory_consumer"),
    )


def evaluate_reviews(
    reviews: Sequence[ReviewResult],
    *,
    bundle_sha256: str,
    required_reviewers: Sequence[ReviewerClass],
) -> ChangeGateResult:
    """Converge immutable review artifacts; persistence remains upstream-owned."""

    if not _is_sha256(bundle_sha256):
        return _deny(ChangeGateReason.REVIEW_BUNDLE_MISMATCH, GatePhase.G3_REVIEW)
    required = tuple(required_reviewers)
    if required not in {
        (ReviewerClass.REVIEWER,),
        (ReviewerClass.NORMAL, ReviewerClass.DEEP),
    }:
        return _replan(ChangeGateReason.ROUTE_POLICY_CONFLICT, GatePhase.G3_REVIEW)
    if type(reviews) not in {tuple, list}:
        return _deny(ChangeGateReason.REVIEW_RESULT_MALFORMED, GatePhase.G3_REVIEW)

    current: list[ReviewResult] = []
    for review in reviews:
        if type(review) is not ReviewResult or not _valid_review(review):
            return _deny(ChangeGateReason.REVIEW_RESULT_MALFORMED, GatePhase.G3_REVIEW)
        if hmac.compare_digest(review.bundle_sha256, bundle_sha256):
            current.append(review)

    seen_classes: set[ReviewerClass] = set()
    seen_identities: set[str] = set()
    for review in current:
        if review.reviewer_class not in required:
            return _deny(ChangeGateReason.REVIEW_CLASS_UNEXPECTED, GatePhase.G3_REVIEW)
        if review.reviewer_class in seen_classes:
            return _deny(ChangeGateReason.REVIEW_DUPLICATE_CLASS, GatePhase.G3_REVIEW)
        if review.reviewer_identity in seen_identities:
            return _deny(ChangeGateReason.REVIEW_IDENTITY_COLLISION, GatePhase.G3_REVIEW)
        seen_classes.add(review.reviewer_class)
        seen_identities.add(review.reviewer_identity)

    if any(review.verdict is ReviewVerdict.REPLAN_REQUIRED for review in current):
        return _replan(ChangeGateReason.REVIEW_REPLAN_REQUIRED, GatePhase.G3_REVIEW)
    if any(review.verdict is ReviewVerdict.REQUEST_CHANGES for review in current):
        return ChangeGateResult(
            GateDecision.REQUEST_CHANGES,
            ChangeGateReason.REVIEW_REQUEST_CHANGES,
            GatePhase.G3_REVIEW,
            required_reviewers=required,
        )
    if any(reviewer not in seen_classes for reviewer in required):
        return ChangeGateResult(
            GateDecision.DENY,
            ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS,
            GatePhase.G3_REVIEW,
            required_reviewers=required,
        )
    return ChangeGateResult(
        GateDecision.ALLOW,
        ChangeGateReason.ALLOWED,
        GatePhase.G3_REVIEW,
        required_reviewers=required,
    )


def expected_release_statement(*, purpose: ReleasePurpose, handoff_sha256: str) -> str:
    if type(purpose) is not ReleasePurpose:
        raise ValueError("release_purpose_invalid")
    return f"AUTHORIZE_HERMES_CHANGE_GATE_{purpose.value} {_required_sha256(handoff_sha256)}"


def issue_current_turn_release_receipt(
    *,
    purpose: ReleasePurpose,
    handoff_sha256: str,
) -> HumanReleaseReceipt | None:
    """Issue raw-free metadata only from the existing foreground host seam."""

    from gateway.session_context import (
        get_session_controller_role,
        get_trusted_current_user_text,
    )
    from tools.workflow_authority import (
        get_current_turn_user_authority,
        matches_active_workflow_turn,
        matches_current_workflow_session,
    )

    try:
        expected = expected_release_statement(
            purpose=purpose,
            handoff_sha256=handoff_sha256,
        )
    except (TypeError, ValueError):
        return None
    authority = get_current_turn_user_authority()
    if (
        authority is None
        or get_trusted_current_user_text() != expected
        or get_session_controller_role() != "main_controller"
        or not matches_active_workflow_turn(authority, user_message=expected)
        or not matches_current_workflow_session(authority)
    ):
        return None
    return HumanReleaseReceipt(
        purpose=purpose,
        handoff_sha256=handoff_sha256,
        action_fingerprint=authority.user_action_fingerprint,
        turn_id_sha256=_identity_sha256("turn", authority.turn_id),
        session_scope_sha256=_identity_sha256("session", authority.session_scope),
        platform_scope_sha256=_identity_sha256("platform", authority.platform_scope),
        user_message_index=authority.user_message_index,
        source_role=authority.source_role,
    )


def validate_current_turn_release_receipt(
    receipt: HumanReleaseReceipt,
    *,
    handoff_sha256: str,
    purpose: ReleasePurpose,
) -> bool:
    """Require the receipt to match the still-live exact host turn."""

    from gateway.session_context import (
        get_session_controller_role,
        get_trusted_current_user_text,
    )
    from tools.workflow_authority import (
        get_current_turn_user_authority,
        matches_active_workflow_turn,
        matches_current_workflow_session,
    )

    try:
        expected = expected_release_statement(
            purpose=purpose,
            handoff_sha256=handoff_sha256,
        )
    except (TypeError, ValueError):
        return False
    authority = get_current_turn_user_authority()
    if authority is None or type(receipt) is not HumanReleaseReceipt:
        return False
    if (
        receipt.schema != RELEASE_RECEIPT_SCHEMA
        or type(receipt.purpose) is not ReleasePurpose
        or not _is_sha256(receipt.handoff_sha256)
        or not _is_sha256(receipt.action_fingerprint)
        or not _is_sha256(receipt.turn_id_sha256)
        or not _is_sha256(receipt.session_scope_sha256)
        or not _is_sha256(receipt.platform_scope_sha256)
        or type(receipt.user_message_index) is not int
        or receipt.user_message_index < 0
        or receipt.source_role != "user"
    ):
        return False
    return (
        receipt.purpose is purpose
        and hmac.compare_digest(receipt.handoff_sha256, handoff_sha256)
        and hmac.compare_digest(receipt.action_fingerprint, authority.user_action_fingerprint)
        and hmac.compare_digest(receipt.turn_id_sha256, _identity_sha256("turn", authority.turn_id))
        and hmac.compare_digest(
            receipt.session_scope_sha256,
            _identity_sha256("session", authority.session_scope),
        )
        and hmac.compare_digest(
            receipt.platform_scope_sha256,
            _identity_sha256("platform", authority.platform_scope),
        )
        and receipt.user_message_index == authority.user_message_index
        and receipt.source_role == authority.source_role == "user"
        and get_trusted_current_user_text() == expected
        and get_session_controller_role() == "main_controller"
        and matches_active_workflow_turn(authority, user_message=expected)
        and matches_current_workflow_session(authority)
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _plain(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("canonical_mapping_key_invalid")
        return {key: _plain(item) for key, item in value.items()}
    if type(value) is tuple:
        assert isinstance(value, tuple)
        return [_plain(item) for item in value]
    if type(value) is list:
        assert isinstance(value, list)
        return [_plain(item) for item in value]
    if type(value) is str:
        return value
    if value is None or type(value) in {bool, int}:
        return value
    raise TypeError(f"canonical_type_invalid:{type(value).__name__}")


def _validate_evidence(
    evidence: EvidencePacket,
    *,
    now_epoch: int,
) -> ChangeGateReason | None:
    if (
        _validate_source(evidence.source) is not None
        or _validate_work(evidence.work) is not None
        or type(evidence.risk) is not RiskLevel
        or type(evidence.route) is not RouteProjection
        or evidence.route.risk is not evidence.risk
        or not _valid_paths(evidence.allowed_paths, allow_empty=False)
        or not _valid_artifacts(evidence.required_inputs)
        or not _valid_artifacts(evidence.produced_artifacts)
        or not _valid_text(evidence.inventory_id)
        or type(evidence.created_at_epoch) is not int
        or type(evidence.expires_at_epoch) is not int
        or type(now_epoch) is not int
    ):
        return ChangeGateReason.EVIDENCE_MALFORMED
    lifetime = evidence.expires_at_epoch - evidence.created_at_epoch
    if lifetime < 1 or lifetime > MAX_EVIDENCE_LIFETIME_SECONDS:
        return ChangeGateReason.EVIDENCE_MALFORMED
    if now_epoch < evidence.created_at_epoch or now_epoch >= evidence.expires_at_epoch:
        return ChangeGateReason.EVIDENCE_EXPIRED
    try:
        normalized = project_route(evidence.risk, evidence.route.executor, evidence.route.reviews)
    except (TypeError, ValueError):
        return ChangeGateReason.ROUTE_POLICY_CONFLICT
    if normalized != evidence.route:
        return ChangeGateReason.EVIDENCE_MALFORMED
    return None


def _validate_handoff(handoff: FrozenHandoff) -> ChangeGateReason | None:
    if (
        not _is_sha256(handoff.evidence_sha256)
        or _validate_source(handoff.source) is not None
        or _validate_work(handoff.work) is not None
        or not _valid_paths(handoff.allowed_paths, allow_empty=False)
        or not _valid_artifacts(handoff.required_inputs)
        or not _valid_artifacts(handoff.produced_artifacts)
        or type(handoff.route) is not RouteProjection
        or not _valid_sorted_texts(handoff.scope, allow_empty=False)
        or not _valid_sorted_texts(handoff.forbidden_effects, allow_empty=False)
        or not _valid_text(handoff.inventory_id)
        or not _is_sha256(handoff.inventory_sha256)
        or not _valid_text(handoff.inventory_owner)
        or not _valid_text(handoff.inventory_consumer)
        or handoff.convergence_rule != CONVERGENCE_RULE
        or handoff.claim_release_required is not True
        or handoff.g4_release_required is not True
    ):
        return ChangeGateReason.FROZEN_HANDOFF_MALFORMED
    try:
        normalized = project_route(handoff.route.risk, handoff.route.executor, handoff.route.reviews)
    except (TypeError, ValueError):
        return ChangeGateReason.FROZEN_HANDOFF_MALFORMED
    if normalized != handoff.route:
        return ChangeGateReason.FROZEN_HANDOFF_MALFORMED
    return None


def _validate_inventory_record(record: ArchitectureInventoryRecord) -> ChangeGateReason | None:
    if (
        type(record) is not ArchitectureInventoryRecord
        or record.schema != ARCHITECTURE_INVENTORY_SCHEMA
        or not _valid_text(record.inventory_id)
        or not _valid_text(record.capability)
        or not _valid_text(record.owner)
        or not _valid_sorted_texts(record.consumers, allow_empty=False)
        or not _valid_text(record.authority_contract)
        or not _valid_text(record.activation_state)
        or not _valid_paths(record.source_paths, allow_empty=False)
        or not _valid_paths(record.artifact_paths, allow_empty=False)
        or type(record.risk) is not RiskLevel
        or not _valid_sorted_texts(record.blast_radius, allow_empty=False)
    ):
        return ChangeGateReason.INVENTORY_BINDING_MISMATCH
    return None


def _compare_handoff_to_evidence(
    handoff: FrozenHandoff,
    evidence: EvidencePacket,
) -> ChangeGateReason | None:
    if handoff.source != evidence.source or handoff.work != evidence.work:
        return ChangeGateReason.HANDOFF_BINDING_MISMATCH
    if handoff.allowed_paths != evidence.allowed_paths:
        return ChangeGateReason.HANDOFF_BINDING_MISMATCH
    if (
        handoff.required_inputs != evidence.required_inputs
        or handoff.produced_artifacts != evidence.produced_artifacts
    ):
        return ChangeGateReason.HANDOFF_BINDING_MISMATCH
    if handoff.route != evidence.route or handoff.inventory_id != evidence.inventory_id:
        return ChangeGateReason.HANDOFF_BINDING_MISMATCH
    return None


def _compare_source(
    actual: SourceIdentity,
    expected: SourceIdentity,
) -> ChangeGateReason | None:
    if type(actual) is not SourceIdentity:
        return ChangeGateReason.REPOSITORY_MISMATCH
    if actual.repository != expected.repository:
        return ChangeGateReason.REPOSITORY_MISMATCH
    if actual.branch != expected.branch:
        return ChangeGateReason.BRANCH_MISMATCH
    if actual.commit != expected.commit:
        return ChangeGateReason.COMMIT_MISMATCH
    if actual.tree != expected.tree:
        return ChangeGateReason.TREE_MISMATCH
    return None


def _compare_work(
    actual: WorkIdentity,
    expected: WorkIdentity,
) -> ChangeGateReason | None:
    if type(actual) is not WorkIdentity or actual.task_id != expected.task_id:
        return ChangeGateReason.TASK_ID_MISMATCH
    if actual.run_id != expected.run_id:
        return ChangeGateReason.RUN_ID_MISMATCH
    if actual.work_id != expected.work_id:
        return ChangeGateReason.WORK_ID_MISMATCH
    if actual.operation != expected.operation:
        return ChangeGateReason.OPERATION_MISMATCH
    if actual.effect != expected.effect:
        return ChangeGateReason.EFFECT_MISMATCH
    return None


def _validate_source(source: SourceIdentity) -> ChangeGateReason | None:
    if (
        type(source) is not SourceIdentity
        or not _valid_text(source.repository)
        or not _valid_text(source.branch)
        or not _is_git_oid(source.commit)
        or not _is_git_oid(source.tree)
    ):
        return ChangeGateReason.EVIDENCE_MALFORMED
    return None


def _validate_work(work: WorkIdentity) -> ChangeGateReason | None:
    if type(work) is not WorkIdentity or any(
        not _valid_text(value)
        for value in (work.task_id, work.run_id, work.work_id, work.operation, work.effect)
    ):
        return ChangeGateReason.EVIDENCE_MALFORMED
    return None


def _valid_review(review: ReviewResult) -> bool:
    if (
        review.schema != REVIEW_RESULT_SCHEMA
        or not _is_sha256(review.bundle_sha256)
        or type(review.reviewer_class) is not ReviewerClass
        or not _valid_text(review.reviewer_identity)
        or not _valid_text(review.attempt_id)
        or type(review.verdict) is not ReviewVerdict
        or not _valid_sorted_texts(review.finding_codes, allow_empty=True)
        or type(review.completed_at_epoch) is not int
        or review.completed_at_epoch < 0
    ):
        return False
    if review.verdict is ReviewVerdict.PASS:
        return not review.finding_codes
    return bool(review.finding_codes)


def _valid_artifacts(bindings: object) -> bool:
    if type(bindings) is not tuple:
        return False
    keys: list[tuple[str, str, str, str]] = []
    for binding in bindings:
        if (
            type(binding) is not ArtifactBinding
            or not _valid_path(binding.path)
            or not _is_sha256(binding.sha256)
            or not _valid_text(binding.role)
            or (binding.git_oid is not None and not _is_git_oid(binding.git_oid))
        ):
            return False
        keys.append((binding.role, binding.path, binding.sha256, binding.git_oid or ""))
    return len(keys) == len(set(keys)) and keys == sorted(keys)


def _valid_paths(paths: object, *, allow_empty: bool) -> bool:
    if type(paths) is not tuple or (not allow_empty and not paths):
        return False
    return (
        all(_valid_path(path) for path in paths)
        and len(paths) == len(set(paths))
        and tuple(sorted(paths)) == paths
    )


def _valid_path(path: object) -> bool:
    if not _valid_text(path):
        return False
    assert isinstance(path, str)
    if "\\" in path:
        return False
    candidate = PurePosixPath(path)
    return (
        not candidate.is_absolute()
        and path == candidate.as_posix()
        and path not in {".", ".."}
        and ".." not in candidate.parts
    )


def _valid_sorted_texts(values: object, *, allow_empty: bool) -> bool:
    if type(values) is not tuple or (not allow_empty and not values):
        return False
    return (
        all(_valid_text(value) for value in values)
        and len(values) == len(set(values))
        and tuple(sorted(values)) == values
    )


def _valid_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and "\0" not in value
    )


def _required_text(value: object, name: str) -> str:
    if not _valid_text(value):
        raise ValueError(f"{name}_invalid")
    assert type(value) is str
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value, "optional_text")


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _required_sha256(value: object) -> str:
    if not _is_sha256(value):
        raise ValueError("sha256_invalid")
    assert type(value) is str
    return value


def _is_git_oid(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in {40, 64}
        and all(char in "0123456789abcdef" for char in value)
    )


def _identity_sha256(domain: str, value: str) -> str:
    return hashlib.sha256(f"{domain}\0{value}".encode("utf-8")).hexdigest()


def _required_reviewer_classes(risk: RiskLevel) -> tuple[ReviewerClass, ...]:
    if risk in {RiskLevel.LOW, RiskLevel.NORMAL}:
        return (ReviewerClass.REVIEWER,)
    if risk is RiskLevel.HIGH:
        return (ReviewerClass.NORMAL, ReviewerClass.DEEP)
    raise ValueError("risk_invalid")


def _allow(
    reason: ChangeGateReason,
    phase: GatePhase,
    *,
    route: RouteProjection | None = None,
) -> ChangeGateResult:
    return ChangeGateResult(
        GateDecision.ALLOW,
        reason,
        phase,
        route=route,
        required_reviewers=route.required_reviewers if route else (),
    )


def _deny(
    reason: ChangeGateReason,
    phase: GatePhase,
    *,
    route: RouteProjection | None = None,
) -> ChangeGateResult:
    return ChangeGateResult(
        GateDecision.DENY,
        reason,
        phase,
        route=route,
        required_reviewers=route.required_reviewers if route else (),
    )


def _replan(
    reason: ChangeGateReason,
    phase: GatePhase,
    *,
    route: RouteProjection | None = None,
) -> ChangeGateResult:
    return ChangeGateResult(
        GateDecision.REPLAN_REQUIRED,
        reason,
        phase,
        route=route,
        required_reviewers=route.required_reviewers if route else (),
    )


def _scope_deviation(
    reason: ChangeGateReason = ChangeGateReason.SCOPE_DEVIATION,
) -> ChangeGateResult:
    return ChangeGateResult(
        GateDecision.SCOPE_DEVIATION,
        reason,
        GatePhase.G2_CLAIM,
    )


__all__ = [
    "ARCHITECTURE_INVENTORY_SCHEMA",
    "ArchitectureInventoryReader",
    "ArchitectureInventoryRecord",
    "ArtifactBinding",
    "ChangeGateAdapter",
    "ChangeGateReason",
    "ChangeGateRequest",
    "ChangeGateResult",
    "EvidencePacket",
    "FrozenHandoff",
    "GateDecision",
    "GatePhase",
    "HumanReleaseReceipt",
    "ReleasePurpose",
    "ReviewResult",
    "ReviewRoute",
    "ReviewerClass",
    "ReviewVerdict",
    "RiskLevel",
    "RouteProjection",
    "SourceIdentity",
    "StaticArchitectureInventoryReader",
    "UpstreamRouteSelector",
    "WorkIdentity",
    "canonical_sha256",
    "evaluate_reviews",
    "expected_release_statement",
    "freeze_handoff",
    "issue_current_turn_release_receipt",
    "project_route",
    "validate_current_turn_release_receipt",
]
