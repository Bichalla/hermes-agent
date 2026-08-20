"""Integration coverage for Change Gate transition-anchor generation."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.change_gate import (
    ChangeGateReason,
    ReleasePurpose,
    TransitionAnchor,
    canonical_sha256,
    expected_release_statement,
    issue_durable_release_artifact,
)
from hermes_cli.change_gate_release import issue_change_gate_release
from hermes_cli.change_gate_runtime import (
    load_task_gate_artifacts,
    project_upstream_reviews,
)
from tests.hermes_cli.test_change_gate_runtime_integration import (
    RuntimeFixture,
    _attach_runtime_artifacts,
    _claim_and_converge_normal_review,
    _connect,
    _create_ready_task,
    _enable_runtime,
    _handoff,
)
from tools.workflow_authority import _scoped_test_current_turn_user_authority


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _setup_enabled_task(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RuntimeFixture:
    tmp_path.mkdir(parents=True, exist_ok=True)
    task_id = _create_ready_task(conn)
    fixture = _attach_runtime_artifacts(conn, tmp_path, task_id)
    _enable_runtime(monkeypatch, tmp_path / "inventory")
    return fixture


def _issue_foreground_release(
    task_id: str,
    fixture: RuntimeFixture,
    purpose: ReleasePurpose,
    *,
    turn_id: str,
) -> str:
    statement = expected_release_statement(
        purpose=purpose,
        handoff_sha256=fixture.handoff_sha256,
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="change-gate-transition-anchor",
        turn_id=turn_id,
        platform_scope="manual",
    ):
        issued = issue_change_gate_release(
            task_id=task_id,
            purpose=purpose.value,
        )
    assert issued.ok, issued.reason
    assert issued.release_id is not None
    return issued.release_id


def _current_anchor(
    conn: sqlite3.Connection,
    fixture: RuntimeFixture,
    purpose: ReleasePurpose,
) -> TransitionAnchor:
    policy = __import__(
        "hermes_cli.change_gate_runtime",
        fromlist=["load_runtime_policy"],
    ).load_runtime_policy()
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
    anchor = kb.derive_change_gate_transition_anchor(
        conn,
        fixture.task_id,
        purpose=purpose,
        artifacts=load.artifacts,
        reviews=reviews,
    )
    assert anchor is not None
    return anchor


def test_owner_derived_claim_release_persists_exact_current_anchor(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        expected_anchor = _current_anchor(conn, fixture, ReleasePurpose.CLAIM)

        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.CLAIM,
            turn_id="claim-anchor-success",
        )

        state = kb.change_gate_release_state(conn, release_id)
        assert state is not None
        assert state["state"] == "ISSUED"
        assert state["transition_anchor_sha256"] == expected_anchor.digest()


@pytest.mark.parametrize(
    ("drift_name", "mutate"),
    (
        ("assignee", lambda conn, task_id: kb.assign_task(conn, task_id, "other-executor")),
        (
            "route",
            lambda conn, task_id: kb.set_model_override(
                conn,
                task_id,
                "other-model",
                "other-provider",
            ),
        ),
        (
            "event",
            lambda conn, task_id: kb.promote_task(
                conn,
                task_id,
                actor="operator",
                force=True,
                dry_run=True,
            ),
        ),
    ),
)
def test_claim_release_is_stale_after_assignee_route_or_event_drift(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_name: str,
    mutate,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / f"{drift_name}.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path / drift_name, monkeypatch)
        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.CLAIM,
            turn_id=f"{drift_name}-before-drift",
        )
        if drift_name == "event":
            assert kb.block_task(
                conn,
                fixture.task_id,
                reason="visible status drift",
            )
            assert kb.promote_task(
                conn,
                fixture.task_id,
                actor="operator",
                reason="restore ready after drift",
                force=True,
            ) == (True, None)
        else:
            assert mutate(conn, fixture.task_id)
        before_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (fixture.task_id,),
        ).fetchone()[0]

        evaluation = kb.evaluate_change_gate_claim_runtime(conn, fixture.task_id)
        claimed = kb.claim_task(conn, fixture.task_id)
        state = kb.change_gate_release_state(conn, release_id)

        assert evaluation.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert claimed is None
        assert state is not None
        assert state["state"] == "ISSUED"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (fixture.task_id,),
        ).fetchone()[0] == before_runs


def test_visible_status_restored_by_real_event_keeps_claim_release_stale(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "restored-status.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.CLAIM,
            turn_id="claim-before-status-restore",
        )

        assert kb.block_task(conn, fixture.task_id, reason="temporary block")
        assert kb.promote_task(
            conn,
            fixture.task_id,
            actor="operator",
            reason="restore visible ready status",
            force=True,
        ) == (True, None)
        task = kb.get_task(conn, fixture.task_id)
        evaluation = kb.evaluate_change_gate_claim_runtime(conn, fixture.task_id)
        claimed = kb.claim_task(conn, fixture.task_id)
        state = kb.change_gate_release_state(conn, release_id)

        assert task is not None
        assert task.status == "ready"
        assert evaluation.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert claimed is None
        assert state is not None
        assert state["state"] == "ISSUED"


def test_g4_release_is_denied_after_review_reopen(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "g4-reopen.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        _claim_and_converge_normal_review(conn, fixture, claim_suffix="1")
        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.G4,
            turn_id="g4-before-reopen",
        )

        assert kb.reopen_review_task(conn, fixture.task_id)
        evaluation = kb.evaluate_change_gate_g4_runtime(conn, fixture.task_id)
        completed = kb.complete_task(conn, fixture.task_id, result="must not complete")
        state = kb.change_gate_release_state(conn, release_id)

        assert evaluation.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert completed is False
        assert state is not None
        assert state["state"] == "ISSUED"


def test_g4_release_is_stale_after_route_drift(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "g4-route-drift.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        _claim_and_converge_normal_review(conn, fixture, claim_suffix="2")
        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.G4,
            turn_id="g4-before-route-drift",
        )

        assert kb.set_model_override(
            conn,
            fixture.task_id,
            "other-model",
            "other-provider",
        )
        evaluation = kb.evaluate_change_gate_g4_runtime(conn, fixture.task_id)
        completed = kb.complete_task(conn, fixture.task_id, result="must not complete")
        state = kb.change_gate_release_state(conn, release_id)

        assert evaluation.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert completed is False
        assert state is not None and state["state"] == "ISSUED"


def test_g4_release_is_stale_during_reopened_run_and_after_review_status_restore(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "g4-current-run-and-restored-status.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        _claim_and_converge_normal_review(conn, fixture, claim_suffix="3")
        g4_release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.G4,
            turn_id="g4-before-current-run-drift",
        )

        assert kb.reopen_review_task(conn, fixture.task_id)
        _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.CLAIM,
            turn_id="claim-after-g4-reopen",
        )
        executor = kb.claim_task(conn, fixture.task_id)
        assert executor is not None and executor.current_run_id is not None

        while_running = kb.evaluate_change_gate_g4_runtime(conn, fixture.task_id)
        assert while_running.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert kb.request_review(
            conn,
            fixture.task_id,
            expected_run_id=int(executor.current_run_id),
        )
        restored = kb.get_task(conn, fixture.task_id)
        assert restored is not None and restored.status == "review"

        after_restore = kb.evaluate_change_gate_g4_runtime(conn, fixture.task_id)
        completed = kb.complete_task(conn, fixture.task_id, result="must not complete")
        g4_state = kb.change_gate_release_state(conn, g4_release_id)

        assert after_restore.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        assert completed is False
        assert g4_state is not None and g4_state["state"] == "ISSUED"


def test_fake_caller_supplied_anchor_cannot_persist_foreground_release(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "fake-anchor.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        assert kb.block_task(conn, fixture.task_id, reason="seed event triple")
        assert kb.promote_task(
            conn,
            fixture.task_id,
            actor="operator",
            reason="ready with current event anchor",
            force=True,
        ) == (True, None)
        current_anchor = _current_anchor(conn, fixture, ReleasePurpose.CLAIM)
        assert current_anchor.latest_event_id is not None
        fake_anchor = replace(
            current_anchor,
            latest_event_payload_sha256=canonical_sha256(
                {"payload": {"caller": "legacy-fake-anchor"}}
            ),
        )
        handoff = _handoff(fixture)
        statement = expected_release_statement(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=fixture.handoff_sha256,
        )

        with _scoped_test_current_turn_user_authority(
            statement,
            session_id="change-gate-transition-anchor",
            turn_id="fake-anchor",
            platform_scope="manual",
        ):
            release = issue_durable_release_artifact(
                purpose=ReleasePurpose.CLAIM,
                handoff=handoff,
                evidence=fixture.evidence,
                transition_anchor=fake_anchor,
                ttl_seconds=300,
                clock=lambda: 1_700_000_000,
            )
            assert release is not None
            with pytest.raises(
                PermissionError,
                match="change_gate_release_binding_required",
            ):
                kb.persist_foreground_change_gate_release(
                    conn,
                    release,
                    now_epoch=1_700_000_000,
                )

        if kb.change_gate_runtime_schema_exists(conn):
            assert conn.execute("SELECT COUNT(*) FROM change_gate_releases").fetchone()[0] == 0


def test_unchanged_exact_claim_release_consumes_once(
    tmp_path: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.profiles as profiles

    db_path = tmp_path / "consume-once.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with _connect(db_path) as conn:
        fixture = _setup_enabled_task(conn, tmp_path, monkeypatch)
        release_id = _issue_foreground_release(
            fixture.task_id,
            fixture,
            ReleasePurpose.CLAIM,
            turn_id="claim-consumes-once",
        )

        claimed = kb.claim_task(conn, fixture.task_id)
        replay = kb.claim_task(conn, fixture.task_id)
        state = kb.change_gate_release_state(conn, release_id)

        assert claimed is not None
        assert replay is None
        assert state is not None
        assert state["state"] == "CONSUMED"
        assert state["consumed_from_status"] == "ready"
        assert state["consumed_to_status"] == "running"
