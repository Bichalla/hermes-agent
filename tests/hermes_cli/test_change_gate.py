"""Behavioral tests for the default-off Change Gate adapter."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    ArchitectureInventoryRecord,
    ArtifactBinding,
    ChangeGateAdapter,
    ChangeGateReason,
    ChangeGateRequest,
    DurableReleaseArtifact,
    EvidencePacket,
    FrozenHandoff,
    HumanReleaseReceipt,
    GateDecision,
    GatePhase,
    MAX_EVIDENCE_LIFETIME_SECONDS,
    MAX_RELEASE_LIFETIME_SECONDS,
    ReleasePurpose,
    ReviewResult,
    ReviewRoute,
    ReviewerClass,
    ReviewVerdict,
    RiskLevel,
    SourceIdentity,
    StaticArchitectureInventoryReader,
    TransitionAnchor,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_sha256,
    evaluate_reviews,
    expected_release_statement,
    freeze_handoff,
    issue_durable_release_artifact,
    issue_current_turn_release_receipt,
    project_route,
    request_from_durable_release,
    validate_current_turn_release_receipt,
    validate_durable_release_artifact,
)
from tools.workflow_authority import _scoped_test_current_turn_user_authority


NOW = 1_000_050
SOURCE = SourceIdentity(
    repository="Bichalla/hermes-agent",
    branch="track-g/synthetic",
    commit="a" * 40,
    tree="b" * 40,
)
INPUTS = (
    ArtifactBinding(
        path="control/input.json",
        sha256="1" * 64,
        role="input",
        git_oid="c" * 40,
    ),
)
OUTPUTS = (
    ArtifactBinding(
        path="results/output.json",
        sha256="2" * 64,
        role="output",
    ),
)
ALLOWED_PATHS = (
    "hermes_cli/change_gate.py",
    "hermes_cli/kanban_db.py",
)


@dataclass(frozen=True)
class SyntheticCase:
    evidence: EvidencePacket
    handoff: FrozenHandoff
    inventory: ArchitectureInventoryRecord
    adapter: ChangeGateAdapter


def _selector(
    assignee: str,
    *,
    model: str | None = "fixture-model",
    provider: str | None = "fixture-provider",
    effort: str | None = "medium",
) -> UpstreamRouteSelector:
    return UpstreamRouteSelector(
        assignee=assignee,
        model_override=model,
        provider_override=provider,
        reasoning_effort=effort,
    )


def _route(risk: RiskLevel, *, executor_assignee: str = "executor"):
    executor = _selector(executor_assignee)
    if risk is RiskLevel.HIGH:
        reviews = (
            ReviewRoute(ReviewerClass.NORMAL, _selector("review-normal", effort="high")),
            ReviewRoute(ReviewerClass.DEEP, _selector("review-deep", effort="high")),
        )
    else:
        reviews = (
            ReviewRoute(ReviewerClass.REVIEWER, _selector("reviewer", effort="high")),
        )
    return project_route(risk, executor, reviews)


def _case(
    risk: RiskLevel = RiskLevel.NORMAL,
    *,
    task_id: str = "task-synthetic",
    executor_assignee: str = "executor",
) -> SyntheticCase:
    work = WorkIdentity(
        task_id=task_id,
        run_id="preclaim-run-key",
        work_id="work-synthetic",
        operation="apply-source-candidate",
        effect="SOURCE_CHANGE",
    )
    route = _route(risk, executor_assignee=executor_assignee)
    evidence = EvidencePacket(
        source=SOURCE,
        work=work,
        risk=risk,
        route=route,
        allowed_paths=ALLOWED_PATHS,
        required_inputs=INPUTS,
        produced_artifacts=OUTPUTS,
        inventory_id="track-g-change-gate",
        created_at_epoch=NOW - 50,
        expires_at_epoch=NOW + 50,
    )
    inventory = ArchitectureInventoryRecord(
        schema=ARCHITECTURE_INVENTORY_SCHEMA,
        inventory_id=evidence.inventory_id,
        capability="change-gate-authority-adapter",
        owner="change-gate-contract-owner",
        consumers=("change-gate-claim-adapter", "change-gate-g4"),
        authority_contract="current-turn-frozen-handoff",
        activation_state="DEFAULT_OFF",
        source_paths=("hermes_cli/change_gate.py", "hermes_cli/kanban_db.py"),
        artifact_paths=("results/change-gate.json",),
        risk=risk,
        blast_radius=("claim-only", "default-off"),
    )
    handoff = freeze_handoff(
        evidence,
        inventory=inventory,
        inventory_consumer="change-gate-claim-adapter",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION", "PRIVATE_STATE_READ"),
    )
    adapter = ChangeGateAdapter(
        enabled=True,
        inventory_reader=StaticArchitectureInventoryReader((inventory,)),
        clock=lambda: NOW,
    )
    return SyntheticCase(evidence, handoff, inventory, adapter)


def _request(
    case: SyntheticCase,
    *,
    purpose: ReleasePurpose = ReleasePurpose.CLAIM,
    receipt=None,
    reviews: tuple[ReviewResult, ...] = (),
) -> ChangeGateRequest:
    return ChangeGateRequest(
        evidence=case.evidence,
        frozen_handoff=case.handoff,
        release_receipt=receipt,
        source=case.evidence.source,
        work=case.evidence.work,
        requested_paths=case.evidence.allowed_paths,
        observed_inputs=case.evidence.required_inputs,
        observed_outputs=case.evidence.produced_artifacts,
        reviews=reviews,
        purpose=purpose,
    )


@contextmanager
def _released_request(
    case: SyntheticCase,
    purpose: ReleasePurpose,
    *,
    reviews: tuple[ReviewResult, ...] = (),
    turn_id: str = "turn-release",
):
    statement = expected_release_statement(
        purpose=purpose,
        handoff_sha256=case.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="session-change-gate",
        turn_id=turn_id,
        platform_scope="manual",
    ):
        receipt = issue_current_turn_release_receipt(
            purpose=purpose,
            handoff_sha256=case.handoff.digest(),
        )
        assert receipt is not None
        yield _request(case, purpose=purpose, receipt=receipt, reviews=reviews), receipt


def _evaluate(case: SyntheticCase, request: ChangeGateRequest):
    return case.adapter.evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )


def _review(
    case: SyntheticCase,
    reviewer_class: ReviewerClass,
    identity: str,
    *,
    verdict: ReviewVerdict = ReviewVerdict.PASS,
    bundle_sha256: str | None = None,
) -> ReviewResult:
    findings = () if verdict is ReviewVerdict.PASS else ("bounded-finding",)
    return ReviewResult(
        bundle_sha256=bundle_sha256 or case.handoff.review_bundle_sha256(),
        reviewer_class=reviewer_class,
        reviewer_identity=identity,
        attempt_id=f"attempt-{reviewer_class.value.lower()}",
        verdict=verdict,
        finding_codes=findings,
        completed_at_epoch=NOW,
    )


def _claim_transition_anchor(
    case: SyntheticCase,
    *,
    latest_event_id: int = 1,
    latest_event_kind: str = "task_ready",
    latest_event_payload_sha256: str = "9" * 64,
) -> TransitionAnchor:
    return TransitionAnchor(
        task_id=case.evidence.work.task_id,
        purpose=ReleasePurpose.CLAIM,
        status="ready",
        route_sha256=canonical_sha256(case.handoff.route.executor),
        workspace_source_sha256="8" * 64,
        current_run_id=None,
        latest_event_id=latest_event_id,
        latest_event_kind=latest_event_kind,
        latest_event_payload_sha256=latest_event_payload_sha256,
        frozen_handoff_sha256=case.handoff.digest(),
        review_bundle_sha256=None,
        selected_review_set_sha256=None,
    )


def _g4_transition_anchor(
    case: SyntheticCase,
    reviews: tuple[ReviewResult, ...],
    *,
    latest_event_id: int = 2,
) -> TransitionAnchor:
    return TransitionAnchor(
        task_id=case.evidence.work.task_id,
        purpose=ReleasePurpose.G4,
        status="review",
        route_sha256=canonical_sha256(case.handoff.route.executor),
        workspace_source_sha256="8" * 64,
        current_run_id=None,
        latest_event_id=latest_event_id,
        latest_event_kind="review_requested",
        latest_event_payload_sha256="a" * 64,
        frozen_handoff_sha256=case.handoff.digest(),
        review_bundle_sha256=case.handoff.review_bundle_sha256(),
        selected_review_set_sha256=canonical_sha256(reviews),
    )


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        (RiskLevel.LOW, (ReviewerClass.REVIEWER,)),
        (RiskLevel.NORMAL, (ReviewerClass.REVIEWER,)),
        (RiskLevel.HIGH, (ReviewerClass.NORMAL, ReviewerClass.DEEP)),
    ],
)
def test_risk_projection_uses_upstream_selectors_and_accepted_cardinality(risk, expected):
    route = _route(risk)

    assert route.required_reviewers == expected
    assert route.executor.model_override == "fixture-model"
    assert route.executor.provider_override == "fixture-provider"
    assert route.executor.reasoning_effort == "medium"


def test_conflicting_route_fixture_requires_replan():
    with pytest.raises(ValueError, match="cardinality"):
        project_route(
            RiskLevel.HIGH,
            _selector("executor"),
            (ReviewRoute(ReviewerClass.REVIEWER, _selector("reviewer")),),
        )

    case = _case(RiskLevel.HIGH)
    malformed = replace(
        case.evidence.route,
        reviews=(ReviewRoute(ReviewerClass.REVIEWER, _selector("reviewer")),),
    )
    evidence = replace(case.evidence, route=malformed)
    request = replace(_request(case), evidence=evidence)
    result = _evaluate(case, request)
    assert result.decision is GateDecision.REPLAN_REQUIRED
    assert result.reason is ChangeGateReason.ROUTE_POLICY_CONFLICT


def test_packet_and_frozen_handoff_digests_bind_exact_source_and_stay_immutable():
    case = _case()
    packet_digest = case.evidence.digest()
    handoff_digest = case.handoff.digest()

    changed_source = replace(case.evidence.source, commit="d" * 40)
    assert replace(case.evidence, source=changed_source).digest() != packet_digest
    reviews = (_review(case, ReviewerClass.REVIEWER, "reviewer-a"),)
    assert reviews[0].bundle_sha256 == case.handoff.review_bundle_sha256()
    assert case.handoff.digest() == handoff_digest

    composed = replace(case.evidence.source, branch="caf\N{LATIN SMALL LETTER E WITH ACUTE}")
    decomposed = replace(case.evidence.source, branch="cafe\N{COMBINING ACUTE ACCENT}")
    assert replace(case.evidence, source=composed).digest() != replace(
        case.evidence,
        source=decomposed,
    ).digest()


def test_unsupported_versions_and_packet_digest_drift_fail_closed():
    case = _case()
    base = _request(case)

    packet_version = _evaluate(
        case,
        replace(base, evidence=replace(case.evidence, schema="unsupported")),
    )
    handoff_version = _evaluate(
        case,
        replace(base, frozen_handoff=replace(case.handoff, schema="unsupported")),
    )
    digest_drift = _evaluate(
        case,
        replace(
            base,
            frozen_handoff=replace(case.handoff, evidence_sha256="f" * 64),
        ),
    )

    assert packet_version.reason is ChangeGateReason.EVIDENCE_VERSION_UNSUPPORTED
    assert handoff_version.reason is ChangeGateReason.FROZEN_HANDOFF_VERSION_UNSUPPORTED
    assert digest_drift.reason is ChangeGateReason.EVIDENCE_DIGEST_MISMATCH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_paths", ("hermes_cli/change_gate.py",)),
        (
            "required_inputs",
            (
                ArtifactBinding(
                    path="control/other.json",
                    sha256="3" * 64,
                    role="input",
                ),
            ),
        ),
        ("inventory_id", "different-inventory"),
    ],
)
def test_frozen_handoff_drift_denies_with_bounded_reason(field, value):
    case = _case()
    drifted = replace(case.handoff, **{field: value})

    result = _evaluate(case, replace(_request(case), frozen_handoff=drifted))

    assert result.decision is GateDecision.DENY
    assert result.reason is ChangeGateReason.HANDOFF_BINDING_MISMATCH


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("repository", "Elsewhere/hermes-agent", ChangeGateReason.REPOSITORY_MISMATCH),
        ("branch", "wrong-branch", ChangeGateReason.BRANCH_MISMATCH),
        ("commit", "d" * 40, ChangeGateReason.COMMIT_MISMATCH),
        ("tree", "e" * 40, ChangeGateReason.TREE_MISMATCH),
    ],
)
def test_wrong_source_identity_denies_with_bounded_reason(field, value, reason):
    case = _case()
    source = replace(case.evidence.source, **{field: value})
    result = _evaluate(case, replace(_request(case), source=source))
    assert result.decision is GateDecision.DENY
    assert result.reason is reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("task_id", "wrong-task", ChangeGateReason.TASK_ID_MISMATCH),
        ("run_id", "wrong-run", ChangeGateReason.RUN_ID_MISMATCH),
        ("work_id", "wrong-work", ChangeGateReason.WORK_ID_MISMATCH),
        ("operation", "wrong-operation", ChangeGateReason.OPERATION_MISMATCH),
        ("effect", "WRONG_EFFECT", ChangeGateReason.EFFECT_MISMATCH),
    ],
)
def test_wrong_work_identity_denies_with_bounded_reason(field, value, reason):
    case = _case()
    work = replace(case.evidence.work, **{field: value})
    result = _evaluate(case, replace(_request(case), work=work))
    assert result.decision is GateDecision.DENY
    assert result.reason is reason


def test_wrong_db_task_and_route_are_denied():
    case = _case()
    request = _request(case)

    task_result = case.adapter.evaluate(
        request,
        actual_task_id="another-task",
        actual_route=case.evidence.route.executor,
    )
    route_result = case.adapter.evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=_selector("different-executor"),
    )
    assert task_result.reason is ChangeGateReason.TASK_ID_MISMATCH
    assert route_result.reason is ChangeGateReason.ROUTE_PROJECTION_MISMATCH


def test_path_artifact_scope_and_freshness_fail_closed():
    case = _case()
    base = _request(case)

    traversal = _evaluate(case, replace(base, requested_paths=("../escape",)))
    outside = _evaluate(case, replace(base, requested_paths=("other/file.py",)))
    artifact = _evaluate(case, replace(base, observed_outputs=()))
    expired_evidence = replace(case.evidence, expires_at_epoch=NOW)
    expired = _evaluate(case, replace(base, evidence=expired_evidence))
    forbidden_work = replace(case.evidence.work, effect="PRIVATE_STATE_READ")
    forbidden = _evaluate(case, replace(base, work=forbidden_work))
    forbidden_handoff = replace(case.handoff, forbidden_effects=("SOURCE_CHANGE",))
    scope_deviation = _evaluate(
        case,
        replace(base, frozen_handoff=forbidden_handoff),
    )

    assert traversal.decision is GateDecision.SCOPE_DEVIATION
    assert traversal.reason is ChangeGateReason.ALLOWED_PATH_VIOLATION
    assert outside.reason is ChangeGateReason.ALLOWED_PATH_VIOLATION
    assert artifact.reason is ChangeGateReason.ARTIFACT_BINDING_MISMATCH
    assert expired.reason is ChangeGateReason.EVIDENCE_EXPIRED
    assert forbidden.reason is ChangeGateReason.EFFECT_MISMATCH
    assert scope_deviation.decision is GateDecision.SCOPE_DEVIATION
    assert scope_deviation.reason is ChangeGateReason.SCOPE_DEVIATION


def test_evidence_lifetime_allows_exact_one_hour_but_rejects_longer():
    case = _case()
    one_hour = replace(
        case.evidence,
        created_at_epoch=NOW,
        expires_at_epoch=NOW + MAX_EVIDENCE_LIFETIME_SECONDS,
    )
    one_hour_handoff = freeze_handoff(
        one_hour,
        inventory=case.inventory,
        inventory_consumer="change-gate-claim-adapter",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION", "PRIVATE_STATE_READ"),
    )
    one_hour_case = SyntheticCase(
        one_hour,
        one_hour_handoff,
        case.inventory,
        case.adapter,
    )
    with _released_request(one_hour_case, ReleasePurpose.CLAIM) as (request, _receipt):
        assert case.adapter.evaluate(
            request,
            actual_task_id=one_hour.work.task_id,
            actual_route=one_hour.route.executor,
        ).allowed

    too_long = replace(
        case.evidence,
        created_at_epoch=NOW,
        expires_at_epoch=NOW + MAX_EVIDENCE_LIFETIME_SECONDS + 1,
    )
    too_long_handoff = freeze_handoff(
        too_long,
        inventory=case.inventory,
        inventory_consumer="change-gate-claim-adapter",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION", "PRIVATE_STATE_READ"),
    )
    too_long_case = SyntheticCase(
        too_long,
        too_long_handoff,
        case.inventory,
        case.adapter,
    )
    result = case.adapter.evaluate(
        _request(too_long_case),
        actual_task_id=too_long.work.task_id,
        actual_route=too_long.route.executor,
    )
    assert result.reason is ChangeGateReason.EVIDENCE_MALFORMED


def test_missing_or_wrong_inventory_binding_requires_replan():
    case = _case()
    request = _request(case)
    missing = ChangeGateAdapter(enabled=True, clock=lambda: NOW).evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )
    wrong_record = replace(case.inventory, owner="other-owner")
    wrong_adapter = ChangeGateAdapter(
        enabled=True,
        inventory_reader=StaticArchitectureInventoryReader((wrong_record,)),
        clock=lambda: NOW,
    )
    mismatch = wrong_adapter.evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )

    assert missing.decision is GateDecision.REPLAN_REQUIRED
    assert missing.reason is ChangeGateReason.INVENTORY_BINDING_MISSING
    assert mismatch.decision is GateDecision.REPLAN_REQUIRED
    assert mismatch.reason is ChangeGateReason.INVENTORY_BINDING_MISMATCH


def test_inventory_consumer_duplicates_and_reader_failure_fail_closed():
    case = _case()
    request = _request(case)
    wrong_consumer = replace(case.inventory, consumers=("different-consumer",))
    consumer_result = ChangeGateAdapter(
        enabled=True,
        inventory_reader=StaticArchitectureInventoryReader((wrong_consumer,)),
        clock=lambda: NOW,
    ).evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )
    duplicate_result = ChangeGateAdapter(
        enabled=True,
        inventory_reader=StaticArchitectureInventoryReader((case.inventory, case.inventory)),
        clock=lambda: NOW,
    ).evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )

    class FailingReader:
        def read(self, inventory_id):
            raise RuntimeError("synthetic read failure")

    failed_result = ChangeGateAdapter(
        enabled=True,
        inventory_reader=FailingReader(),
        clock=lambda: NOW,
    ).evaluate(
        request,
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )

    assert consumer_result.reason is ChangeGateReason.INVENTORY_BINDING_MISMATCH
    assert duplicate_result.reason is ChangeGateReason.INVENTORY_BINDING_MISSING
    assert failed_result.reason is ChangeGateReason.INVENTORY_READ_FAILED


def test_default_off_adapter_is_inert():
    result = ChangeGateAdapter().evaluate(None)
    assert result.allowed is True
    assert result.reason is ChangeGateReason.DISABLED
    assert result.phase is GatePhase.G0_EVIDENCE

    non_boolean = ChangeGateAdapter(enabled=1).evaluate(None)  # type: ignore[arg-type]
    assert non_boolean.allowed is True
    assert non_boolean.reason is ChangeGateReason.DISABLED


def test_enabled_adapter_uses_its_clock_and_rejects_forged_route_type():
    case = _case()

    failing_clock = replace(
        case.adapter,
        clock=lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
    )
    failed = failing_clock.evaluate(
        _request(case),
        actual_task_id=case.evidence.work.task_id,
        actual_route=case.evidence.route.executor,
    )

    class ForgedRoute:
        def normalized(self):
            return case.evidence.route.executor

    forged = case.adapter.evaluate(
        _request(case),
        actual_task_id=case.evidence.work.task_id,
        actual_route=ForgedRoute(),  # type: ignore[arg-type]
    )

    assert failed.reason is ChangeGateReason.EVIDENCE_MALFORMED
    assert forged.reason is ChangeGateReason.ROUTE_PROJECTION_MISMATCH


def test_claim_requires_exact_live_current_turn_release():
    case = _case()
    missing = _evaluate(case, _request(case))
    assert missing.reason is ChangeGateReason.RELEASE_RECEIPT_MISSING

    with _released_request(case, ReleasePurpose.CLAIM, turn_id="turn-a") as (
        released,
        receipt,
    ):
        allowed = _evaluate(case, released)
        assert allowed.allowed is True
        assert allowed.phase is GatePhase.G2_CLAIM
        assert "host_signature" not in repr(receipt)

    stale = _evaluate(case, replace(_request(case), release_receipt=receipt))
    assert stale.reason is ChangeGateReason.RELEASE_RECEIPT_STALE

    statement = expected_release_statement(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=case.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="session-change-gate",
        turn_id="turn-b",
    ):
        assert not validate_current_turn_release_receipt(
            receipt,
            handoff_sha256=case.handoff.digest(),
            purpose=ReleasePurpose.CLAIM,
        )


def test_unrelated_current_user_text_cannot_issue_release():
    case = _case()
    with _scoped_test_current_turn_user_authority(
        "please continue",
        session_id="session-change-gate",
        turn_id="turn-unrelated",
    ):
        assert (
            issue_current_turn_release_receipt(
                purpose=ReleasePurpose.CLAIM,
                handoff_sha256=case.handoff.digest(),
            )
            is None
        )


def test_malformed_or_wrong_handoff_receipt_fails_closed_without_exception():
    case = _case()
    with _released_request(case, ReleasePurpose.CLAIM) as (_, receipt):
        forged = replace(receipt, action_fingerprint=object())
        assert not validate_current_turn_release_receipt(
            forged,
            handoff_sha256=case.handoff.digest(),
            purpose=ReleasePurpose.CLAIM,
        )
        wrong_handoff = replace(case.handoff, inventory_owner="other-owner")
        assert not validate_current_turn_release_receipt(
            receipt,
            handoff_sha256=wrong_handoff.digest(),
            purpose=ReleasePurpose.CLAIM,
        )

    forged_publicly = HumanReleaseReceipt(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=case.handoff.digest(),
        action_fingerprint="0" * 64,
        turn_id_sha256="0" * 64,
        session_scope_sha256="0" * 64,
        platform_scope_sha256="0" * 64,
        user_message_index=0,
        source_role="user",
    )
    assert not validate_current_turn_release_receipt(
        forged_publicly,
        handoff_sha256=case.handoff.digest(),
        purpose=ReleasePurpose.CLAIM,
    )


def test_durable_claim_release_binds_exact_transition_anchor():
    case = _case()
    anchor = _claim_transition_anchor(case)
    statement = expected_release_statement(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=case.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="session-change-gate",
        turn_id="turn-durable-claim",
        platform_scope="manual",
    ):
        release = issue_durable_release_artifact(
            purpose=ReleasePurpose.CLAIM,
            handoff=case.handoff,
            evidence=case.evidence,
            transition_anchor=anchor,
            ttl_seconds=60,
            clock=lambda: NOW,
        )

    assert release is not None
    assert release.transition_anchor == anchor
    assert release.route_sha256 == canonical_sha256(case.handoff.route)
    assert validate_durable_release_artifact(
        release,
        purpose=ReleasePurpose.CLAIM,
        evidence=case.evidence,
        handoff=case.handoff,
        now_epoch=NOW,
        expected_transition_anchor=anchor,
    ) is ChangeGateReason.ALLOWED

    stale_anchor = _claim_transition_anchor(case, latest_event_id=2)
    assert validate_durable_release_artifact(
        release,
        purpose=ReleasePurpose.CLAIM,
        evidence=case.evidence,
        handoff=case.handoff,
        now_epoch=NOW,
        expected_transition_anchor=stale_anchor,
    ) is ChangeGateReason.RELEASE_TRANSITION_STALE

    request = request_from_durable_release(
        release,
        evidence=case.evidence,
        handoff=case.handoff,
        expected_transition_anchor=anchor,
        requested_paths=(),
    )
    assert request is not None
    assert request.requested_paths == ()


def test_durable_release_lifetime_stays_capped_at_ten_minutes():
    case = _case()
    anchor = _claim_transition_anchor(case)
    statement = expected_release_statement(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=case.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="session-change-gate",
        turn_id="turn-durable-ttl",
        platform_scope="manual",
    ):
        release = issue_durable_release_artifact(
            purpose=ReleasePurpose.CLAIM,
            handoff=case.handoff,
            evidence=case.evidence,
            transition_anchor=anchor,
            ttl_seconds=MAX_RELEASE_LIFETIME_SECONDS,
            clock=lambda: NOW,
        )
        too_long = issue_durable_release_artifact(
            purpose=ReleasePurpose.CLAIM,
            handoff=case.handoff,
            evidence=case.evidence,
            transition_anchor=anchor,
            ttl_seconds=MAX_RELEASE_LIFETIME_SECONDS + 1,
            clock=lambda: NOW,
        )

    assert release is not None
    assert release.expires_at_epoch - release.issued_at_epoch == 600
    assert too_long is None


def test_durable_g4_release_requires_review_transition_anchor():
    case = _case(RiskLevel.HIGH)
    reviews = (
        _review(case, ReviewerClass.NORMAL, "reviewer-normal"),
        _review(case, ReviewerClass.DEEP, "reviewer-deep"),
    )
    anchor = _g4_transition_anchor(case, reviews)
    statement = expected_release_statement(
        purpose=ReleasePurpose.G4,
        handoff_sha256=case.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="session-change-gate",
        turn_id="turn-durable-g4",
        platform_scope="manual",
    ):
        release = issue_durable_release_artifact(
            purpose=ReleasePurpose.G4,
            handoff=case.handoff,
            evidence=case.evidence,
            transition_anchor=anchor,
            ttl_seconds=60,
            clock=lambda: NOW,
        )

    assert release is not None
    assert validate_durable_release_artifact(
        release,
        purpose=ReleasePurpose.G4,
        evidence=case.evidence,
        handoff=case.handoff,
        now_epoch=NOW,
        expected_transition_anchor=anchor,
    ) is ChangeGateReason.ALLOWED

    wrong_generation = replace(release, transition_anchor=replace(anchor, status="ready"))
    assert validate_durable_release_artifact(
        wrong_generation,
        purpose=ReleasePurpose.G4,
        evidence=case.evidence,
        handoff=case.handoff,
        now_epoch=NOW,
        expected_transition_anchor=anchor,
    ) is ChangeGateReason.RELEASE_TRANSITION_STALE


@pytest.mark.parametrize("risk", [RiskLevel.LOW, RiskLevel.NORMAL])
def test_low_and_normal_converge_with_one_reviewer_artifact(risk):
    case = _case(risk)
    result = evaluate_reviews(
        (_review(case, ReviewerClass.REVIEWER, "reviewer-a"),),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    assert result.allowed is True


def test_high_requires_distinct_normal_and_deep_on_same_bundle():
    case = _case(RiskLevel.HIGH)
    normal = _review(case, ReviewerClass.NORMAL, "reviewer-normal")
    deep = _review(case, ReviewerClass.DEEP, "reviewer-deep")

    missing = evaluate_reviews(
        (normal,),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    converged = evaluate_reviews(
        (normal, deep),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    stale_deep = replace(deep, bundle_sha256="f" * 64)
    stale = evaluate_reviews(
        (normal, stale_deep),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    duplicate = evaluate_reviews(
        (normal, replace(normal, reviewer_identity="reviewer-other")),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    collision = evaluate_reviews(
        (normal, replace(deep, reviewer_identity=normal.reviewer_identity)),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )

    assert missing.reason is ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS
    assert converged.allowed is True
    assert stale.reason is ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS
    assert duplicate.reason is ChangeGateReason.REVIEW_DUPLICATE_CLASS
    assert collision.reason is ChangeGateReason.REVIEW_IDENTITY_COLLISION


def test_review_correction_outcomes_have_deterministic_precedence():
    case = _case(RiskLevel.HIGH)
    request_changes = _review(
        case,
        ReviewerClass.NORMAL,
        "reviewer-normal",
        verdict=ReviewVerdict.REQUEST_CHANGES,
    )
    replan = _review(
        case,
        ReviewerClass.DEEP,
        "reviewer-deep",
        verdict=ReviewVerdict.REPLAN_REQUIRED,
    )
    result = evaluate_reviews(
        (request_changes, replan),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    assert result.decision is GateDecision.REPLAN_REQUIRED
    assert result.reason is ChangeGateReason.REVIEW_REPLAN_REQUIRED

    changes_only = evaluate_reviews(
        (
            request_changes,
            _review(case, ReviewerClass.DEEP, "reviewer-deep"),
        ),
        bundle_sha256=case.handoff.review_bundle_sha256(),
        required_reviewers=case.handoff.route.required_reviewers,
    )
    assert changes_only.decision is GateDecision.REQUEST_CHANGES
    assert changes_only.reason is ChangeGateReason.REVIEW_REQUEST_CHANGES


def test_g4_denials_cover_missing_stale_wrong_purpose_and_corrections():
    case = _case(RiskLevel.HIGH)
    passing = (
        _review(case, ReviewerClass.NORMAL, "reviewer-normal"),
        _review(case, ReviewerClass.DEEP, "reviewer-deep"),
    )
    missing = _evaluate(
        case,
        _request(case, purpose=ReleasePurpose.G4, reviews=passing),
    )

    with _released_request(case, ReleasePurpose.G4, reviews=passing) as (request, _):
        live = _evaluate(case, request)
    stale = _evaluate(case, request)

    with _released_request(case, ReleasePurpose.CLAIM) as (_, claim_receipt):
        wrong_purpose = _evaluate(
            case,
            _request(
                case,
                purpose=ReleasePurpose.G4,
                receipt=claim_receipt,
                reviews=passing,
            ),
        )

    changes = _evaluate(
        case,
        _request(
            case,
            purpose=ReleasePurpose.G4,
            reviews=(
                _review(
                    case,
                    ReviewerClass.NORMAL,
                    "reviewer-normal",
                    verdict=ReviewVerdict.REQUEST_CHANGES,
                ),
                _review(case, ReviewerClass.DEEP, "reviewer-deep"),
            ),
        ),
    )
    replan = _evaluate(
        case,
        _request(
            case,
            purpose=ReleasePurpose.G4,
            reviews=(
                _review(case, ReviewerClass.NORMAL, "reviewer-normal"),
                _review(
                    case,
                    ReviewerClass.DEEP,
                    "reviewer-deep",
                    verdict=ReviewVerdict.REPLAN_REQUIRED,
                ),
            ),
        ),
    )

    assert missing.reason is ChangeGateReason.RELEASE_RECEIPT_MISSING
    assert live.allowed is True
    assert stale.reason is ChangeGateReason.RELEASE_RECEIPT_STALE
    assert wrong_purpose.reason is ChangeGateReason.RELEASE_RECEIPT_STALE
    assert changes.decision is GateDecision.REQUEST_CHANGES
    assert replan.decision is GateDecision.REPLAN_REQUIRED


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _domain_snapshot(conn, task_id: str):
    task = conn.execute(
        "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]
    events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]
    return tuple(task), runs, events


def test_end_to_end_synthetic_claim_run_review_and_g4_flow(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="end-to-end gated task",
            assignee="executor",
            model_override="fixture-model",
            provider_override="fixture-provider",
            reasoning_effort="medium",
        )
        case = _case(RiskLevel.HIGH, task_id=task_id)
        frozen_digest = case.handoff.digest()
        before = _domain_snapshot(conn, task_id)

        with _released_request(case, ReleasePurpose.CLAIM, turn_id="turn-claim") as (
            claim_request,
            _,
        ):
            claimed = kb.claim_task(
                conn,
                task_id,
                change_gate_adapter=case.adapter,
                change_gate_request=claim_request,
            )

        assert claimed is not None
        after = _domain_snapshot(conn, task_id)
        assert after[0][0] == "running"
        assert after[0][2]
        assert after[1] == before[1] + 1
        assert after[2] == before[2] + 1
        assert conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()[0] == "claimed"

        reviews = (
            _review(case, ReviewerClass.NORMAL, "reviewer-normal"),
            _review(case, ReviewerClass.DEEP, "reviewer-deep"),
        )
        with _released_request(
            case,
            ReleasePurpose.G4,
            reviews=reviews,
            turn_id="turn-g4",
        ) as (g4_request, _):
            g4 = _evaluate(case, g4_request)

        assert g4.allowed is True
        assert g4.phase is GatePhase.G4_RELEASE
        assert case.handoff.digest() == frozen_digest


def test_enabled_claim_denial_has_zero_domain_mutation(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="gated task",
            assignee="executor",
            model_override="fixture-model",
            provider_override="fixture-provider",
            reasoning_effort="medium",
        )
        case = _case(task_id=task_id)
        before = _domain_snapshot(conn, task_id)

        claimed = kb.claim_task(
            conn,
            task_id,
            change_gate_adapter=case.adapter,
            change_gate_request=_request(case),
        )

        assert claimed is None
        assert _domain_snapshot(conn, task_id) == before

        bounded = kb._evaluate_change_gate_claim(
            conn,
            task_id,
            case.adapter,
            _request(case),
        )
        assert bounded.decision is GateDecision.DENY
        assert bounded.reason is ChangeGateReason.RELEASE_RECEIPT_MISSING


def test_default_off_and_allowed_claim_delegate_to_upstream_mechanics(kanban_home):
    with kb.connect() as conn:
        default_task = kb.create_task(conn, title="default", assignee="executor")
        default_claim = kb.claim_task(conn, default_task)
        assert default_claim is not None

        explicitly_disabled = kb.create_task(conn, title="disabled", assignee="executor")
        disabled_claim = kb.claim_task(
            conn,
            explicitly_disabled,
            change_gate_adapter=ChangeGateAdapter(enabled=False),
            change_gate_request=None,
        )
        assert disabled_claim is not None

        gated_task = kb.create_task(
            conn,
            title="gated",
            assignee="executor",
            model_override="fixture-model",
            provider_override="fixture-provider",
            reasoning_effort="medium",
        )
        case = _case(task_id=gated_task)
        before = _domain_snapshot(conn, gated_task)
        with _released_request(case, ReleasePurpose.CLAIM) as (request, _):
            claimed = kb.claim_task(
                conn,
                gated_task,
                change_gate_adapter=case.adapter,
                change_gate_request=request,
            )

        assert claimed is not None
        after = _domain_snapshot(conn, gated_task)
        assert after[0][0] == "running"
        assert after[1] == before[1] + 1
        assert after[2] == before[2] + 1


def test_g4_release_cannot_be_reused_as_claim_authority(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="g4 is not claim authority",
            assignee="executor",
            model_override="fixture-model",
            provider_override="fixture-provider",
            reasoning_effort="medium",
        )
        case = _case(task_id=task_id)
        reviews = (_review(case, ReviewerClass.REVIEWER, "reviewer-a"),)
        before = _domain_snapshot(conn, task_id)
        with _released_request(case, ReleasePurpose.G4, reviews=reviews) as (request, _):
            claimed = kb.claim_task(
                conn,
                task_id,
                change_gate_adapter=case.adapter,
                change_gate_request=request,
            )

        assert claimed is None
        assert _domain_snapshot(conn, task_id) == before
