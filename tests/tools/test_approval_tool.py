"""Closed pending-intake approval tool operations (temp DB only)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from gateway.kanban_intake import (
    CURRENT_POLICY_VERSION,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
)
from tools import approval


def _cfg(tmp_path):
    return KanbanIntakeConfig(
        enabled=True,
        default_board="test",
        store_path=tmp_path / "pending.db",
        proposal_ttl_seconds=600,
        pending_retention_seconds=3600,
    )


def _binding(
    *,
    user: str = "u1",
    platform: str = "discord",
    chat: str = "c1",
    thread: str | None = "t1",
    session: str = "s1",
):
    return SourceBinding(platform, chat, thread, user, session)


def _pending(tmp_path, *, now=100.0):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(
        KanbanCardProposal(
            board="test",
            title="Implement pending soft delete tests",
            body={"acceptance_criteria": ["closed result"]},
            source_ref="kp_source",
            user_id="u1",
        ),
        _binding(),
        cfg,
        now=now,
    )
    from hermes_cli import kanban_intake_migrate

    assert kanban_intake_migrate.main(
        ["--db", str(cfg.store_path), "--dry-run", "false", "--json"]
    ) == 0
    return cfg, store, pending


def _audit_rows(store):
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM kanban_intake_transition_audit ORDER BY id"
        )]


def test_pending_read_is_same_source_closed_projection_without_body(tmp_path, capsys):
    cfg, store, pending = _pending(tmp_path)
    capsys.readouterr()

    result = approval.pending_read(store, _binding(), pending.pending_id, now=101)

    assert result == {
        "schema": "kanban-intake-pending-operation/v1",
        "action": "pending_read",
        "decision": "allow",
        "pending_id": pending.pending_id,
        "status": "pending",
        "expires_at": 700.0,
        "retain_until": 3700.0,
        "policy_version": CURRENT_POLICY_VERSION,
        "replayed": False,
        "reason_code": None,
    }
    rendered = json.dumps(result, sort_keys=True)
    for forbidden in ("body", "source_ids", "title", "chat_id", "thread_id", "user_id", "session_key"):
        assert forbidden not in rendered

    denied = approval.pending_read(store, _binding(user="u2"), pending.pending_id, now=101)
    assert denied["decision"] == "deny_target_mismatch"
    assert denied["status"] is None
    assert pending.pending_id not in repr(denied)


def test_pending_read_with_matching_nullable_thread_binding_is_allowed(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding(thread=None)
    pending = store.put_pending(
        KanbanCardProposal(
            board="test",
            title="Implement nullable thread binding tests",
            body={"acceptance_criteria": ["zero disclosure"]},
            source_ref="kp_source",
            user_id="u1",
        ),
        binding,
        cfg,
        now=100,
    )
    from hermes_cli import kanban_intake_migrate

    assert kanban_intake_migrate.main(
        ["--db", str(cfg.store_path), "--dry-run", "false", "--json"]
    ) == 0
    capsys.readouterr()
    result = approval.pending_read(store, binding, pending.pending_id, now=101)
    assert result["decision"] == "allow"
    assert result["status"] == "pending"

    mismatch = approval.pending_read(
        store, _binding(thread="unexpected-thread"), pending.pending_id, now=102
    )
    assert mismatch["decision"] == "deny_target_mismatch"


def test_soft_delete_and_restore_are_atomic_exact_replay_transitions(tmp_path, capsys):
    _cfg_obj, store, pending = _pending(tmp_path)
    capsys.readouterr()

    dismissed = approval.pending_soft_delete(
        store,
        _binding(),
        pending.pending_id,
        reason_code="user_dismissed",
        invocation_key="external:v1:dismiss",
        now=101,
    )
    assert dismissed["decision"] == "allow"
    assert dismissed["status"] == "dismissed"
    assert dismissed["replayed"] is False

    replay = approval.pending_soft_delete(
        store,
        _binding(),
        pending.pending_id,
        reason_code="user_dismissed",
        invocation_key="external:v1:dismiss",
        now=102,
    )
    assert replay["decision"] == "allow"
    assert replay["replayed"] is True

    restored = approval.pending_restore(
        store,
        _binding(),
        pending.pending_id,
        invocation_key="external:v1:restore",
        now=103,
    )
    assert restored["decision"] == "allow"
    assert restored["status"] == "pending"
    assert restored["reason_code"] == "user_restored"

    replay_after_restore = approval.pending_soft_delete(
        store,
        _binding(),
        pending.pending_id,
        reason_code="user_dismissed",
        invocation_key="external:v1:dismiss",
        now=104,
    )
    assert replay_after_restore["decision"] == "deny_soft_delete_not_restorable"
    assert replay_after_restore["replayed"] is False
    assert replay_after_restore["status"] is None

    audits = _audit_rows(store)
    transitions = [(r["from_status"], r["to_status"], r["reason_code"]) for r in audits]
    assert transitions == [
        ("pending", "pending", "legacy_state_observed"),
        ("pending", "dismissed", "user_dismissed"),
        ("dismissed", "pending", "user_restored"),
    ]


def test_reason_status_expiry_policy_and_invocation_conflicts_fail_closed(tmp_path, capsys):
    cfg, store, pending = _pending(tmp_path)
    capsys.readouterr()

    bad_reason = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="free form", invocation_key="external:v1:bad", now=101,
    )
    assert bad_reason["decision"] == "deny_schema_invalid"

    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version = ? WHERE pending_id = ?",
            ("old-policy", pending.pending_id),
        )
        conn.commit()
    stale = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="superseded", invocation_key="external:v1:stale", now=101,
    )
    assert stale["decision"] == "deny_soft_delete_not_restorable"

    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version = ? WHERE pending_id = ?",
            (CURRENT_POLICY_VERSION, pending.pending_id),
        )
        conn.commit()
    expired = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="cleanup_confirmed", invocation_key="external:v1:expired",
        now=100 + cfg.proposal_ttl_seconds + 1,
    )
    assert expired["decision"] == "deny_soft_delete_not_restorable"

    first = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="user_dismissed", invocation_key="external:v1:collision", now=101,
    )
    assert first["decision"] == "allow"
    conflict = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="superseded", invocation_key="external:v1:collision", now=102,
    )
    assert conflict["decision"] == "deny_soft_delete_not_restorable"


def test_executing_never_dismisses_restores_or_purges(tmp_path, capsys):
    _cfg_obj, store, pending = _pending(tmp_path)
    capsys.readouterr()
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="executing",
        reason_code="approval_execution_claimed",
        invocation_key="proposal-key:claim",
        now=101,
    )

    dismissed = approval.pending_soft_delete(
        store, _binding(), pending.pending_id,
        reason_code="user_dismissed", invocation_key="external:v1:dismiss-executing", now=102,
    )
    restored = approval.pending_restore(
        store, _binding(), pending.pending_id,
        invocation_key="external:v1:restore-executing", now=102,
    )
    assert dismissed["decision"] == restored["decision"] == "deny_soft_delete_not_restorable"
    assert store.purge(now=999999) == 0
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT status FROM kanban_intake_pending WHERE pending_id=?", (pending.pending_id,)
        ).fetchone()[0] == "executing"
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_transition_audit WHERE pending_id=?", (pending.pending_id,)
        ).fetchone()[0] == 2


def test_unmigrated_executing_row_is_never_purged(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(
        KanbanCardProposal(
            board="test",
            title="Implement unmigrated executing retention tests",
            body={"acceptance_criteria": ["executing remains"]},
            source_ref="kp_source",
            user_id="u1",
        ),
        _binding(),
        cfg,
        now=100,
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET status='executing' WHERE pending_id=?",
            (pending.pending_id,),
        )
        conn.commit()
    assert store.purge(now=999999) == 0


def test_mark_status_is_rejected_after_audit_migration(tmp_path, capsys):
    _cfg_obj, store, pending = _pending(tmp_path)
    capsys.readouterr()
    with pytest.raises(RuntimeError, match="unaudited"):
        store.mark_status(pending.pending_id, "denied", now=101)


def test_tool_operations_fail_owner_unavailable_before_migration(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(
        KanbanCardProposal(
            board="test", title="Implement owner readiness tests",
            body={"acceptance_criteria": ["no auto migration"]},
            source_ref="kp_source", user_id="u1",
        ),
        _binding(), cfg, now=100,
    )
    result = approval.pending_read(store, _binding(), pending.pending_id, now=101)
    assert result["decision"] == "deny_owner_unavailable"
    assert "kanban_intake_transition_audit" not in {
        row[0] for row in sqlite3.connect(store.path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
