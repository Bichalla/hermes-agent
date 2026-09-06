from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.change_gate import ReleasePurpose
from hermes_cli.change_gate_runtime import (
    load_runtime_policy,
    load_task_gate_artifacts,
)
from tests.tools.test_kanban_g2_handoff_tool import (
    _active_planner,
    _args,
    _git_repo,
    _setup_env,
    _store_task_release,
)
from tools.kanban_tools import _handle_g2_handoff


@pytest.fixture
def g2_recovery_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod, conn = _setup_env(monkeypatch, tmp_path)
    try:
        yield mod, conn, tmp_path, monkeypatch
    finally:
        conn.close()


def _event(conn, task_id: str, *, kind: str, run_id: int | None = None):
    params: tuple[object, ...]
    if run_id is None:
        params = (task_id, kind)
        run_filter = ""
    else:
        params = (task_id, kind, run_id)
        run_filter = " AND run_id = ?"
    return conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id = ? AND kind = ?"
        + run_filter
        + " ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()


def _event_count(conn, task_id: str, kind: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        ).fetchone()["n"]
    )


def _row_dict(conn, sql: str, params: tuple[object, ...]) -> dict[str, object]:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return dict(row)


def _release_binding(conn, task_id: str) -> dict[str, object]:
    return _row_dict(
        conn,
        "SELECT release_id, artifact_sha256, handoff_sha256, evidence_sha256, "
        "inventory_sha256, artifact_set_sha256, route_sha256, "
        "authority_receipt_sha256 FROM change_gate_releases "
        "WHERE task_id = ? AND purpose = 'CLAIM'",
        (task_id,),
    )


def _prepare_stopped_review(
    conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    review_claimer: str = "review-host:claim",
    worker_pid: int = 43210,
) -> tuple[dict[str, object], dict[str, str]]:
    repo, artifact_binding = _git_repo(tmp_path)
    parent = _active_planner(conn, kb, monkeypatch, repo)
    g2_result = json.loads(_handle_g2_handoff(_args(artifact_binding, repo)))
    assert g2_result["ok"] is True
    child = str(g2_result["task_id"])

    foreign = kb.create_task(conn, title="foreign", assignee="other")
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'sentinel', 'preserve me', 123)",
            (foreign,),
        )

    _store_task_release(conn, kb, child, ReleasePurpose.CLAIM, "r")
    claimed = kb.claim_task(conn, child, claimer="executor-host:claim")
    assert claimed is not None
    executor_run_id = int(claimed.current_run_id)
    executor_claim = _event(conn, child, kind="claimed", run_id=executor_run_id)
    assert executor_claim is not None

    reviewed, reason = kb.request_review(
        conn,
        child,
        summary="ready for review",
        expected_run_id=executor_run_id,
        with_reason=True,
    )
    assert reviewed, reason
    release_event = _event(conn, child, kind="review_requested", run_id=executor_run_id)
    assert release_event is not None

    review = kb.claim_review_task(conn, child, claimer=review_claimer)
    assert review is not None
    review_run_id = int(review.current_run_id)
    review_claim = _event(conn, child, kind="claimed", run_id=review_run_id)
    assert review_claim is not None
    review_claim_payload = json.loads(review_claim["payload"])

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps({"existing": "keep"}), review_run_id),
        )
    kb._set_worker_pid(conn, child, worker_pid)

    artifacts = load_task_gate_artifacts(
        conn,
        child,
        policy=load_runtime_policy(),
        attachment_root=kb.task_attachments_dir(child),
    )
    assert artifacts.ok and artifacts.artifacts is not None
    parent_g2_event = _event(conn, parent, kind="g2_handoff_completed")
    assert parent_g2_event is not None and parent_g2_event["payload"]
    parent_g2_payload = json.loads(parent_g2_event["payload"])
    parent_claim = _release_binding(conn, parent)
    child_claim = _release_binding(conn, child)
    db_identity = kb.stopped_review_database_identity(conn)

    binding: dict[str, object] = {
        "task_id": child,
        "run_id": review_run_id,
        "claim_lock": review_claimer,
        "worker_pid": worker_pid,
        "task_assignee": str(review.assignee),
        "reviewer_profile": str(review.assignee),
        "executor_assignee": str(claimed.assignee),
        "parent_task_id": parent,
        "g2_task_id": parent,
        "child_task_id": child,
        "parent_g2_run_id": parent_g2_event["run_id"],
        "parent_g2_event_id": parent_g2_event["id"],
        "parent_claim_release_id": parent_claim["release_id"],
        "parent_claim_consumed_event_id": parent_g2_payload[
            "claim_consumed_event_id"
        ],
        "executor_run_id": executor_run_id,
        "executor_claim_lock": "executor-host:claim",
        "executor_claim_event_id": int(executor_claim["id"]),
        "executor_claim_release_id": child_claim["release_id"],
        "executor_release_event_id": int(release_event["id"]),
        "executor_release_handoff_sha256": child_claim["handoff_sha256"],
        "executor_release_artifact_sha256": child_claim["artifact_sha256"],
        "executor_release_evidence_sha256": child_claim["evidence_sha256"],
        "executor_release_inventory_sha256": child_claim["inventory_sha256"],
        "executor_release_artifact_set_sha256": child_claim["artifact_set_sha256"],
        "executor_release_route_sha256": child_claim["route_sha256"],
        "executor_release_authority_receipt_sha256": child_claim[
            "authority_receipt_sha256"
        ],
        "review_reviewer_class": review_claim_payload["reviewer_class"],
        "review_bundle_sha256": review_claim_payload["bundle_sha256"],
        "review_route_sha256": review_claim_payload["route_sha256"],
        "frozen_handoff_sha256": artifacts.artifacts.handoff.digest(),
        "operation_id": f"op-{child}",
        "operation_failure_sha256": parent_claim["authority_receipt_sha256"],
        "operation_terminalizer_sha256": child_claim["authority_receipt_sha256"],
        "db_semantic_preimage_sha256": db_identity["sha256"],
        "db_semantic_schema": db_identity["schema"],
        "gateway_pid": 54321,
        "gateway_absent": True,
        "worker_absent": True,
    }
    return binding, {"child": child, "foreign": foreign}


def test_stopped_review_identity_preserves_sqlite_types_and_schema(g2_recovery_env):
    _mod, conn, _tmp_path, _monkeypatch = g2_recovery_env
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE typed_sentinel(value)")
        conn.execute("INSERT INTO typed_sentinel VALUES (?)", (b"sentinel",))
    blob_identity = kb.stopped_review_database_identity(conn)
    assert kb.stopped_review_database_identity(conn) == blob_identity
    with kb.write_txn(conn):
        conn.execute("UPDATE typed_sentinel SET value=?", ("sentinel",))
    text_identity = kb.stopped_review_database_identity(conn)
    assert text_identity != blob_identity
    with kb.write_txn(conn):
        conn.execute("CREATE INDEX sentinel_index ON typed_sentinel(value)")
    assert kb.stopped_review_database_identity(conn) != text_identity


@pytest.mark.parametrize("drift", ["schema", "foreign_blob", "duplicate_audit"])
def test_exact_recovery_retry_rejects_schema_blob_or_extra_audit(g2_recovery_env, drift):
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    with kb.write_txn(conn):
        conn.execute("CREATE TABLE typed_sentinel(value)")
        conn.execute("INSERT INTO typed_sentinel VALUES (?)", (b"preserve",))
    binding["db_semantic_preimage_sha256"] = kb.stopped_review_database_identity(conn)["sha256"]
    assert kb.terminalize_stopped_review_run(conn, binding=binding)["ok"] is True
    with kb.write_txn(conn):
        if drift == "schema":
            conn.execute("CREATE INDEX sentinel_index ON typed_sentinel(value)")
        elif drift == "foreign_blob":
            conn.execute("UPDATE typed_sentinel SET value=?", (b"changed",))
        else:
            conn.execute(
                "INSERT INTO task_events(task_id,kind,payload,created_at,run_id) "
                "SELECT task_id,kind,payload,created_at,run_id FROM task_events "
                "WHERE task_id=? AND kind='stopped_review_terminalized'", (ids["child"],),
            )
    before_retry = kb.stopped_review_database_identity(conn)
    result = kb.terminalize_stopped_review_run(conn, binding=binding)
    assert result["ok"] is False
    assert result["reason"] == "existing_recovery_foreign_state_mismatch"
    assert kb.stopped_review_database_identity(conn) == before_retry


def test_review_dispatch_forces_reserved_builtin_review_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda sample=None: "unknown")
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
    )
    conn = kb.connect()
    try:
        child = kb.create_task(
            conn,
            title="domain review",
            assignee="reviewer",
            skills=["domain-specific-review"],
        )
        claimed = kb.claim_task(conn, child, claimer="executor-host:claim")
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.request_review(
            conn,
            child,
            expected_run_id=int(claimed.current_run_id),
        )
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
        assert _row_dict(
            conn,
            "SELECT status, assignee, claim_lock FROM tasks WHERE id = ?",
            (child,),
        ) == {"status": "review", "assignee": "reviewer", "claim_lock": None}
        monkeypatch.setattr(kb, "release_stale_claims", lambda _conn: 0)
        monkeypatch.setattr(kb, "reconcile_orphaned_running", lambda _conn: [])
        monkeypatch.setattr(kb, "detect_stale_running", lambda _conn, **_kw: [])
        monkeypatch.setattr(kb, "detect_crashed_workers", lambda _conn: [])
        monkeypatch.setattr(kb, "enforce_max_runtime", lambda _conn: [])
        monkeypatch.setattr(kb, "recompute_ready", lambda _conn, **_kw: 0)

        spawned: list[tuple[str, list[str]]] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args, **_kw: spawned.append(
                (task.id, list(task.skills or []))
            )
            or None,
        )

        assert result.spawned == [
            (child, "reviewer", kb.get_task(conn, child).workspace_path)
        ], result
        assert spawned == [
            (
                child,
                ["domain-specific-review", kb.FORCED_REVIEW_SKILL_IDENTIFIER],
            )
        ]
    finally:
        conn.close()


def test_terminalize_stopped_review_run_blocks_without_replay_and_preserves_rows(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    hooks: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda *args, **_kw: hooks.append(args),
    )
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    hooks.clear()
    before_foreign = _row_dict(
        conn,
        "SELECT * FROM tasks WHERE id = ?",
        (ids["foreign"],),
    )
    before_comment = _row_dict(
        conn,
        "SELECT * FROM task_comments WHERE task_id = ?",
        (ids["foreign"],),
    )

    result = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert result["status"] == "blocked"
    assert result["run_status"] == "stopped_review_terminalized"
    row = _row_dict(
        conn,
        "SELECT status, current_run_id, claim_lock, worker_pid, block_kind, "
        "last_failure_error FROM tasks WHERE id = ?",
        (ids["child"],),
    )
    assert row == {
        "status": "blocked",
        "current_run_id": None,
        "claim_lock": None,
        "worker_pid": None,
        "block_kind": "capability",
        "last_failure_error": "stopped_review_worker_recovered",
    }
    run = _row_dict(
        conn,
        "SELECT status, outcome, ended_at, worker_pid, metadata FROM task_runs "
        "WHERE id = ?",
        (binding["run_id"],),
    )
    assert run["status"] == "blocked"
    assert run["outcome"] == "blocked"
    assert run["ended_at"] is not None
    assert run["worker_pid"] is None
    metadata = json.loads(run["metadata"])
    assert metadata["existing"] == "keep"
    assert metadata["stopped_review_recovery_binding_sha256"] == result["binding_sha256"]
    assert metadata["run_status"] == "stopped_review_terminalized"
    assert _event_count(conn, ids["child"], "stopped_review_terminalized") == 1
    event = _event(conn, ids["child"], kind="stopped_review_terminalized")
    event_payload = json.loads(event["payload"])
    assert "post_db_digest" not in event_payload
    assert result["pre_db_digest"]["sha256"] == binding["db_semantic_preimage_sha256"]
    assert result["post_db_digest"]["counts"]["task_events"] == (
        result["pre_db_digest"]["counts"]["task_events"] + 1
    )
    assert _row_dict(conn, "SELECT * FROM tasks WHERE id = ?", (ids["foreign"],)) == before_foreign
    assert (
        _row_dict(conn, "SELECT * FROM task_comments WHERE task_id = ?", (ids["foreign"],))
        == before_comment
    )
    assert hooks == []

    spawned: list[str] = []
    dispatch = kb.dispatch_once(
        conn,
        spawn_fn=lambda task, *_args, **_kw: spawned.append(task.id) or 999,
    )
    assert spawned == []
    assert dispatch.spawned == []


def test_terminalize_stopped_review_run_is_exactly_idempotent(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    first = kb.terminalize_stopped_review_run(conn, binding=binding)
    second = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["binding_sha256"] == first["binding_sha256"]
    assert second["post_db_digest"] == kb.stopped_review_database_identity(conn)
    assert _event_count(conn, ids["child"], "stopped_review_terminalized") == 1


def test_terminalize_stopped_review_run_rejects_lost_response_foreign_drift(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    first = kb.terminalize_stopped_review_run(conn, binding=binding)
    assert first["ok"] is True
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_comments SET body = 'drift' WHERE task_id = ?",
            (ids["foreign"],),
        )

    second = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert second == {
        "ok": False,
        "reason": "existing_recovery_foreign_state_mismatch",
        "task_id": ids["child"],
        "run_id": binding["run_id"],
    }
    assert _event_count(conn, ids["child"], "stopped_review_terminalized") == 1


def test_terminalize_stopped_review_run_rejects_lost_response_target_drift(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    first = kb.terminalize_stopped_review_run(conn, binding=binding)
    assert first["ok"] is True
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps({"existing": "drift"}), binding["run_id"]),
        )

    second = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert second == {
        "ok": False,
        "reason": "existing_recovery_target_state_mismatch",
        "task_id": ids["child"],
        "run_id": binding["run_id"],
    }
    assert _event_count(conn, ids["child"], "stopped_review_terminalized") == 1


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b.__setitem__("claim_lock", "wrong"), "active_review_task_mismatch"),
        (lambda b: b.__setitem__("worker_absent", False), "process_absence_not_proven"),
        (
            lambda b: b.__setitem__("executor_release_handoff_sha256", "0" * 64),
            "executor_claim_release_mismatch",
        ),
        (
            lambda b: b.__setitem__("executor_assignee", "wrong"),
            "executor_release_event_mismatch",
        ),
        (
            lambda b: b.__setitem__("review_bundle_sha256", "0" * 64),
            "change_gate_artifact_binding_mismatch",
        ),
        (
            lambda b: b.__setitem__("db_semantic_preimage_sha256", "0" * 64),
            "db_semantic_preimage_mismatch",
        ),
        (lambda b: b.__setitem__("g2_task_id", "wrong"), "task_graph_mismatch"),
    ],
)
def test_terminalize_stopped_review_run_rejects_wrong_bindings_without_effect(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    reason: str,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    before = _row_dict(
        conn,
        "SELECT status, current_run_id, claim_lock, worker_pid FROM tasks "
        "WHERE id = ?",
        (ids["child"],),
    )
    mutate(binding)

    result = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert result == {
        "ok": False,
        "reason": reason,
        "task_id": ids["child"],
        "run_id": binding["run_id"],
    }
    after = _row_dict(
        conn,
        "SELECT status, current_run_id, claim_lock, worker_pid FROM tasks "
        "WHERE id = ?",
        (ids["child"],),
    )
    assert after == before
    assert _event_count(conn, ids["child"], "stopped_review_terminalized") == 0


def test_terminalize_stopped_review_run_rejects_live_process(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == binding["worker_pid"])

    result = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert result["ok"] is False
    assert result["reason"] == "worker_pid_alive"
    assert kb.get_task(conn, ids["child"]).status == "running"


def test_terminalize_stopped_review_run_rejects_forged_g4_without_effect(
    g2_recovery_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mod, conn, tmp_path, monkeypatch = g2_recovery_env
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    binding, ids = _prepare_stopped_review(conn, tmp_path, monkeypatch)
    with kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO change_gate_releases (
                release_id, artifact_sha256, artifact_schema,
                transition_anchor_sha256, task_id, work_id, purpose,
                handoff_sha256, evidence_sha256, inventory_sha256,
                artifact_set_sha256, route_sha256, authority_receipt_sha256,
                issued_at, expires_at, max_consumptions, artifact_json, state
            ) VALUES ('g4-1', ?, 'test.schema/v1', ?, ?, 'work-g4', 'G4',
                      ?, ?, ?, ?, ?, ?, 100, 1000, 1, '{}', 'ISSUED')
            """,
            (
                "9" * 64,
                "8" * 64,
                ids["child"],
                "7" * 64,
                "6" * 64,
                "5" * 64,
                "4" * 64,
                "3" * 64,
                "2" * 64,
            ),
        )
        db_identity = kb.stopped_review_database_identity(conn)
        binding["db_semantic_preimage_sha256"] = db_identity["sha256"]
        binding["db_semantic_schema"] = db_identity["schema"]

    result = kb.terminalize_stopped_review_run(conn, binding=binding)

    assert result["ok"] is False
    assert result["reason"] == "g4_release_present"
    assert kb.get_task(conn, ids["child"]).status == "running"
