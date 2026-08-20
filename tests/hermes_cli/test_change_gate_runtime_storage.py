"""Runtime storage tests for default-off Change Gate release persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    ArchitectureInventoryRecord,
    DurableReleaseArtifact,
    EvidencePacket,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewRoute,
    ReviewerClass,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_sha256,
    freeze_handoff,
)
from hermes_cli.change_gate_runtime import runtime_policy_from_mapping

NOW = 1_000_000


def _connect(path: Path) -> sqlite3.Connection:
    conn = kb.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _selector(assignee: str) -> UpstreamRouteSelector:
    return UpstreamRouteSelector(
        assignee=assignee,
        model_override="fixture-model",
        provider_override="fixture-provider",
        reasoning_effort="medium",
    )


def _evidence(*, task_id: str = "task-runtime-storage") -> EvidencePacket:
    source = SourceIdentity(
        repository="Bichalla/hermes-agent",
        branch="track-g/runtime-storage",
        commit="a" * 40,
        tree="b" * 40,
    )
    work = WorkIdentity(
        task_id=task_id,
        run_id="run-runtime-storage",
        work_id="work-runtime-storage",
        operation="apply-source-candidate",
        effect="SOURCE_CHANGE",
    )
    route = RouteProjection(
        risk=RiskLevel.NORMAL,
        executor=_selector("executor"),
        reviews=(ReviewRoute(ReviewerClass.REVIEWER, _selector("reviewer")),),
    )
    return EvidencePacket(
        source=source,
        work=work,
        risk=RiskLevel.NORMAL,
        route=route,
        allowed_paths=("hermes_cli/change_gate.py",),
        required_inputs=(),
        produced_artifacts=(),
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
        consumers=("change-gate-runtime-storage",),
        authority_contract="current-turn-frozen-handoff",
        activation_state="DEFAULT_OFF",
        source_paths=("hermes_cli/change_gate.py",),
        artifact_paths=("results/change-gate.json",),
        risk=evidence.risk,
        blast_radius=("claim", "g4"),
    )


def _release(
    *,
    purpose: ReleasePurpose = ReleasePurpose.CLAIM,
    task_id: str = "task-runtime-storage",
    release_id: str = "cgr_" + "1" * 64,
    issued_at: int = NOW,
    expires_at: int = NOW + 300,
) -> DurableReleaseArtifact:
    evidence = _evidence(task_id=task_id)
    inventory = _inventory(evidence)
    handoff = freeze_handoff(
        evidence,
        inventory=inventory,
        inventory_consumer="change-gate-runtime-storage",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION",),
    )
    receipt = HumanReleaseReceipt(
        purpose=purpose,
        handoff_sha256=handoff.digest(),
        action_fingerprint="2" * 64,
        turn_id_sha256="3" * 64,
        session_scope_sha256="4" * 64,
        platform_scope_sha256="5" * 64,
        user_message_index=1,
        source_role="human",
    )
    return DurableReleaseArtifact(
        release_id=release_id,
        purpose=purpose,
        handoff_sha256=handoff.digest(),
        evidence_sha256=evidence.digest(),
        inventory_sha256=handoff.inventory_sha256,
        artifact_set_sha256=canonical_sha256(
            handoff.required_inputs + handoff.produced_artifacts
        ),
        route_sha256=canonical_sha256(handoff.route),
        task_id=evidence.work.task_id,
        work_id=evidence.work.work_id,
        source=evidence.source,
        authority_receipt=receipt,
        issued_at_epoch=issued_at,
        expires_at_epoch=expires_at,
    )


def test_default_off_connect_does_not_create_release_table(tmp_path: Path) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        assert _table_exists(conn, "tasks")
        assert not kb.change_gate_runtime_schema_exists(conn)
        assert not _table_exists(conn, "change_gate_releases")


def test_explicit_runtime_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        kb.initialize_change_gate_runtime_schema(conn)
        kb.initialize_change_gate_runtime_schema(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(change_gate_releases)").fetchall()
        }

    assert {"release_id", "artifact_sha256", "task_id", "purpose", "state"} <= columns


def test_store_load_round_trips_canonical_durable_release(tmp_path: Path) -> None:
    release = _release()
    with _connect(tmp_path / "kanban.db") as conn:
        kb.initialize_change_gate_runtime_schema(conn)

        release_id = kb._store_change_gate_release(conn, release)
        loaded = kb._load_change_gate_release(conn, release_id)
        row = kb.change_gate_release_state(conn, release_id)

    assert loaded == release
    assert row is not None
    assert row["artifact_sha256"] == release.digest()
    assert row["state"] == "ISSUED"


def test_store_refuses_to_create_default_off_schema(tmp_path: Path) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        with pytest.raises(RuntimeError, match="change_gate_schema_not_initialized"):
            kb._store_change_gate_release(conn, _release())

        assert not kb.change_gate_runtime_schema_exists(conn)


def test_latest_release_requires_matching_purpose_and_unexpired_state(
    tmp_path: Path,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        kb.initialize_change_gate_runtime_schema(conn)
        kb._store_change_gate_release(conn, _release(purpose=ReleasePurpose.CLAIM))

        claim_id = kb.latest_change_gate_release_id(
            conn,
            "task-runtime-storage",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 1,
        )
        g4_id = kb.latest_change_gate_release_id(
            conn,
            "task-runtime-storage",
            purpose=ReleasePurpose.G4,
            now_epoch=NOW + 1,
        )
        expired_id = kb.latest_change_gate_release_id(
            conn,
            "task-runtime-storage",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 300,
        )

    assert claim_id == "cgr_" + "1" * 64
    assert g4_id is None
    assert expired_id is None


def test_transition_classifies_missing_wrong_purpose_expired_revoked_and_replay(
    tmp_path: Path,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        kb.initialize_change_gate_runtime_schema(conn)

        missing = kb._change_gate_release_for_transition(
            conn,
            "task-runtime-storage",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 1,
        )

        g4_release = _release(
            purpose=ReleasePurpose.G4,
            release_id="cgr_" + "2" * 64,
        )
        kb._store_change_gate_release(conn, g4_release)
        wrong_purpose = kb._change_gate_release_for_transition(
            conn,
            "task-runtime-storage",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 1,
        )

        expired_release = _release(
            task_id="task-expired",
            release_id="cgr_" + "3" * 64,
            issued_at=NOW,
            expires_at=NOW + 1,
        )
        kb._store_change_gate_release(conn, expired_release)
        expired = kb._change_gate_release_for_transition(
            conn,
            "task-expired",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 1,
        )

        revoked_release = _release(
            task_id="task-revoked",
            release_id="cgr_" + "4" * 64,
        )
        kb._store_change_gate_release(conn, revoked_release)
        kb.revoke_change_gate_releases(
            conn,
            "task-revoked",
            reason=kb.ChangeGateReason.RELEASE_REVOKED,
            now_epoch=NOW + 2,
        )
        revoked = kb._change_gate_release_for_transition(
            conn,
            "task-revoked",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 3,
        )

        consumed_release = _release(
            task_id="task-consumed",
            release_id="cgr_" + "5" * 64,
        )
        kb._store_change_gate_release(conn, consumed_release)
        assert kb._mark_change_gate_release_consumed(
            conn,
            consumed_release,
            run_id=7,
            event_id=11,
            from_status="ready",
            to_status="doing",
            now_epoch=NOW + 4,
        )
        replay = kb._change_gate_release_for_transition(
            conn,
            "task-consumed",
            purpose=ReleasePurpose.CLAIM,
            now_epoch=NOW + 5,
        )

    assert missing == (None, kb.ChangeGateReason.RELEASE_MISSING)
    assert wrong_purpose == (None, kb.ChangeGateReason.RELEASE_PURPOSE_UNSUPPORTED)
    assert expired == (None, kb.ChangeGateReason.RELEASE_EXPIRED)
    assert revoked == (None, kb.ChangeGateReason.RELEASE_REVOKED)
    assert replay == (None, kb.ChangeGateReason.RELEASE_REPLAY)


def test_consumption_is_single_use_and_bound_to_exact_release_state(
    tmp_path: Path,
) -> None:
    release = _release()
    wrong_task = replace(release, task_id="other-task")
    with _connect(tmp_path / "kanban.db") as conn:
        kb.initialize_change_gate_runtime_schema(conn)
        kb._store_change_gate_release(conn, release)

        assert not kb._mark_change_gate_release_consumed(
            conn,
            wrong_task,
            run_id=7,
            event_id=11,
            from_status="ready",
            to_status="doing",
            now_epoch=NOW + 1,
        )
        assert kb._mark_change_gate_release_consumed(
            conn,
            release,
            run_id=7,
            event_id=11,
            from_status="ready",
            to_status="doing",
            now_epoch=NOW + 1,
        )
        assert not kb._mark_change_gate_release_consumed(
            conn,
            release,
            run_id=8,
            event_id=12,
            from_status="ready",
            to_status="doing",
            now_epoch=NOW + 2,
        )
        state = kb.change_gate_release_state(conn, release.release_id)

    assert state is not None
    assert state["state"] == "CONSUMED"
    assert state["consumed_run_id"] == 7
    assert state["consumed_event_id"] == 11
    assert state["consumed_from_status"] == "ready"
    assert state["consumed_to_status"] == "doing"


def test_runtime_policy_requires_exact_true_for_enabled(tmp_path: Path) -> None:
    valid = runtime_policy_from_mapping(
        {
            "change_gate": {
                "enabled": True,
                "inventory_root": str(tmp_path),
                "release_ttl_seconds": 120,
                "ungated_policy": "passthrough",
                "planner_assignee": "planner",
                "max_corrections": 1,
            }
        }
    )

    assert valid.enabled is True
    assert valid.valid is True
    assert valid.inventory_root == tmp_path
    assert runtime_policy_from_mapping({}).enabled is False
    assert runtime_policy_from_mapping({"change_gate": {"enabled": False}}).enabled is False
    assert runtime_policy_from_mapping({"change_gate": {"enabled": 1}}).enabled is False
    assert runtime_policy_from_mapping({"change_gate": {"enabled": "true"}}).enabled is False


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": True, "inventory_root": "relative"},
        {"enabled": True, "inventory_root": ""},
        {"enabled": True, "inventory_root": "/tmp/inventory", "release_ttl_seconds": 0},
        {"enabled": True, "inventory_root": "/tmp/inventory", "release_ttl_seconds": 601},
        {"enabled": True, "inventory_root": "/tmp/inventory", "ungated_policy": "enforce"},
        {"enabled": True, "inventory_root": "/tmp/inventory", "planner_assignee": " planner"},
        {"enabled": True, "inventory_root": "/tmp/inventory", "max_corrections": 11},
        {"enabled": True, "inventory_root": "/tmp/inventory", "unexpected": True},
    ],
)
def test_runtime_policy_marks_invalid_exact_true_blocks_invalid(block: dict) -> None:
    policy = runtime_policy_from_mapping({"change_gate": block})

    assert policy.enabled is True
    assert policy.valid is False
