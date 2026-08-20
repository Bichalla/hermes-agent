"""Synthetic integration tests for default-off Change Gate runtime wiring."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    ArtifactBinding,
    ArchitectureInventoryRecord,
    ChangeGateReason,
    DurableReleaseArtifact,
    EvidencePacket,
    GateDecision,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewRoute,
    ReviewerClass,
    ReviewVerdict,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_sha256,
    expected_release_statement,
    freeze_handoff,
)
from hermes_cli.change_gate_codec import encode_artifact
from hermes_cli.change_gate_release import issue_change_gate_release
from hermes_cli.change_gate_runtime import (
    ChangeGateRuntimePolicy,
    REVIEW_METADATA_KEY,
    project_upstream_reviews,
)
from tools.workflow_authority import (
    _scoped_test_current_turn_user_authority,
    fingerprint_user_action,
)


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    task_id: str
    source: SourceIdentity
    evidence: EvidencePacket
    inventory: ArchitectureInventoryRecord
    handoff_sha256: str


def _connect(path: Path) -> sqlite3.Connection:
    conn = kb.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, SourceIdentity, ArtifactBinding]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "track-g-test"], check=True)
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test Engineer")
    _run_git(repo, "remote", "add", "origin", "git@github.com:Bichalla/hermes-agent.git")
    artifact = repo / "gate-artifact.txt"
    artifact.write_text("bounded synthetic artifact\n", encoding="utf-8")
    _run_git(repo, "add", "gate-artifact.txt")
    _run_git(repo, "commit", "-m", "fixture")
    source = SourceIdentity(
        repository="Bichalla/hermes-agent",
        branch="track-g-test",
        commit=_run_git(repo, "rev-parse", "HEAD"),
        tree=_run_git(repo, "rev-parse", "HEAD^{tree}"),
    )
    binding = ArtifactBinding(
        path="gate-artifact.txt",
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        role="produced",
        git_oid=_run_git(repo, "hash-object", "gate-artifact.txt"),
    )
    return repo, source, binding


def _selector(assignee: str) -> UpstreamRouteSelector:
    return UpstreamRouteSelector(
        assignee=assignee,
        model_override="fixture-model",
        provider_override="fixture-provider",
        reasoning_effort="medium",
    )


def _route(risk: RiskLevel) -> RouteProjection:
    reviews = (
        (
            ReviewRoute(ReviewerClass.NORMAL, _selector("reviewer-normal")),
            ReviewRoute(ReviewerClass.DEEP, _selector("reviewer-deep")),
        )
        if risk is RiskLevel.HIGH
        else (ReviewRoute(ReviewerClass.REVIEWER, _selector("reviewer")),)
    )
    return RouteProjection(risk=risk, executor=_selector("executor"), reviews=reviews)


def _artifact_set_sha256(handoff) -> str:
    return canonical_sha256(
        {
            "allowed_paths": handoff.allowed_paths,
            "required_inputs": handoff.required_inputs,
            "produced_artifacts": handoff.produced_artifacts,
        }
    )


def _release(
    fixture: RuntimeFixture,
    purpose: ReleasePurpose,
    *,
    release_id: str,
    issued_at: int | None = None,
) -> DurableReleaseArtifact:
    now = int(time.time()) if issued_at is None else issued_at
    handoff = freeze_handoff(
        fixture.evidence,
        inventory=fixture.inventory,
        inventory_consumer="change-gate-runtime-integration",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION",),
    )
    handoff_sha256 = handoff.digest()
    statement = expected_release_statement(purpose=purpose, handoff_sha256=handoff_sha256)
    receipt = HumanReleaseReceipt(
        purpose=purpose,
        handoff_sha256=handoff_sha256,
        action_fingerprint=fingerprint_user_action(statement),
        turn_id_sha256="1" * 64,
        session_scope_sha256="2" * 64,
        platform_scope_sha256="3" * 64,
        user_message_index=1,
        source_role="user",
    )
    return DurableReleaseArtifact(
        release_id=release_id,
        purpose=purpose,
        handoff_sha256=handoff_sha256,
        evidence_sha256=fixture.evidence.digest(),
        inventory_sha256=handoff.inventory_sha256,
        artifact_set_sha256=_artifact_set_sha256(handoff),
        route_sha256=canonical_sha256(handoff.route),
        task_id=fixture.task_id,
        work_id=fixture.evidence.work.work_id,
        source=fixture.source,
        authority_receipt=receipt,
        issued_at_epoch=now,
        expires_at_epoch=now + 300,
    )


def _attach_runtime_artifacts(
    conn: sqlite3.Connection,
    tmp_path: Path,
    task_id: str,
    *,
    risk: RiskLevel = RiskLevel.NORMAL,
) -> RuntimeFixture:
    repo, source, binding = _source_repo(tmp_path)
    now = int(time.time())
    route = _route(risk)
    evidence = EvidencePacket(
        source=source,
        work=WorkIdentity(
            task_id=task_id,
            run_id="synthetic-run",
            work_id=f"work-{task_id}",
            operation="apply-source-candidate",
            effect="SOURCE_CHANGE",
        ),
        risk=risk,
        route=route,
        allowed_paths=("gate-artifact.txt",),
        required_inputs=(),
        produced_artifacts=(binding,),
        inventory_id=f"inventory-{task_id}",
        created_at_epoch=now - 1,
        expires_at_epoch=now + 599,
    )
    inventory = ArchitectureInventoryRecord(
        schema=ARCHITECTURE_INVENTORY_SCHEMA,
        inventory_id=evidence.inventory_id,
        capability="change-gate-runtime-integration",
        owner="change-gate-contract-owner",
        consumers=("change-gate-runtime-integration",),
        authority_contract="current-turn-frozen-handoff",
        activation_state="DEFAULT_OFF",
        source_paths=("hermes_cli/change_gate.py", "hermes_cli/kanban_db.py"),
        artifact_paths=("gate-artifact.txt",),
        risk=risk,
        blast_radius=("claim", "g4", "review"),
    )
    handoff = freeze_handoff(
        evidence,
        inventory=inventory,
        inventory_consumer="change-gate-runtime-integration",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION",),
    )
    conn.execute(
        "UPDATE tasks SET workspace_path = ?, assignee = ?, model_override = ?, "
        "provider_override = ?, reasoning_effort = ? WHERE id = ?",
        (
            str(repo),
            route.executor.assignee,
            route.executor.model_override,
            route.executor.provider_override,
            route.executor.reasoning_effort,
            task_id,
        ),
    )
    inventory_root = tmp_path / "inventory"
    inventory_root.mkdir(exist_ok=True)
    (inventory_root / f"{inventory.inventory_id}.json").write_bytes(encode_artifact(inventory))
    kb.store_attachment_bytes(
        conn,
        task_id,
        filename="change-gate-evidence.json",
        data=encode_artifact(evidence),
        content_type="application/json",
    )
    kb.store_attachment_bytes(
        conn,
        task_id,
        filename="change-gate-frozen-handoff.json",
        data=encode_artifact(handoff),
        content_type="application/json",
    )
    return RuntimeFixture(
        task_id=task_id,
        source=source,
        evidence=evidence,
        inventory=inventory,
        handoff_sha256=handoff.digest(),
    )


def _enable_runtime(
    monkeypatch: pytest.MonkeyPatch,
    inventory_root: Path,
    *,
    max_corrections: int = 1,
) -> None:
    import hermes_cli.change_gate_runtime as runtime

    policy = ChangeGateRuntimePolicy(
        enabled=True,
        valid=True,
        inventory_root=inventory_root,
        max_corrections=max_corrections,
    )
    monkeypatch.setattr(runtime, "load_runtime_policy", lambda config=None: policy)


def _snapshot(conn: sqlite3.Connection, task_id: str) -> dict[str, object]:
    task = conn.execute(
        "SELECT status, assignee, claim_lock, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return {
        "task": tuple(task),
        "runs": conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0],
    }


def _create_ready_task(conn: sqlite3.Connection) -> str:
    return kb.create_task(
        conn,
        title="change gate runtime integration",
        assignee="executor",
        model_override="fixture-model",
        provider_override="fixture-provider",
        reasoning_effort="medium",
    )


def _store_release(
    conn: sqlite3.Connection,
    fixture: RuntimeFixture,
    purpose: ReleasePurpose,
    suffix: str,
    *,
    issued_at: int | None = None,
) -> str:
    kb.initialize_change_gate_runtime_schema(conn)
    release = _release(
        fixture,
        purpose,
        release_id="cgr_" + suffix * 64,
        issued_at=int(time.time()) - 1 if issued_at is None else issued_at,
    )
    return kb._store_change_gate_release(conn, release)


def test_default_off_passthrough_does_not_create_runtime_table(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.change_gate_runtime as runtime

    monkeypatch.setattr(runtime, "load_runtime_policy", lambda config=None: ChangeGateRuntimePolicy())
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)

        claimed = kb.claim_task(conn, task_id)

        assert claimed is not None
        assert claimed.status == "running"
        assert not kb.change_gate_runtime_schema_exists(conn)


def test_enabled_claim_without_release_has_zero_domain_mutation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        before = _snapshot(conn, task_id)

        claimed = kb.claim_task(conn, task_id)

        assert claimed is None
        assert _snapshot(conn, task_id) == before


def test_manual_force_review_cannot_impersonate_claim_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        before = _snapshot(conn, task_id)

        ok, reason = kb.request_review(
            conn,
            task_id,
            summary="manual force must not become authority",
            force=True,
            with_reason=True,
        )

        assert ok is False
        assert reason == "change_gate_review_run_binding_required"
        assert _snapshot(conn, task_id) == before


def test_enabled_dispatcher_denial_has_no_claim_event_or_spawn(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        before = _snapshot(conn, task_id)
        spawns: list[str] = []

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args, **_kwargs: spawns.append(task.id),
            reconcile_orphans=False,
        )

        assert spawns == []
        assert result.spawned == []
        assert result.change_gate_denied == [
            (task_id, ChangeGateReason.RELEASE_MISSING.value)
        ]
        assert _snapshot(conn, task_id) == before


def test_enabled_ungated_historical_task_uses_explicit_passthrough_policy(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    inventory_root = tmp_path / "inventory"
    inventory_root.mkdir()
    _enable_runtime(monkeypatch, inventory_root)
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 4242,
            reconcile_orphans=False,
        )

        assert [row[0] for row in result.spawned] == [task_id]
        assert result.change_gate_denied == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"


def test_claim_consumes_release_once_and_replay_does_not_mutate_domain(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        release_id = _store_release(conn, fixture, ReleasePurpose.CLAIM, "a")

        claimed = kb.claim_task(conn, task_id)
        after_claim = _snapshot(conn, task_id)
        replay = kb.claim_task(conn, task_id)

        state = kb.change_gate_release_state(conn, release_id)
        assert claimed is not None
        assert state is not None
        assert state["state"] == "CONSUMED"
        assert state["consumed_from_status"] == "ready"
        assert state["consumed_to_status"] == "running"
        assert replay is None
        assert _snapshot(conn, task_id) == after_claim


def test_concurrent_claim_release_has_exactly_one_winner(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        release_id = _store_release(conn, fixture, ReleasePurpose.CLAIM, "7")

    barrier = threading.Barrier(2)

    def _claim() -> bool:
        with _connect(db_path) as worker_conn:
            barrier.wait(timeout=5)
            return kb.claim_task(worker_conn, task_id) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(lambda _index: _claim(), range(2)))

    with _connect(db_path) as conn:
        assert winners.count(True) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'claimed'",
            (task_id,),
        ).fetchone()[0] == 1
        state = kb.change_gate_release_state(conn, release_id)
        assert state is not None
        assert state["state"] == "CONSUMED"


def test_failed_claim_cas_and_source_drift_do_not_consume_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        release_id = _store_release(conn, fixture, ReleasePurpose.CLAIM, "8")
        before = _snapshot(conn, task_id)

        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        conn.commit()
        failed_cas = kb.claim_task(conn, task_id)
        state_after_cas = kb.change_gate_release_state(conn, release_id)

        assert failed_cas is None
        assert state_after_cas is not None
        assert state_after_cas["state"] == "ISSUED"
        assert _snapshot(conn, task_id)["runs"] == before["runs"]
        assert _snapshot(conn, task_id)["events"] == before["events"]

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        conn.commit()
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        workspace = task.workspace_path
        (Path(workspace) / "gate-artifact.txt").write_text(
            "drifted synthetic artifact\n",
            encoding="utf-8",
        )
        before_drift = _snapshot(conn, task_id)
        drifted = kb.claim_task(conn, task_id)

        assert drifted is None
        assert _snapshot(conn, task_id) == before_drift
        state_after_drift = kb.change_gate_release_state(conn, release_id)
        assert state_after_drift is not None
        assert state_after_drift["state"] == "ISSUED"


def test_expired_claim_release_has_zero_domain_mutation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        release_id = _store_release(
            conn,
            fixture,
            ReleasePurpose.CLAIM,
            "6",
            issued_at=int(time.time()) - 400,
        )
        before = _snapshot(conn, task_id)

        assert kb.claim_task(conn, task_id) is None
        assert _snapshot(conn, task_id) == before
        state = kb.change_gate_release_state(conn, release_id)
        assert state is not None
        assert state["state"] == "ISSUED"


def test_g4_cannot_complete_without_release_and_consumes_release_once(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _store_release(conn, fixture, ReleasePurpose.CLAIM, "b")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = int(claimed.current_run_id)
        assert kb.request_review(conn, task_id, expected_run_id=run_id)
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        review_run_id = int(review.current_run_id)
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=review_run_id,
            change_gate_review={
                "reviewer_class": "REVIEWER",
                "verdict": "PASS",
                "finding_codes": [],
            },
        )
        after_review = kb.get_task(conn, task_id)
        assert after_review is not None
        assert after_review.status == "review"
        assert after_review.assignee == "executor"
        before_bypass = _snapshot(conn, task_id)

        bypassed = kb.complete_task(conn, task_id, result="done")
        after_bypass = _snapshot(conn, task_id)
        g4_release_id = _store_release(conn, fixture, ReleasePurpose.G4, "c")
        g4_evaluation = kb.evaluate_change_gate_g4_runtime(conn, task_id)
        assert g4_evaluation.result.allowed, g4_evaluation.result.reason.value
        completed = kb.complete_task(conn, task_id, result="done")
        replay_completed = kb.complete_task(conn, task_id, result="done again")

        state = kb.change_gate_release_state(conn, g4_release_id)
        assert bypassed is False
        assert after_bypass == before_bypass
        assert completed is True
        assert replay_completed is False
        assert state is not None
        assert state["state"] == "CONSUMED"
        assert state["consumed_to_status"] == "done"


def test_high_risk_review_projection_requires_normal_then_deep_passes(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id, risk=RiskLevel.HIGH)
        _enable_runtime(monkeypatch, tmp_path / "inventory")

        claim_statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )
        with _scoped_test_current_turn_user_authority(
            claim_statement + " trailing-text",
            session_id="synthetic-change-gate",
            turn_id="wrong-release-turn",
            platform_scope="manual",
        ):
            wrong_statement = issue_change_gate_release(
                task_id=task_id,
                purpose=ReleasePurpose.CLAIM.value,
            )
        assert not wrong_statement.ok
        assert wrong_statement.reason == ChangeGateReason.RELEASE_RECEIPT_MISSING.value
        assert conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases"
        ).fetchone()[0] == 0

        with _scoped_test_current_turn_user_authority(
            claim_statement,
            session_id="synthetic-change-gate",
            turn_id="claim-release-turn",
            platform_scope="manual",
        ):
            claim_release = issue_change_gate_release(
                task_id=task_id,
                purpose=ReleasePurpose.CLAIM.value,
            )
        assert claim_release.ok, claim_release.reason

        # The dispatcher-side claim succeeds after the foreground ContextVar
        # has been cleared: only the persisted short-lived release remains.
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(conn, task_id, expected_run_id=int(claimed.current_run_id))

        normal = kb.claim_review_task(conn, task_id)
        assert normal is not None
        assert normal.assignee == "reviewer-normal"
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(normal.current_run_id),
            change_gate_review={
                "reviewer_class": "NORMAL",
                "verdict": "PASS",
                "finding_codes": [],
            },
        )

        deep = kb.claim_review_task(conn, task_id)
        assert deep is not None
        assert deep.assignee == "reviewer-deep"
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(deep.current_run_id),
            change_gate_review={
                "reviewer_class": "DEEP",
                "verdict": "PASS",
                "finding_codes": [],
            },
        )
        final_route = kb.get_task(conn, task_id)
        assert final_route is not None
        assert final_route.assignee == "executor"
        denied_extra_review = kb.claim_review_task(conn, task_id)
        projection = project_upstream_reviews(
            conn,
            task_id,
            handoff=freeze_handoff(
                fixture.evidence,
                inventory=fixture.inventory,
                inventory_consumer="change-gate-runtime-integration",
                scope=("SOURCE_CHANGE",),
                forbidden_effects=("LIVE_SERVICE_MUTATION",),
            ),
        )
        metadata_rows = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL",
            (task_id,),
        ).fetchall()

        assert denied_extra_review is None
        assert projection.ok
        assert [review.reviewer_class for review in projection.reviews] == [
            ReviewerClass.NORMAL,
            ReviewerClass.DEEP,
        ]
        assert all(REVIEW_METADATA_KEY in json.loads(row["metadata"]) for row in metadata_rows)

        # Cross the original Evidence Packet's 600-second freshness window.
        # G4 must still succeed because immutable bindings remain exact and it
        # receives its own fresh short-lived foreground release.
        future_epoch = fixture.evidence.expires_at_epoch + 101
        monkeypatch.setattr(time, "time", lambda: future_epoch)
        g4_statement = expected_release_statement(
            purpose=ReleasePurpose.G4,
            handoff_sha256=fixture.handoff_sha256,
        )
        with _scoped_test_current_turn_user_authority(
            g4_statement,
            session_id="synthetic-change-gate",
            turn_id="g4-release-turn",
            platform_scope="manual",
        ):
            g4_release = issue_change_gate_release(
                task_id=task_id,
                purpose=ReleasePurpose.G4.value,
            )
        assert g4_release.ok, g4_release.reason
        assert kb.complete_task(conn, task_id, result="synthetic E2E complete")
        assert kb.get_task(conn, task_id).status == "done"
        g4_state = kb.change_gate_release_state(conn, g4_release.release_id)
        assert g4_state is not None
        assert g4_state["state"] == "CONSUMED"


@pytest.mark.parametrize(
    ("verdict", "max_corrections", "expected_reason"),
    (
        (
            ReviewVerdict.REQUEST_CHANGES,
            0,
            ChangeGateReason.CORRECTION_LIMIT_EXCEEDED,
        ),
        (
            ReviewVerdict.REPLAN_REQUIRED,
            1,
            ChangeGateReason.REVIEW_REPLAN_REQUIRED,
        ),
    ),
)
def test_review_consequence_routes_one_bounded_planner_parent(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: ReviewVerdict,
    max_corrections: int,
    expected_reason: ChangeGateReason,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(
            monkeypatch,
            tmp_path / "inventory",
            max_corrections=max_corrections,
        )
        _store_release(conn, fixture, ReleasePurpose.CLAIM, "e")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(claimed.current_run_id),
        )
        reviewer = kb.claim_review_task(conn, task_id)
        assert reviewer is not None
        issued_g4 = _store_release(conn, fixture, ReleasePurpose.G4, "f")

        ok, implementer = kb.request_changes(
            conn,
            task_id,
            reason="bounded synthetic finding",
            expected_run_id=int(reviewer.current_run_id),
            change_gate_review={
                "reviewer_class": ReviewerClass.REVIEWER.value,
                "verdict": verdict.value,
                "finding_codes": ["bounded-finding"],
            },
        )

        assert ok
        assert implementer == "executor"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "todo"
        assert task.assignee == "executor"
        parents = kb.parent_ids(conn, task_id)
        assert len(parents) == 1
        planner = kb.get_task(conn, parents[0])
        assert planner is not None
        assert planner.assignee == "planner"
        assert planner.status == "ready"
        state = kb.change_gate_release_state(conn, issued_g4)
        assert state is not None
        assert state["state"] == "REVOKED"
        assert state["revoked_reason"] == expected_reason.value

        handoff = freeze_handoff(
            fixture.evidence,
            inventory=fixture.inventory,
            inventory_consumer="change-gate-runtime-integration",
            scope=("SOURCE_CHANGE",),
            forbidden_effects=("LIVE_SERVICE_MUTATION",),
        )
        projection = project_upstream_reviews(conn, task_id, handoff=handoff)
        assert projection.ok
        assert len(projection.reviews) == 1
        assert projection.reviews[0].verdict is verdict


def test_scope_deviation_consequence_is_idempotent_and_cannot_widen_scope(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        release_id = _store_release(conn, fixture, ReleasePurpose.CLAIM, "9")

        first = kb.apply_change_gate_planner_consequence(
            conn,
            task_id,
            decision=GateDecision.SCOPE_DEVIATION,
            reason=ChangeGateReason.SCOPE_DEVIATION,
            handoff_sha256=fixture.handoff_sha256,
            planner_assignee="planner",
        )
        second = kb.apply_change_gate_planner_consequence(
            conn,
            task_id,
            decision=GateDecision.SCOPE_DEVIATION,
            reason=ChangeGateReason.SCOPE_DEVIATION,
            handoff_sha256=fixture.handoff_sha256,
            planner_assignee="planner",
        )

        assert first.applied and second.applied
        assert first.planner_task_id == second.planner_task_id
        assert kb.parent_ids(conn, task_id) == [first.planner_task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "todo"
        release = kb.change_gate_release_state(conn, release_id)
        assert release is not None
        assert release["state"] == "REVOKED"
        assert release["revoked_reason"] == ChangeGateReason.SCOPE_DEVIATION.value
