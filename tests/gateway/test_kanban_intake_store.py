import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from gateway.kanban_intake import (
    ApprovalResult,
    CURRENT_POLICY_VERSION,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    handle_reply,
)


def cfg(tmp_path):
    return KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")


def binding(user="u1", thread="t1", session="s1"):
    return SourceBinding(platform="discord", chat_id="c1", thread_id=thread, user_id=user, session_key=session)


def proposal(user="u1", title="Implement local guardrail scope"):
    return KanbanCardProposal(
        board="lifelog-control",
        title=title,
        body={"source_ref": "kp_safe", "acceptance_criteria": ["check"]},
        source_ref="kp_safe",
        user_id=user,
    )


def test_put_and_lookup_same_source(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(), binding(), c, now=100)
    found = store.get_active_for_source(binding(), now=101)
    assert found.state == "one"
    assert found.pending.pending_id == pending.pending_id


def test_expired_cross_user_and_cross_thread_fail_closed(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(), binding(), c, now=100)
    assert store.get_active_for_source(binding(), now=100 + c.proposal_ttl_seconds + 1).state == "none"
    assert store.get_active_for_source(binding(user="u2"), now=101).state == "none"
    assert store.get_active_for_source(binding(thread="other"), now=101).state == "none"


def test_multiple_active_is_ambiguous(tmp_path):
    c = KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db", max_pending_per_session=2)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(title="Implement local guardrail scope A"), binding(), c, now=100)
    store.put_pending(proposal(title="Implement local guardrail scope B"), binding(), c, now=101)
    found = store.get_active_for_source(binding(), now=102)
    assert found.state == "ambiguous"
    assert found.count == 2


def test_default_one_pending_limit_rejects_second(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(proposal(title="Implement local guardrail scope A"), binding(), c, now=100)
    try:
        store.put_pending(proposal(title="Implement local guardrail scope B"), binding(), c, now=101)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("second active pending proposal should be rejected by default")


def test_concurrent_put_pending_serializes_capacity_check_and_insert(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    start = threading.Barrier(2)

    def attempt(suffix: str) -> str:
        start.wait(timeout=2)
        try:
            store.put_pending(
                proposal(title=f"Implement local guardrail scope {suffix}"),
                binding(),
                c,
                now=100,
            )
        except ValueError as exc:
            assert "limit" in str(exc)
            return "limited"
        return "inserted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("A", "B")))

    assert sorted(results) == ["inserted", "limited"]
    assert store.get_active_for_source(binding(), now=101).count == 1


def test_missing_user_id_rejected(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    bad = SourceBinding(platform="discord", chat_id="c1", thread_id="t1", user_id="", session_key="s1")
    try:
        store.put_pending(proposal(user=""), bad, c)
    except ValueError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("missing user_id should fail closed")


def test_purge_removes_old_non_pending(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(), binding(), c, now=100)
    store.mark_status(pending.pending_id, "denied", now=101)
    assert store.purge(now=100 + c.pending_retention_seconds + 1) == 1


def test_pending_rows_store_current_policy_version(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement local guardrail scope"), binding(), c, now=100)
    found = store.get_active_for_source(binding(), now=101)
    assert found.state == "one"
    assert found.pending is not None
    assert found.pending.pending_id == pending.pending_id
    assert found.pending.policy_version == CURRENT_POLICY_VERSION


def test_old_policy_pending_requires_revalidation_after_policy_bump(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement local guardrail scope"), binding(), c)
    with store.connect() as conn:
        conn.execute("UPDATE kanban_intake_pending SET policy_version = ? WHERE pending_id = ?", ("kanban-intake-policy/v2", pending.pending_id))
        conn.commit()
    result = handle_reply("승인", binding(), c, store)
    assert result.handled is True
    assert result.action == "approve"
    assert result.verified is False
    assert "정책 버전" in result.message or "revalidate" in result.message.lower()
    assert store.get_active_for_source(binding(), now=101).state == "none"
    review = store.review_pending(include_all=True, now=101)
    items = {item["pending_id"]: item for item in review["items"]}
    assert items[pending.pending_id]["status"] == "needs_revalidation"


def test_pending_review_and_revalidate_flag_stale_or_generic_without_raw_ids(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement local guardrail scope"), binding(), c, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version = '', title = ? WHERE pending_id = ?",
            ("Plan Kanban follow-up work", pending.pending_id),
        )
        conn.commit()
    review = store.revalidate_pending(now=101)
    assert review["current_policy_version"] == CURRENT_POLICY_VERSION
    assert review["items"][0]["pending_id"] == pending.pending_id
    assert set(review["items"][0]["flags"]) >= {"stale_policy", "generic_title"}
    assert review["items"][0]["would_invalidate"] is True
    rendered = str(review)
    assert "'chat_id'" not in rendered
    assert "'thread_id'" not in rendered
    assert "'user_id'" not in rendered
    assert "'session_key'" not in rendered
    bulk = store.bulk_invalidate(dry_run=True, now=101)
    assert bulk["dry_run"] is True
    assert bulk["matched"] == 1
    assert bulk["updated"] == 0
    assert store.get_active_for_source(binding(), now=101).state == "one"


def test_pending_review_redacts_diagnostic_titles(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement local guardrail scope"), binding(), c, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET title = ? WHERE pending_id = ?",
            ("Review raw 1522060000000000010 해수 fever follow-up", pending.pending_id),
        )
        conn.commit()
    review = store.revalidate_pending(now=101)
    item = review["items"][0]
    assert item["pending_id"] == pending.pending_id
    assert "1522060000000000010" not in item["title"]
    assert "해수" not in item["title"]
    assert "fever" not in item["title"].lower()
    assert "[id]" in item["title"]
    assert "[child-sensitive]" in item["title"]
    assert "[health-sensitive]" in item["title"]
    assert "sensitive_leak" in item["flags"]


def test_pending_review_exposes_expired_hygiene_without_mutating_rows(tmp_path):
    c = KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
        max_pending_per_session=2,
    )
    store = PendingKanbanStore(c.store_path)
    review_now = 100 + c.proposal_ttl_seconds + 1
    expired = store.put_pending(proposal(title="Implement local guardrail scope B"), binding(thread="expired"), c, now=100)
    active = store.put_pending(proposal(title="Implement local guardrail scope A"), binding(thread="active"), c, now=review_now - 10)

    review = store.review_pending(now=review_now, include_all=False)
    items = {item["pending_id"]: item for item in review["items"]}

    assert review["counts"]["pending"] == 2
    assert review["counts"]["pending_active"] == 1
    assert review["counts"]["pending_expired"] == 1
    assert items[active.pending_id]["effective_status"] == "pending"
    assert items[expired.pending_id]["effective_status"] == "expired"
    assert "expired" in items[expired.pending_id]["flags"]
    assert items[active.pending_id]["expires_in_seconds"] > 0
    assert items[expired.pending_id]["expires_in_seconds"] < 0
    assert store.get_active_for_source(binding(thread="expired"), now=review_now).state == "none"


def test_pending_review_absent_db_is_no_write(tmp_path):
    c = cfg(tmp_path)
    assert c.store_path is not None
    path = c.store_path
    store = PendingKanbanStore(path)
    assert not path.exists()
    assert store.review_pending() == {"current_policy_version": CURRENT_POLICY_VERSION, "counts": {}, "items": []}
    assert not path.exists()
    result = store.bulk_invalidate(dry_run=True)
    assert result == {"dry_run": True, "where": "stale-or-generic", "matched": 0, "updated": 0, "pending_ids": []}
    assert not path.exists()


def test_bulk_invalidate_execute_updates_only_pending_rows(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement local guardrail scope"), binding(), c, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET title = ? WHERE pending_id = ?",
            ("Plan Kanban follow-up work", pending.pending_id),
        )
        conn.commit()
    store.mark_status(pending.pending_id, "denied", now=101)
    result = store.bulk_invalidate(dry_run=False, now=102)
    assert result["matched"] == 0
    assert result["updated"] == 0


def _migrate_audit(c):
    from hermes_cli import kanban_intake_migrate

    assert c.store_path is not None
    assert kanban_intake_migrate.main(
        ["--db", str(c.store_path), "--dry-run", "false", "--json"]
    ) == 0


def _transition_rows(store, pending_id):
    with sqlite3.connect(store.path) as conn:
        return conn.execute(
            "SELECT from_status, to_status, reason_code, invocation_key "
            "FROM kanban_intake_transition_audit WHERE pending_id = ? ORDER BY id",
            (pending_id,),
        ).fetchall()


def test_denial_and_policy_revalidation_transitions_audit_atomically(
    tmp_path, capsys, monkeypatch
):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    denied = store.put_pending(proposal(title="Implement denial audit tests"), binding(thread="deny"), c, now=100)
    stale = store.put_pending(proposal(title="Implement policy audit tests"), binding(thread="stale"), c, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version = ? WHERE pending_id = ?",
            ("kanban-intake-policy/v2", stale.pending_id),
        )
        conn.commit()
    _migrate_audit(c)
    capsys.readouterr()
    monkeypatch.setattr("gateway.kanban_intake.time.time", lambda: 101.0)

    denied_result = handle_reply("취소", binding(thread="deny"), c, store)
    stale_result = handle_reply("승인", binding(thread="stale"), c, store)

    assert denied_result.handled is True
    assert stale_result.handled is True
    assert _transition_rows(store, denied.pending_id)[-1][:3] == (
        "pending", "denied", "user_denied"
    )
    assert _transition_rows(store, stale.pending_id)[-1][:3] == (
        "pending", "needs_revalidation", "policy_version_mismatch"
    )


def test_approval_claim_and_finalize_are_separate_audited_transitions(tmp_path, monkeypatch, capsys):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(proposal(title="Implement approval transition tests"), binding(), c, now=100)
    _migrate_audit(c)
    capsys.readouterr()
    monkeypatch.setattr("gateway.kanban_intake.time.time", lambda: 101.0)
    monkeypatch.setattr(
        "gateway.kanban_intake.execute_pending_approval",
        lambda *_: ApprovalResult(True, "ok", task_id="t_test", verified=True, action="approve"),
    )

    result = handle_reply("승인", binding(), c, store)

    assert result.verified is True
    assert [row[:3] for row in _transition_rows(store, pending.pending_id)] == [
        ("pending", "pending", "legacy_state_observed"),
        ("pending", "executing", "approval_execution_claimed"),
        ("executing", "executed", "approval_execution_succeeded"),
    ]


def _execution_fixture(tmp_path, capsys):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(
        proposal(title="Implement crash reconciliation tests"), binding(), c, now=100
    )
    _migrate_audit(c)
    capsys.readouterr()
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="executing",
        reason_code="approval_execution_claimed",
        invocation_key=f"reply:v1:{pending.pending_id}:claim",
        now=101,
    )
    board_path = tmp_path / "board.db"
    from hermes_cli import kanban_db as kb

    with sqlite3.connect(board_path) as conn:
        conn.executescript(kb.SCHEMA_SQL)
    return c, store, pending, board_path


def test_retry_resumes_execution_after_crash_before_board_commit(
    tmp_path, monkeypatch, capsys
):
    c, store, pending, board_path = _execution_fixture(tmp_path, capsys)
    monkeypatch.setattr("gateway.kanban_intake.time.time", lambda: 300.0)
    from hermes_cli import kanban_db as kb

    def connect(*_args, **_kwargs):
        conn = sqlite3.connect(board_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(
        "gateway.kanban_intake.execute_pending_approval",
        lambda *_: ApprovalResult(
            True, "resumed", task_id="t_resumed", verified=True, action="approve"
        ),
    )
    result = handle_reply("승인", binding(), c, store)
    assert result.verified is True
    assert _transition_rows(store, pending.pending_id)[-1][:3] == (
        "executing",
        "executed",
        "approval_execution_succeeded",
    )


def test_retry_reconciles_execution_after_crash_after_board_commit(
    tmp_path, monkeypatch, capsys
):
    c, store, pending, board_path = _execution_fixture(tmp_path, capsys)
    monkeypatch.setattr("gateway.kanban_intake.time.time", lambda: 300.0)
    from gateway.kanban_intake import _safe_contract_body

    with sqlite3.connect(board_path) as conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, body, assignee, status, priority, created_by, created_at, "
            "workspace_kind, tenant, idempotency_key, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t_committed",
                pending.proposal.title,
                _safe_contract_body(pending.proposal),
                pending.proposal.assignee,
                pending.proposal.proposed_status,
                int(pending.proposal.priority),
                "kanban-intake",
                101,
                "scratch",
                pending.proposal.tenant,
                pending.proposal.idempotency_key,
                pending.binding.session_key,
            ),
        )
    from hermes_cli import kanban_db as kb

    def connect(*_args, **_kwargs):
        conn = sqlite3.connect(board_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(
        "gateway.kanban_intake.execute_pending_approval",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not execute again")),
    )
    result = handle_reply("승인", binding(), c, store)
    assert result.verified is True
    assert result.task_id == "t_committed"
    assert _transition_rows(store, pending.pending_id)[-1][:3] == (
        "executing",
        "executed",
        "approval_execution_succeeded",
    )


def test_retention_purge_removes_due_row_and_matching_audits_but_not_early(tmp_path, capsys):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(
        proposal(title="Implement paired retention purge for Kanban intake"),
        binding(),
        c,
        now=100,
    )
    _migrate_audit(c)
    capsys.readouterr()
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="dismissed",
        reason_code="cleanup_confirmed",
        invocation_key="external:v1:retention",
        now=101,
    )

    assert store.purge(now=pending.purge_after - 1) == 0
    assert len(_transition_rows(store, pending.pending_id)) == 2
    assert store.purge(now=pending.purge_after) == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_pending WHERE pending_id = ?",
            (pending.pending_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_transition_audit WHERE pending_id = ?",
            (pending.pending_id,),
        ).fetchone()[0] == 0
