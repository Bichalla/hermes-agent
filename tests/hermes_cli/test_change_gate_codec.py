"""Strict codec tests for Change Gate runtime artifacts."""

from __future__ import annotations

import hashlib
import json

from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    DURABLE_RELEASE_SCHEMA,
    EVIDENCE_PACKET_SCHEMA,
    FROZEN_HANDOFF_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    ArchitectureInventoryRecord,
    ArtifactBinding,
    DurableReleaseArtifact,
    EvidencePacket,
    FrozenHandoff,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewResult,
    ReviewRoute,
    ReviewerClass,
    ReviewVerdict,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    freeze_handoff,
)
from hermes_cli.change_gate_codec import (
    ArtifactCodecReason,
    decode_artifact,
    encode_artifact,
    read_artifact,
    validate_artifact_binding,
)

NOW = 1_000_000
SOURCE = SourceIdentity(
    repository="Bichalla/hermes-agent",
    branch="track-g/codec",
    commit="a" * 40,
    tree="b" * 40,
)
WORK = WorkIdentity(
    task_id="task-codec",
    run_id="run-codec",
    work_id="work-codec",
    operation="apply-source-candidate",
    effect="SOURCE_CHANGE",
)


def _selector(assignee: str) -> UpstreamRouteSelector:
    return UpstreamRouteSelector(
        assignee=assignee,
        model_override="fixture-model",
        provider_override="fixture-provider",
        reasoning_effort="medium",
    )


def _evidence() -> EvidencePacket:
    route = RouteProjection(
        risk=RiskLevel.HIGH,
        executor=_selector("executor"),
        reviews=(
            ReviewRoute(ReviewerClass.NORMAL, _selector("normal-reviewer")),
            ReviewRoute(ReviewerClass.DEEP, _selector("deep-reviewer")),
        ),
    )
    return EvidencePacket(
        source=SOURCE,
        work=WORK,
        risk=RiskLevel.HIGH,
        route=route,
        allowed_paths=("hermes_cli/change_gate.py", "hermes_cli/kanban_db.py"),
        required_inputs=(
            ArtifactBinding(
                path="control/input.json",
                sha256="1" * 64,
                role="input",
                git_oid="c" * 40,
            ),
        ),
        produced_artifacts=(
            ArtifactBinding(
                path="results/output.json",
                sha256="2" * 64,
                role="output",
                git_oid=None,
            ),
        ),
        inventory_id="track-g-change-gate",
        created_at_epoch=NOW,
        expires_at_epoch=NOW + 600,
    )


def _inventory(evidence: EvidencePacket) -> ArchitectureInventoryRecord:
    return ArchitectureInventoryRecord(
        schema=ARCHITECTURE_INVENTORY_SCHEMA,
        inventory_id=evidence.inventory_id,
        capability="change-gate-authority-adapter",
        owner="change-gate-contract-owner",
        consumers=("change-gate-claim-adapter", "change-gate-g4"),
        authority_contract="current-turn-frozen-handoff",
        activation_state="DEFAULT_OFF",
        source_paths=("hermes_cli/change_gate.py", "hermes_cli/kanban_db.py"),
        artifact_paths=("results/change-gate.json",),
        risk=evidence.risk,
        blast_radius=("claim-only", "default-off"),
    )


def _handoff(evidence: EvidencePacket, inventory: ArchitectureInventoryRecord) -> FrozenHandoff:
    return freeze_handoff(
        evidence,
        inventory=inventory,
        inventory_consumer="change-gate-claim-adapter",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION", "PRIVATE_STATE_READ"),
    )


def _review(handoff: FrozenHandoff) -> ReviewResult:
    return ReviewResult(
        bundle_sha256=handoff.review_bundle_sha256(),
        reviewer_class=ReviewerClass.NORMAL,
        reviewer_identity="normal-reviewer",
        attempt_id="attempt-1",
        verdict=ReviewVerdict.REQUEST_CHANGES,
        finding_codes=("needs-bounded-runtime",),
        completed_at_epoch=NOW + 1,
    )


def _durable_release(evidence: EvidencePacket, handoff: FrozenHandoff) -> DurableReleaseArtifact:
    receipt = HumanReleaseReceipt(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=handoff.digest(),
        action_fingerprint="3" * 64,
        turn_id_sha256="4" * 64,
        session_scope_sha256="5" * 64,
        platform_scope_sha256="6" * 64,
        user_message_index=1,
        source_role="human",
    )
    return DurableReleaseArtifact(
        release_id="release-codec",
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=handoff.digest(),
        evidence_sha256=evidence.digest(),
        inventory_sha256=handoff.inventory_sha256,
        artifact_set_sha256="7" * 64,
        route_sha256="8" * 64,
        task_id=evidence.work.task_id,
        work_id=evidence.work.work_id,
        source=evidence.source,
        authority_receipt=receipt,
        issued_at_epoch=NOW,
        expires_at_epoch=NOW + 60,
    )


def _artifacts():
    evidence = _evidence()
    inventory = _inventory(evidence)
    handoff = _handoff(evidence, inventory)
    return (
        (EVIDENCE_PACKET_SCHEMA, evidence),
        (FROZEN_HANDOFF_SCHEMA, handoff),
        (ARCHITECTURE_INVENTORY_SCHEMA, inventory),
        (REVIEW_RESULT_SCHEMA, _review(handoff)),
        (DURABLE_RELEASE_SCHEMA, _durable_release(evidence, handoff)),
    )


def test_supported_artifacts_roundtrip_with_digest() -> None:
    for schema, artifact in _artifacts():
        data = encode_artifact(artifact)
        digest = hashlib.sha256(data).hexdigest()

        result = decode_artifact(data, expected_schema=schema, expected_sha256=digest)

        assert result.ok
        assert result.reason is ArtifactCodecReason.OK
        assert result.sha256 == digest
        assert result.byte_count == len(data)
        assert result.value == artifact
        assert result.artifact == artifact


def test_rejects_duplicate_key() -> None:
    data = (
        b'{"allowed_paths":[],"allowed_paths":[],"created_at_epoch":1,'
        b'"expires_at_epoch":2,"inventory_id":"x","produced_artifacts":[],'
        b'"required_inputs":[],"risk":"NORMAL","route":{},"schema":'
        b'"hermes.change-gate.evidence-packet/v1","source":{},"work":{}}'
    )

    result = decode_artifact(data, expected_schema=EVIDENCE_PACKET_SCHEMA)

    assert result.reason is ArtifactCodecReason.DUPLICATE_KEY


def test_rejects_unknown_field() -> None:
    data = json.loads(encode_artifact(_evidence()))
    data["unexpected"] = "x"
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    result = decode_artifact(encoded, expected_schema=EVIDENCE_PACKET_SCHEMA)

    assert result.reason is ArtifactCodecReason.UNKNOWN_FIELD


def test_rejects_wrong_schema_expectation() -> None:
    result = decode_artifact(
        encode_artifact(_evidence()),
        expected_schema=FROZEN_HANDOFF_SCHEMA,
    )

    assert result.reason is ArtifactCodecReason.WRONG_SCHEMA


def test_rejects_unsupported_schema_version() -> None:
    data = json.loads(encode_artifact(_evidence()))
    data["schema"] = "hermes.change-gate.evidence-packet/v999"
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    result = decode_artifact(encoded, expected_schema=EVIDENCE_PACKET_SCHEMA)

    assert result.reason is ArtifactCodecReason.UNSUPPORTED_SCHEMA


def test_rejects_noncanonical_json() -> None:
    data = json.dumps(json.loads(encode_artifact(_evidence())), indent=2).encode()

    result = decode_artifact(data, expected_schema=EVIDENCE_PACKET_SCHEMA)

    assert result.reason is ArtifactCodecReason.NON_CANONICAL


def test_rejects_float_and_nonfinite_numbers() -> None:
    data = b'{"schema":"hermes.change-gate.review-result/v1","completed_at_epoch":1.5}'

    result = decode_artifact(data, expected_schema=REVIEW_RESULT_SCHEMA)

    assert result.reason is ArtifactCodecReason.FLOAT_REJECTED


def test_rejects_oversize_input() -> None:
    result = decode_artifact(b" " * (256 * 1024 + 1), expected_schema=EVIDENCE_PACKET_SCHEMA)

    assert result.reason is ArtifactCodecReason.OVERSIZED


def test_rejects_digest_mismatch() -> None:
    result = decode_artifact(encode_artifact(_evidence()), expected_schema=EVIDENCE_PACKET_SCHEMA, expected_sha256="0" * 64)

    assert result.reason is ArtifactCodecReason.DIGEST_MISMATCH


def test_read_owner_relative_file_blocks_traversal_and_symlink(tmp_path) -> None:
    owner = tmp_path / "owner"
    owner.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(encode_artifact(_evidence()))
    symlink = owner / "linked.json"
    symlink.symlink_to(outside)

    traversal = read_artifact(owner, "../outside.json", expected_schema=EVIDENCE_PACKET_SCHEMA)
    linked = read_artifact(owner, "linked.json", expected_schema=EVIDENCE_PACKET_SCHEMA)

    real_dir = owner / "real"
    real_dir.mkdir()
    (real_dir / "evidence.json").write_bytes(encode_artifact(_evidence()))
    linked_dir = owner / "linked-dir"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    parent_linked = read_artifact(
        owner,
        "linked-dir/evidence.json",
        expected_schema=EVIDENCE_PACKET_SCHEMA,
    )

    assert traversal.reason is ArtifactCodecReason.PATH_TRAVERSAL
    assert linked.reason is ArtifactCodecReason.FILE_NOT_REGULAR
    assert parent_linked.reason is ArtifactCodecReason.FILE_NOT_REGULAR


def test_read_owner_relative_file_accepts_regular_file_with_digest(tmp_path) -> None:
    owner = tmp_path / "owner"
    owner.mkdir()
    data = encode_artifact(_evidence())
    artifact = owner / "evidence.json"
    artifact.write_bytes(data)

    result = read_artifact(
        owner,
        "evidence.json",
        expected_schema=EVIDENCE_PACKET_SCHEMA,
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )

    assert result.ok


def test_validate_artifact_binding_rejects_bad_role_path_and_oid() -> None:
    good = ArtifactBinding("results/output.json", "2" * 64, "output", "c" * 40)
    bad_path = ArtifactBinding("../escape.json", "2" * 64, "output", "c" * 40)
    bad_oid = ArtifactBinding("results/output.json", "2" * 64, "output", "bad")

    assert validate_artifact_binding(good, expected_role="output", require_git_oid=True) is ArtifactCodecReason.OK
    assert validate_artifact_binding(good, expected_role="input") is ArtifactCodecReason.ARTIFACT_BINDING_INVALID
    assert validate_artifact_binding(bad_path) is ArtifactCodecReason.PATH_INVALID
    assert validate_artifact_binding(bad_oid) is ArtifactCodecReason.ARTIFACT_BINDING_INVALID
