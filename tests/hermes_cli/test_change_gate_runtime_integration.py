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
    FrozenHandoff,
    GateDecision,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewResult,
    ReviewRoute,
    ReviewerClass,
    ReviewVerdict,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    TransitionAnchor,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_sha256,
    evaluate_reviews,
    expected_release_statement,
    freeze_handoff,
)
from hermes_cli.change_gate_codec import encode_artifact
from hermes_cli.change_gate_release import (
    issue_change_gate_release,
    issue_current_turn_change_gate_release,
)
from hermes_cli.change_gate_runtime import (
    ChangeGateRuntimePolicy,
    REVIEW_METADATA_KEY,
    build_review_result_metadata,
    count_change_gate_corrections,
    load_task_gate_artifacts,
    project_upstream_reviews,
)
from tools.workflow_authority import (
    _scoped_test_current_turn_user_authority,
    fingerprint_user_action,
)
from gateway.session_context import clear_session_vars, set_session_vars


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


def _source_repo(
    tmp_path: Path,
    *,
    dirty_tracked: bool = False,
    tracked_outside: bool = False,
) -> tuple[Path, SourceIdentity, ArtifactBinding]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "track-g-test"], check=True)
    _run_git(repo, "config", "user.email", "test@example.invalid")
    _run_git(repo, "config", "user.name", "Test Engineer")
    _run_git(repo, "remote", "add", "origin", "git@github.com:Bichalla/hermes-agent.git")
    artifact = repo / "gate-artifact.txt"
    artifact.write_text("bounded synthetic artifact\n", encoding="utf-8")
    if tracked_outside:
        (repo / "outside.txt").write_text("outside baseline\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "fixture")
    source = SourceIdentity(
        repository="Bichalla/hermes-agent",
        branch="track-g-test",
        commit=_run_git(repo, "rev-parse", "HEAD"),
        tree=_run_git(repo, "rev-parse", "HEAD^{tree}"),
    )
    if dirty_tracked:
        artifact.write_text("bounded current artifact\n", encoding="utf-8")
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


def _handoff(fixture: RuntimeFixture) -> FrozenHandoff:
    return freeze_handoff(
        fixture.evidence,
        inventory=fixture.inventory,
        inventory_consumer="change-gate-runtime-integration",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION",),
    )


def _release(
    fixture: RuntimeFixture,
    purpose: ReleasePurpose,
    *,
    release_id: str,
    transition_anchor: TransitionAnchor,
    issued_at: int | None = None,
) -> DurableReleaseArtifact:
    now = int(time.time()) if issued_at is None else issued_at
    handoff = _handoff(fixture)
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
        transition_anchor=transition_anchor,
        issued_at_epoch=now,
        expires_at_epoch=now + 300,
    )


def _attach_runtime_artifacts(
    conn: sqlite3.Connection,
    tmp_path: Path,
    task_id: str,
    *,
    risk: RiskLevel = RiskLevel.NORMAL,
    dirty_tracked: bool = False,
    tracked_outside: bool = False,
    extra_allowed_paths: tuple[str, ...] = (),
) -> RuntimeFixture:
    repo, source, binding = _source_repo(
        tmp_path,
        dirty_tracked=dirty_tracked,
        tracked_outside=tracked_outside,
    )
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
        allowed_paths=tuple(sorted(("gate-artifact.txt",) + extra_allowed_paths)),
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
    inventory_root: Path | None = None,
    issued_at: int | None = None,
) -> str:
    import hermes_cli.change_gate_runtime as runtime

    kb.initialize_change_gate_runtime_schema(conn)
    policy = (
        ChangeGateRuntimePolicy(
            enabled=True,
            valid=True,
            inventory_root=inventory_root,
        )
        if inventory_root is not None
        else runtime.load_runtime_policy()
    )
    load = load_task_gate_artifacts(
        conn,
        fixture.task_id,
        policy=policy,
        attachment_root=kb.task_attachments_dir(fixture.task_id),
    )
    assert load.ok and load.artifacts is not None
    reviews = ()
    if purpose is ReleasePurpose.G4:
        projection = project_upstream_reviews(
            conn,
            fixture.task_id,
            handoff=load.artifacts.handoff,
        )
        assert projection.ok
        reviews = projection.reviews
    transition_anchor = kb.derive_change_gate_transition_anchor(
        conn,
        fixture.task_id,
        purpose=purpose,
        artifacts=load.artifacts,
        reviews=reviews,
    )
    assert transition_anchor is not None
    release = _release(
        fixture,
        purpose,
        release_id="cgr_" + suffix * 64,
        transition_anchor=transition_anchor,
        issued_at=int(time.time()) - 1 if issued_at is None else issued_at,
    )
    return kb._store_change_gate_release(conn, release)


def _current_turn_claim_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_path: Path,
    session_id: str,
) -> tuple[str, RuntimeFixture, str]:
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    task_id = _create_ready_task(conn)
    fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
    conn.execute(
        "UPDATE tasks SET session_id = ? WHERE id = ?",
        (session_id, task_id),
    )
    _enable_runtime(monkeypatch, tmp_path / "inventory")
    return (
        task_id,
        fixture,
        expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        ),
    )


def _current_turn_release_count(conn: sqlite3.Connection, task_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    )


def _issue_current_turn_claim(
    *,
    statement: str,
    session_id: str,
    turn_id: str,
):
    tokens = set_session_vars(platform="manual", session_id=session_id)
    try:
        with _scoped_test_current_turn_user_authority(
            statement,
            session_id=session_id,
            turn_id=turn_id,
            platform_scope="manual",
        ):
            return issue_current_turn_change_gate_release()
    finally:
        clear_session_vars(tokens)


def _claim_and_converge_normal_review(
    conn: sqlite3.Connection,
    fixture: RuntimeFixture,
    *,
    claim_suffix: str,
) -> int:
    _store_release(conn, fixture, ReleasePurpose.CLAIM, claim_suffix)
    claimed = kb.claim_task(conn, fixture.task_id)
    assert claimed is not None
    assert claimed.current_run_id is not None
    assert kb.request_review(
        conn,
        fixture.task_id,
        expected_run_id=int(claimed.current_run_id),
    )
    review = kb.claim_review_task(conn, fixture.task_id)
    assert review is not None
    assert review.current_run_id is not None
    review_run_id = int(review.current_run_id)
    assert kb.request_review(
        conn,
        fixture.task_id,
        expected_run_id=review_run_id,
        change_gate_review={
            "reviewer_class": ReviewerClass.REVIEWER.value,
            "verdict": ReviewVerdict.PASS.value,
            "finding_codes": [],
        },
    )
    return review_run_id


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


def test_detached_dispatcher_consumes_valid_release_and_spawns_once(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )
        with _scoped_test_current_turn_user_authority(
            statement,
            session_id="detached-dispatch",
            turn_id="foreground-release",
            platform_scope="manual",
        ):
            issued = issue_change_gate_release(
                task_id=task_id,
                purpose=ReleasePurpose.CLAIM.value,
        )
        assert issued.ok, issued.reason
        assert issued.release_id is not None

        spawns: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args, **_kwargs: (
                spawns.append(task.id) or 4242
            ),
            reconcile_orphans=False,
        )

        assert spawns == [task_id]
        assert [item[0] for item in result.spawned] == [task_id]
        assert result.change_gate_denied == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'claimed'",
            (task_id,),
        ).fetchone()[0] == 1
        state = kb.change_gate_release_state(conn, issued.release_id)
        assert state is not None
        assert state["state"] == "CONSUMED"
        assert state["consumed_run_id"] == task.current_run_id


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


def test_claim_allows_observed_allowed_bound_tracked_change(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(
            conn,
            tmp_path,
            task_id,
            dirty_tracked=True,
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _store_release(conn, fixture, ReleasePurpose.CLAIM, "0")

        evaluation = kb.evaluate_change_gate_claim_runtime(conn, task_id)
        claimed = kb.claim_task(conn, task_id)

        assert evaluation.result.allowed, evaluation.result.reason.value
        assert evaluation.artifacts is not None
        assert evaluation.artifacts.workspace_observation.changed_paths == (
            "gate-artifact.txt",
        )
        assert claimed is not None


def test_claim_reobserves_tracked_outside_scope_as_scope_deviation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(
            conn,
            tmp_path,
            task_id,
            tracked_outside=True,
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        (Path(task.workspace_path) / "outside.txt").write_text(
            "outside changed\n",
            encoding="utf-8",
        )

        evaluation = kb.evaluate_change_gate_claim_runtime(conn, task_id)

        assert evaluation.result.decision is GateDecision.SCOPE_DEVIATION
        assert evaluation.result.reason is ChangeGateReason.SCOPE_DEVIATION


def test_claim_reobserves_untracked_outside_scope_as_scope_deviation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        (Path(task.workspace_path) / "outside.txt").write_text(
            "outside untracked\n",
            encoding="utf-8",
        )

        evaluation = kb.evaluate_change_gate_claim_runtime(conn, task_id)

        assert evaluation.result.decision is GateDecision.SCOPE_DEVIATION
        assert evaluation.result.reason is ChangeGateReason.SCOPE_DEVIATION


def test_dispatch_scope_deviation_from_actual_observation_routes_planner_consequence(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _store_release(conn, fixture, ReleasePurpose.CLAIM, "2")
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        (Path(task.workspace_path) / "outside.txt").write_text(
            "outside untracked\n",
            encoding="utf-8",
        )

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 4242,
            reconcile_orphans=False,
        )

        assert result.spawned == []
        assert result.change_gate_denied == [
            (task_id, ChangeGateReason.SCOPE_DEVIATION.value)
        ]
        parents = kb.parent_ids(conn, task_id)
        assert len(parents) == 1
        planner = kb.get_task(conn, parents[0])
        assert planner is not None
        assert planner.assignee == "planner"
        assert planner.status == "ready"


def test_claim_reobserves_allowed_unbound_path_as_artifact_mismatch(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(
            conn,
            tmp_path,
            task_id,
            extra_allowed_paths=("allowed-unbound.txt",),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        (Path(task.workspace_path) / "allowed-unbound.txt").write_text(
            "allowed but unbound\n",
            encoding="utf-8",
        )

        evaluation = kb.evaluate_change_gate_claim_runtime(conn, task_id)

        assert not evaluation.result.allowed
        assert evaluation.result.reason is ChangeGateReason.ARTIFACT_BINDING_MISMATCH


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


def test_wrong_task_release_has_zero_domain_mutation(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        release_root = tmp_path / "release-task"
        target_root = tmp_path / "target-task"
        release_root.mkdir()
        target_root.mkdir()
        release_task_id = _create_ready_task(conn)
        release_fixture = _attach_runtime_artifacts(
            conn,
            release_root,
            release_task_id,
        )
        target_task_id = _create_ready_task(conn)
        _attach_runtime_artifacts(
            conn,
            target_root,
            target_task_id,
        )
        _enable_runtime(monkeypatch, target_root / "inventory")
        release_id = _store_release(
            conn,
            release_fixture,
            ReleasePurpose.CLAIM,
            "4",
            inventory_root=release_root / "inventory",
        )
        before = _snapshot(conn, target_task_id)

        assert kb.claim_task(conn, target_task_id) is None
        assert _snapshot(conn, target_task_id) == before
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
        assert claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        assert kb.request_review(conn, task_id, expected_run_id=run_id)
        review = kb.claim_review_task(conn, task_id)
        assert review is not None
        assert review.current_run_id is not None
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


def test_failed_g4_consumption_rolls_back_terminal_transition(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _claim_and_converge_normal_review(conn, fixture, claim_suffix="5")
        release_id = _store_release(conn, fixture, ReleasePurpose.G4, "d")
        before = _snapshot(conn, task_id)
        monkeypatch.setattr(
            kb,
            "_mark_change_gate_release_consumed",
            lambda *_args, **_kwargs: False,
        )

        assert kb.complete_task(conn, task_id, result="must roll back") is False
        assert _snapshot(conn, task_id) == before
        state = kb.change_gate_release_state(conn, release_id)
        assert state is not None
        assert state["state"] == "ISSUED"
        assert state["consumed_run_id"] is None
        assert state["consumed_event_id"] is None


def test_g4_reobserves_artifact_drift_before_terminal_transition(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _claim_and_converge_normal_review(conn, fixture, claim_suffix="6")
        release_id = _store_release(conn, fixture, ReleasePurpose.G4, "1")
        task = kb.get_task(conn, task_id)
        assert task is not None and task.workspace_path is not None
        (Path(task.workspace_path) / "gate-artifact.txt").write_text(
            "drift before g4\n",
            encoding="utf-8",
        )
        before = _snapshot(conn, task_id)

        evaluation = kb.evaluate_change_gate_g4_runtime(conn, task_id)
        completed = kb.complete_task(conn, task_id, result="must not complete")

        assert not evaluation.result.allowed
        assert evaluation.result.reason is ChangeGateReason.ARTIFACT_DIGEST_MISMATCH
        assert completed is False
        assert _snapshot(conn, task_id) == before
        state = kb.change_gate_release_state(conn, release_id)
        assert state is not None
        assert state["state"] == "ISSUED"
        assert state["consumed_run_id"] is None


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
        assert claimed.current_run_id is not None
        assert kb.request_review(conn, task_id, expected_run_id=int(claimed.current_run_id))

        normal = kb.claim_review_task(conn, task_id)
        assert normal is not None
        assert normal.current_run_id is not None
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
        assert deep.current_run_id is not None
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
            handoff=_handoff(fixture),
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
            g4_statement + " trailing-text",
            session_id="synthetic-change-gate",
            turn_id="wrong-g4-release-turn",
            platform_scope="manual",
        ):
            wrong_g4_statement = issue_change_gate_release(
                task_id=task_id,
                purpose=ReleasePurpose.G4.value,
            )
        assert not wrong_g4_statement.ok
        assert (
            wrong_g4_statement.reason
            == ChangeGateReason.RELEASE_RECEIPT_MISSING.value
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
        assert g4_release.release_id is not None
        assert kb.complete_task(conn, task_id, result="synthetic E2E complete")
        final_task = kb.get_task(conn, task_id)
        assert final_task is not None
        assert final_task.status == "done"
        g4_state = kb.change_gate_release_state(conn, g4_release.release_id)
        assert g4_state is not None
        assert g4_state["state"] == "CONSUMED"


def test_high_projection_rejects_interleaved_row_claiming_latest_deep_attempt(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(
            conn,
            tmp_path,
            task_id,
            risk=RiskLevel.HIGH,
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        _store_release(conn, fixture, ReleasePurpose.CLAIM, "d")
        executor = kb.claim_task(conn, task_id)
        assert executor is not None and executor.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(executor.current_run_id),
        )

        normal = kb.claim_review_task(conn, task_id)
        assert normal is not None and normal.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(normal.current_run_id),
            change_gate_review={
                "reviewer_class": ReviewerClass.NORMAL.value,
                "verdict": ReviewVerdict.PASS.value,
                "finding_codes": [],
            },
        )

        placeholder = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'reviewer-deep', 'review_requested', 1, 2, 'review_requested')",
            (task_id,),
        )
        assert placeholder.lastrowid is not None
        placeholder_id = int(placeholder.lastrowid)

        deep = kb.claim_review_task(conn, task_id)
        assert deep is not None and deep.current_run_id is not None
        deep_run_id = int(deep.current_run_id)
        assert placeholder_id < deep_run_id
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=deep_run_id,
            change_gate_review={
                "reviewer_class": ReviewerClass.DEEP.value,
                "verdict": ReviewVerdict.PASS.value,
                "finding_codes": [],
            },
        )
        deep_row = conn.execute(
            "SELECT profile, ended_at FROM task_runs WHERE id = ?",
            (deep_run_id,),
        ).fetchone()
        assert deep_row is not None
        collision = ReviewResult(
            bundle_sha256=_handoff(fixture).review_bundle_sha256(),
            reviewer_class=ReviewerClass.DEEP,
            reviewer_identity=str(deep_row["profile"]),
            attempt_id=str(deep_run_id),
            verdict=ReviewVerdict.PASS,
            finding_codes=(),
            completed_at_epoch=int(deep_row["ended_at"]),
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(build_review_result_metadata(collision)), placeholder_id),
        )
        conn.commit()

        projection = project_upstream_reviews(
            conn,
            task_id,
            handoff=_handoff(fixture),
        )
        assert not projection.ok
        assert projection.reason is ChangeGateReason.REVIEW_RESULT_MALFORMED


def test_bounded_correction_reaches_latest_pass_and_atomic_g4(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(
            conn,
            tmp_path,
            task_id,
            dirty_tracked=True,
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")

        def issue(purpose: ReleasePurpose, turn: str):
            statement = expected_release_statement(
                purpose=purpose,
                handoff_sha256=fixture.handoff_sha256,
            )
            with _scoped_test_current_turn_user_authority(
                statement,
                session_id="synthetic-change-gate-correction",
                turn_id=turn,
                platform_scope="manual",
            ):
                return issue_change_gate_release(
                    task_id=task_id,
                    purpose=purpose.value,
                )

        first_claim_release = issue(ReleasePurpose.CLAIM, "correction-claim-1")
        assert first_claim_release.ok, first_claim_release.reason
        first_executor = kb.claim_task(conn, task_id)
        assert first_executor is not None and first_executor.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(first_executor.current_run_id),
        )

        first_reviewer = kb.claim_review_task(conn, task_id)
        assert first_reviewer is not None and first_reviewer.current_run_id is not None
        changed, implementer = kb.request_changes(
            conn,
            task_id,
            reason="bounded correction required",
            expected_run_id=int(first_reviewer.current_run_id),
            change_gate_review={
                "reviewer_class": ReviewerClass.REVIEWER.value,
                "verdict": ReviewVerdict.REQUEST_CHANGES.value,
                "finding_codes": ["bounded-correction"],
            },
        )
        assert changed and implementer == "executor"
        assert count_change_gate_corrections(conn, task_id) == 1

        second_claim_release = issue(ReleasePurpose.CLAIM, "correction-claim-2")
        assert second_claim_release.ok, second_claim_release.reason
        second_executor = kb.claim_task(conn, task_id)
        assert second_executor is not None and second_executor.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(second_executor.current_run_id),
        )

        second_reviewer = kb.claim_review_task(conn, task_id)
        assert second_reviewer is not None and second_reviewer.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(second_reviewer.current_run_id),
            change_gate_review={
                "reviewer_class": ReviewerClass.REVIEWER.value,
                "verdict": ReviewVerdict.PASS.value,
                "finding_codes": [],
            },
        )

        handoff = _handoff(fixture)
        projection = project_upstream_reviews(conn, task_id, handoff=handoff)
        assert projection.ok
        assert len(projection.reviews) == 1
        assert projection.reviews[0].attempt_id == str(second_reviewer.current_run_id)
        assert projection.reviews[0].verdict is ReviewVerdict.PASS
        assert count_change_gate_corrections(conn, task_id) == 1

        g4_release = issue(ReleasePurpose.G4, "correction-g4")
        assert g4_release.ok, g4_release.reason
        assert g4_release.release_id is not None
        assert kb.complete_task(conn, task_id, result="bounded correction complete")
        final_task = kb.get_task(conn, task_id)
        assert final_task is not None and final_task.status == "done"
        g4_state = kb.change_gate_release_state(conn, g4_release.release_id)
        assert g4_state is not None
        assert g4_state["state"] == "CONSUMED"


def test_runtime_projection_rejects_latest_wrong_bundle_review(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        review_run_id = _claim_and_converge_normal_review(
            conn,
            fixture,
            claim_suffix="1",
        )
        row = conn.execute(
            "SELECT profile, ended_at FROM task_runs WHERE id = ?",
            (review_run_id,),
        ).fetchone()
        assert row is not None
        wrong_bundle = ReviewResult(
            bundle_sha256="0" * 64,
            reviewer_class=ReviewerClass.REVIEWER,
            reviewer_identity=str(row["profile"]),
            attempt_id=str(review_run_id),
            verdict=ReviewVerdict.PASS,
            finding_codes=(),
            completed_at_epoch=int(row["ended_at"]),
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (
                json.dumps(build_review_result_metadata(wrong_bundle)),
                review_run_id,
            ),
        )
        conn.commit()

        handoff = _handoff(fixture)
        projection = project_upstream_reviews(conn, task_id, handoff=handoff)
        aggregate = evaluate_reviews(
            projection.reviews,
            bundle_sha256=handoff.review_bundle_sha256(),
            required_reviewers=handoff.route.required_reviewers,
        )

        assert not projection.ok
        assert projection.reviews == ()
        assert projection.reason is ChangeGateReason.REVIEW_BUNDLE_MISMATCH
        assert not aggregate.allowed
        assert aggregate.reason is ChangeGateReason.REVIEW_MISSING_REQUIRED_CLASS


def test_runtime_projection_rejects_malformed_current_review_metadata(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        review_run_id = _claim_and_converge_normal_review(
            conn,
            fixture,
            claim_suffix="2",
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps({REVIEW_METADATA_KEY: "{"}), review_run_id),
        )
        conn.commit()

        projection = project_upstream_reviews(
            conn,
            task_id,
            handoff=_handoff(fixture),
        )

        assert not projection.ok
        assert projection.reviews == ()
        assert projection.reason is ChangeGateReason.REVIEW_RESULT_MALFORMED


def test_runtime_projection_uses_latest_current_role_without_older_fallback(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        review_run_id = _claim_and_converge_normal_review(
            conn,
            fixture,
            claim_suffix="3",
        )
        original = conn.execute(
            "SELECT profile, ended_at FROM task_runs WHERE id = ?",
            (review_run_id,),
        ).fetchone()
        claimed = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
            (task_id, review_run_id),
        ).fetchone()
        assert original is not None
        assert claimed is not None
        duplicate_end = int(original["ended_at"]) + 1
        cursor = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, ?, 'review_requested', ?, ?, 'review_requested')",
            (
                task_id,
                original["profile"],
                duplicate_end - 1,
                duplicate_end,
            ),
        )
        assert cursor.lastrowid is not None
        duplicate_run_id = int(cursor.lastrowid)
        duplicate = ReviewResult(
            bundle_sha256=_handoff(fixture).review_bundle_sha256(),
            reviewer_class=ReviewerClass.REVIEWER,
            reviewer_identity=str(original["profile"]),
            attempt_id=str(duplicate_run_id),
            verdict=ReviewVerdict.PASS,
            finding_codes=(),
            completed_at_epoch=duplicate_end,
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (
                json.dumps(build_review_result_metadata(duplicate)),
                duplicate_run_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'claimed', ?, ?)",
            (task_id, duplicate_run_id, claimed["payload"], duplicate_end - 1),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'review_requested', '{}', ?)",
            (task_id, duplicate_run_id, duplicate_end),
        )
        conn.commit()

        handoff = _handoff(fixture)
        projection = project_upstream_reviews(conn, task_id, handoff=handoff)
        aggregate = evaluate_reviews(
            projection.reviews,
            bundle_sha256=handoff.review_bundle_sha256(),
            required_reviewers=handoff.route.required_reviewers,
        )

        assert projection.ok
        assert projection.reviews == (duplicate,)
        assert aggregate.allowed


def test_runtime_projection_keeps_latest_request_changes_over_older_pass(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        older_run_id = _claim_and_converge_normal_review(
            conn,
            fixture,
            claim_suffix="4",
        )
        older = conn.execute(
            "SELECT profile, ended_at FROM task_runs WHERE id = ?",
            (older_run_id,),
        ).fetchone()
        claimed = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
            (task_id, older_run_id),
        ).fetchone()
        assert older is not None
        assert claimed is not None
        newer_end = int(older["ended_at"]) + 1
        cursor = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, ?, 'changes_requested', ?, ?, 'changes_requested')",
            (
                task_id,
                older["profile"],
                newer_end - 1,
                newer_end,
            ),
        )
        assert cursor.lastrowid is not None
        newer_run_id = int(cursor.lastrowid)
        newer = ReviewResult(
            bundle_sha256=_handoff(fixture).review_bundle_sha256(),
            reviewer_class=ReviewerClass.REVIEWER,
            reviewer_identity=str(older["profile"]),
            attempt_id=str(newer_run_id),
            verdict=ReviewVerdict.REQUEST_CHANGES,
            finding_codes=("latest-finding",),
            completed_at_epoch=newer_end,
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (
                json.dumps(build_review_result_metadata(newer)),
                newer_run_id,
            ),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'claimed', ?, ?)",
            (task_id, newer_run_id, claimed["payload"], newer_end - 1),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'changes_requested', '{}', ?)",
            (task_id, newer_run_id, newer_end),
        )
        conn.commit()

        handoff = _handoff(fixture)
        projection = project_upstream_reviews(conn, task_id, handoff=handoff)
        aggregate = evaluate_reviews(
            projection.reviews,
            bundle_sha256=handoff.review_bundle_sha256(),
            required_reviewers=handoff.route.required_reviewers,
        )

        assert projection.ok
        assert projection.reviews == (newer,)
        assert not aggregate.allowed
        assert aggregate.decision is GateDecision.REQUEST_CHANGES
        assert aggregate.reason is ChangeGateReason.REVIEW_REQUEST_CHANGES


def test_runtime_projection_rejects_older_attempt_id_collision_with_latest(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _connect(tmp_path / "kanban.db") as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        latest_run_id = _claim_and_converge_normal_review(
            conn,
            fixture,
            claim_suffix="5",
        )
        latest = conn.execute(
            "SELECT profile, ended_at FROM task_runs WHERE id = ?",
            (latest_run_id,),
        ).fetchone()
        assert latest is not None
        cursor = conn.execute(
            "INSERT INTO task_runs "
            "(task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, ?, 'review_requested', ?, ?, 'review_requested')",
            (
                task_id,
                latest["profile"],
                int(latest["ended_at"]) - 20,
                int(latest["ended_at"]) - 10,
            ),
        )
        assert cursor.lastrowid is not None
        older_row_id = int(cursor.lastrowid)
        collision = ReviewResult(
            bundle_sha256=_handoff(fixture).review_bundle_sha256(),
            reviewer_class=ReviewerClass.REVIEWER,
            reviewer_identity=str(latest["profile"]),
            attempt_id=str(latest_run_id),
            verdict=ReviewVerdict.PASS,
            finding_codes=(),
            completed_at_epoch=int(latest["ended_at"]),
        )
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (
                json.dumps(build_review_result_metadata(collision)),
                older_row_id,
            ),
        )
        conn.commit()

        projection = project_upstream_reviews(
            conn,
            task_id,
            handoff=_handoff(fixture),
        )

        assert not projection.ok
        assert projection.reason is ChangeGateReason.REVIEW_RESULT_MALFORMED


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
        assert claimed.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(claimed.current_run_id),
        )
        reviewer = kb.claim_review_task(conn, task_id)
        assert reviewer is not None
        assert reviewer.current_run_id is not None
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
        assert conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases "
            "WHERE task_id = ? AND purpose = 'G4'",
            (task_id,),
        ).fetchone()[0] == 0

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


def test_current_turn_host_adapter_claim_resolves_session_and_reuses_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            ("session-claim", task_id),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )

        tokens = set_session_vars(platform="manual", session_id="session-claim")
        try:
            with _scoped_test_current_turn_user_authority(
                statement,
                session_id="session-claim",
                turn_id="host-claim-turn",
                platform_scope="manual",
            ):
                first = issue_current_turn_change_gate_release()
                second = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert first.ok
        assert first.status == "issued"
        assert first.task_id == task_id
        assert first.purpose == ReleasePurpose.CLAIM.value
        assert first.release_id is not None
        assert second.ok
        assert second.status == "existing_idempotent"
        assert second.release_id == first.release_id
        assert conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0] == 1


def test_current_turn_same_authority_revoked_release_does_not_reissue(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-revoked-same",
        )

        tokens = set_session_vars(platform="manual", session_id="session-revoked-same")
        try:
            with _scoped_test_current_turn_user_authority(
                statement,
                session_id="session-revoked-same",
                turn_id="same-revoked-turn",
                platform_scope="manual",
            ):
                first = issue_current_turn_change_gate_release()
                assert first.ok
                assert first.release_id is not None
                assert kb.revoke_change_gate_releases(
                    conn,
                    task_id,
                    reason=ChangeGateReason.RELEASE_REVOKED,
                ) == 1
                second = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1
        state = kb.change_gate_release_state(conn, first.release_id)
        assert state is not None
        assert state["state"] == "REVOKED"


def test_current_turn_same_authority_expired_release_does_not_reissue(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-expired-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-expired-same",
            turn_id="same-expired-turn",
        )
        assert first.ok
        assert first.issued_at_epoch is not None
        monkeypatch.setattr(time, "time", lambda: first.issued_at_epoch + 301)

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-expired-same",
            turn_id="same-expired-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_same_authority_consumed_release_does_not_reissue(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-consumed-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-consumed-same",
            turn_id="same-consumed-turn",
        )
        assert first.ok
        assert first.release_id is not None
        assert first.issued_at_epoch is not None
        release = kb._load_change_gate_release(conn, first.release_id)
        assert release is not None
        assert kb._mark_change_gate_release_consumed(
            conn,
            release,
            run_id=101,
            event_id=202,
            from_status="ready",
            to_status="running",
            now_epoch=first.issued_at_epoch + 1,
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-consumed-same",
            turn_id="same-consumed-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1
        state = kb.change_gate_release_state(conn, first.release_id)
        assert state is not None
        assert state["state"] == "CONSUMED"


def test_current_turn_same_authority_stale_transition_does_not_reissue(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-stale-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-stale-same",
            turn_id="same-stale-turn",
        )
        assert first.ok
        kb._append_event(
            conn,
            task_id,
            "synthetic-transition-drift",
            {"reason": "same authority must not reissue stale transition"},
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-stale-same",
            turn_id="same-stale-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_same_authority_malformed_release_does_not_reissue(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-malformed-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-malformed-same",
            turn_id="same-malformed-turn",
        )
        assert first.ok
        assert first.release_id is not None
        conn.execute(
            "UPDATE change_gate_releases SET artifact_schema = ? WHERE release_id = ?",
            ("malformed-release/v0", first.release_id),
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-malformed-same",
            turn_id="same-malformed-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_same_authority_live_corrupted_receipt_hash_fails_closed(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-live-corrupt-hash-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-corrupt-hash-same",
            turn_id="live-corrupt-hash-same-turn",
        )
        assert first.ok
        assert first.release_id is not None
        conn.execute(
            "UPDATE change_gate_releases "
            "SET authority_receipt_sha256 = ? WHERE release_id = ?",
            ("0" * 64, first.release_id),
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-corrupt-hash-same",
            turn_id="live-corrupt-hash-same-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1
        state = kb.change_gate_release_state(conn, first.release_id)
        assert state is not None
        assert state["state"] == "ISSUED"


def test_current_turn_fresh_authority_live_corrupted_receipt_hash_fails_closed(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-live-corrupt-hash-fresh",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-corrupt-hash-fresh",
            turn_id="live-corrupt-hash-old-turn",
        )
        assert first.ok
        assert first.release_id is not None
        conn.execute(
            "UPDATE change_gate_releases "
            "SET authority_receipt_sha256 = ? WHERE release_id = ?",
            ("f" * 64, first.release_id),
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-corrupt-hash-fresh",
            turn_id="live-corrupt-hash-fresh-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_same_authority_revoked_corrupted_receipt_hash_fails_closed(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-revoked-corrupt-hash-same",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-corrupt-hash-same",
            turn_id="revoked-corrupt-hash-same-turn",
        )
        assert first.ok
        assert first.release_id is not None
        assert kb.revoke_change_gate_releases(
            conn,
            task_id,
            reason=ChangeGateReason.RELEASE_REVOKED,
        ) == 1
        conn.execute(
            "UPDATE change_gate_releases "
            "SET authority_receipt_sha256 = ? WHERE release_id = ?",
            ("0" * 64, first.release_id),
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-corrupt-hash-same",
            turn_id="revoked-corrupt-hash-same-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1
        state = kb.change_gate_release_state(conn, first.release_id)
        assert state is not None
        assert state["state"] == "REVOKED"


def test_current_turn_fresh_authority_revoked_corrupted_receipt_hash_fails_closed(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-revoked-corrupt-hash-fresh",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-corrupt-hash-fresh",
            turn_id="revoked-corrupt-hash-old-turn",
        )
        assert first.ok
        assert first.release_id is not None
        assert kb.revoke_change_gate_releases(
            conn,
            task_id,
            reason=ChangeGateReason.RELEASE_REVOKED,
        ) == 1
        conn.execute(
            "UPDATE change_gate_releases "
            "SET authority_receipt_sha256 = ? WHERE release_id = ?",
            ("f" * 64, first.release_id),
        )

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-corrupt-hash-fresh",
            turn_id="revoked-corrupt-hash-fresh-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_fresh_authority_after_revoked_release_can_reauthorize(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-revoked-fresh",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-fresh",
            turn_id="revoked-old-turn",
        )
        assert first.ok
        assert first.release_id is not None
        assert kb.revoke_change_gate_releases(
            conn,
            task_id,
            reason=ChangeGateReason.RELEASE_REVOKED,
        ) == 1

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-revoked-fresh",
            turn_id="revoked-fresh-turn",
        )

        assert second.ok
        assert second.status == "issued"
        assert second.release_id is not None
        assert second.release_id != first.release_id
        assert _current_turn_release_count(conn, task_id) == 2


def test_current_turn_fresh_authority_cannot_replace_live_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-live-fresh",
        )
        first = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-fresh",
            turn_id="live-old-turn",
        )
        assert first.ok

        second = _issue_current_turn_claim(
            statement=statement,
            session_id="session-live-fresh",
            turn_id="live-fresh-turn",
        )

        assert not second.ok
        assert second.status == "owner_failure"
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_host_then_model_tool_same_authority_reuses_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    with _connect(db_path) as conn:
        task_id, _fixture, statement = _current_turn_claim_fixture(
            conn,
            tmp_path,
            monkeypatch,
            db_path=db_path,
            session_id="session-host-model",
        )

        tokens = set_session_vars(platform="manual", session_id="session-host-model")
        try:
            with _scoped_test_current_turn_user_authority(
                statement,
                session_id="session-host-model",
                turn_id="host-model-same-turn",
                platform_scope="manual",
            ):
                host = issue_current_turn_change_gate_release()
                model_tool = issue_change_gate_release(
                    task_id=task_id,
                    purpose=ReleasePurpose.CLAIM.value,
                )
        finally:
            clear_session_vars(tokens)

        assert host.ok
        assert host.release_id is not None
        assert model_tool.ok
        assert model_tool.reused
        assert model_tool.release_id == host.release_id
        assert _current_turn_release_count(conn, task_id) == 1


def test_current_turn_resolver_notify_fallback_never_overrides_task_session(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        conn.execute("UPDATE tasks SET session_id = '' WHERE id = ?", (task_id,))
        conn.execute(
            "INSERT INTO kanban_notify_subs("
            "task_id, platform, chat_id, thread_id, created_at, last_event_id"
            ") VALUES (?, ?, ?, ?, ?, 0)",
            (task_id, "discord", "wrong-chat", "", 20),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs("
            "task_id, platform, chat_id, thread_id, created_at, last_event_id"
            ") VALUES (?, ?, ?, ?, ?, 0)",
            (task_id, "discord", "chat-a", "thread-a", 10),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )

        tokens = set_session_vars(
            platform="discord",
            chat_id="chat-a",
            thread_id="thread-a",
            session_id="origin-session",
        )
        try:
            with _scoped_test_current_turn_user_authority(
                statement,
                session_id="origin-session",
                turn_id="notify-fallback-turn",
                platform_scope="discord",
            ):
                fallback = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert fallback.ok
        assert fallback.status == "issued"
        assert fallback.task_id == task_id

        task_id_mismatch = _create_ready_task(conn)
        mismatch_root = tmp_path / "mismatch"
        mismatch_root.mkdir()
        fixture_mismatch = _attach_runtime_artifacts(
            conn,
            mismatch_root,
            task_id_mismatch,
        )
        for inventory_file in (mismatch_root / "inventory").iterdir():
            (tmp_path / "inventory" / inventory_file.name).write_bytes(
                inventory_file.read_bytes()
            )
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            ("different-session", task_id_mismatch),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs("
            "task_id, platform, chat_id, thread_id, created_at, last_event_id"
            ") VALUES (?, ?, ?, ?, ?, 0)",
            (task_id_mismatch, "discord", "chat-a", "thread-a", 30),
        )
        mismatch_statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture_mismatch.handoff_sha256,
        )
        tokens = set_session_vars(
            platform="discord",
            chat_id="chat-a",
            thread_id="thread-a",
            session_id="origin-session",
        )
        try:
            with _scoped_test_current_turn_user_authority(
                mismatch_statement,
                session_id="origin-session",
                turn_id="notify-mismatch-turn",
                platform_scope="discord",
            ):
                mismatch = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert not mismatch.ok
        assert mismatch.status == "zero_candidate"
        assert conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases WHERE task_id = ?",
            (task_id_mismatch,),
        ).fetchone()[0] == 0


def test_current_turn_malformed_authority_fails_closed_without_schema(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            ("session-malformed", task_id),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )

        tokens = set_session_vars(platform="manual", session_id="session-malformed")
        try:
            with _scoped_test_current_turn_user_authority(
                statement + " trailing",
                session_id="session-malformed",
                turn_id="malformed-turn",
                platform_scope="manual",
            ):
                result = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert not result.ok
        assert result.status == "zero_candidate"
        assert result.terminal
        assert not kb.change_gate_runtime_schema_exists(conn)


def test_current_turn_default_off_authority_fails_open_to_provider_without_schema(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        statement = "AUTHORIZE_HERMES_CHANGE_GATE_CLAIM " + "0" * 64
        with _scoped_test_current_turn_user_authority(
            statement,
            session_id="session-default-off",
            turn_id="default-off-turn",
            platform_scope="manual",
        ):
            result = issue_current_turn_change_gate_release()

        assert not result.ok
        assert result.status == "ineligible"
        assert result.reason == ChangeGateReason.DISABLED.value
        assert not result.terminal
        assert not kb.change_gate_runtime_schema_exists(conn)


def test_current_turn_ambiguous_authority_fails_closed_without_release_schema(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        first_task = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, first_task)
        second_task = _create_ready_task(conn)
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id IN (?, ?)",
            ("session-ambiguous", first_task, second_task),
        )
        rows = conn.execute(
            "SELECT filename, stored_path, content_type FROM task_attachments "
            "WHERE task_id = ?",
            (first_task,),
        ).fetchall()
        for row in rows:
            kb.store_attachment_bytes(
                conn,
                second_task,
                filename=row["filename"],
                data=Path(row["stored_path"]).read_bytes(),
                content_type=row["content_type"],
            )
        source = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?",
            (first_task,),
        ).fetchone()["workspace_path"]
        conn.execute(
            "UPDATE tasks SET workspace_path = ?, assignee = ?, model_override = ?, "
            "provider_override = ?, reasoning_effort = ? WHERE id = ?",
            (
                source,
                "executor",
                "fixture-model",
                "fixture-provider",
                "medium",
                second_task,
            ),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )

        with _scoped_test_current_turn_user_authority(
            statement,
            session_id="session-ambiguous",
            turn_id="ambiguous-turn",
            platform_scope="manual",
        ):
            result = issue_current_turn_change_gate_release()

        assert not result.ok
        assert result.status == "ambiguous"
        assert result.candidate_count == 2
        assert result.terminal
        assert not kb.change_gate_runtime_schema_exists(conn)


def test_current_turn_host_adapter_g4_resolves_review_task(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        task_id = _create_ready_task(conn)
        fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
        conn.execute(
            "UPDATE tasks SET session_id = ? WHERE id = ?",
            ("session-g4", task_id),
        )
        _enable_runtime(monkeypatch, tmp_path / "inventory")
        claim_statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )
        tokens = set_session_vars(platform="manual", session_id="session-g4")
        try:
            with _scoped_test_current_turn_user_authority(
                claim_statement,
                session_id="session-g4",
                turn_id="host-g4-claim-turn",
                platform_scope="manual",
            ):
                claim_release = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)
        assert claim_release.ok

        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.current_run_id is not None
        assert kb.request_review(conn, task_id, expected_run_id=int(claimed.current_run_id))
        reviewer = kb.claim_review_task(conn, task_id)
        assert reviewer is not None
        assert reviewer.current_run_id is not None
        assert kb.request_review(
            conn,
            task_id,
            expected_run_id=int(reviewer.current_run_id),
            change_gate_review={
                "reviewer_class": ReviewerClass.REVIEWER.value,
                "verdict": ReviewVerdict.PASS.value,
                "finding_codes": [],
            },
        )
        ready_for_g4 = kb.get_task(conn, task_id)
        assert ready_for_g4 is not None
        assert ready_for_g4.status == "review"
        assert ready_for_g4.current_run_id is None

        g4_statement = expected_release_statement(
            purpose=ReleasePurpose.G4,
            handoff_sha256=fixture.handoff_sha256,
        )
        tokens = set_session_vars(platform="manual", session_id="session-g4")
        try:
            with _scoped_test_current_turn_user_authority(
                g4_statement,
                session_id="session-g4",
                turn_id="host-g4-turn",
                platform_scope="manual",
            ):
                g4_release = issue_current_turn_change_gate_release()
        finally:
            clear_session_vars(tokens)

        assert g4_release.ok
        assert g4_release.status == "issued"
        assert g4_release.task_id == task_id
        assert g4_release.purpose == ReleasePurpose.G4.value
        assert conn.execute(
            "SELECT COUNT(*) FROM change_gate_releases "
            "WHERE task_id = ? AND purpose = 'G4'",
            (task_id,),
        ).fetchone()[0] == 1
