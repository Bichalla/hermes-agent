from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from gateway.kanban_intake import (
    APPROVAL,
    DENY,
    ApprovalResult,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    migrate_transition_audit,
    transition_audit_ready,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource
from gateway.session_context import (
    TrustedKanbanProposalCapability,
    clear_session_vars,
    set_session_vars,
)
from tools.workflow_authority import (
    _mint_host_current_turn_user_authority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
)


def _cfg(tmp_path) -> KanbanIntakeConfig:
    return KanbanIntakeConfig(
        enabled=True,
        default_board="lifelog-control",
        store_path=tmp_path / "pending.db",
    )


def _binding(*, session_key: str = "before-reset", user_id: str = "11111111111111111", thread_id: str = "33333333333333333") -> SourceBinding:
    return SourceBinding(
        platform="discord",
        chat_id=thread_id,
        thread_id=thread_id,
        user_id=user_id,
        session_key=session_key,
        message_id="55555555555555555",
    )


def _proposal() -> KanbanCardProposal:
    return KanbanCardProposal(
        board="lifelog-control",
        title="Implement Kanban intake approval guardrail",
        body={"source_ref": "source-safe", "acceptance_criteria": ["no-live tests pass"]},
        source_ref="source-safe",
        user_id="11111111111111111",
        tenant="lifelog",
        assignee="honbul",
    )


def _bind_delivery(
    store: PendingKanbanStore,
    pending,
    binding: SourceBinding,
    *,
    now: float | None = None,
):
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        ["55555555555555555"],
        now=now,
    )
    refreshed = store.get_active_by_proposal_ref(
        pending.pending_id,
        binding,
        now=now,
    )
    assert refreshed.pending is not None
    return refreshed.pending


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="33333333333333333",
        chat_type="thread",
        thread_id="33333333333333333",
        user_id="11111111111111111",
        scope_id="44444444444444444",
        parent_chat_id="22222222222222222",
        message_id="66666666666666666",
    )


def _bind_host_authority(pending=None, capability=None) -> list:
    authority = _mint_host_current_turn_user_authority(
        turn_id="kanban-semantic-turn",
        source_role="user",
        session_scope="after-reset",
        platform_scope="discord",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("semantic approval selected by model"),
        source_event_fingerprint=fingerprint_user_action("discord current source event"),
        allowed_action_classes=frozenset(),
        allowed_operations=frozenset(),
        operation_target_grants=frozenset(),
        target_fingerprints=frozenset(),
    )
    bind_current_turn_user_authority(authority)
    bind_active_workflow_turn(
        authority.turn_id,
        authority.platform_scope,
        authority.session_scope,
    )
    if capability is None and pending is not None:
        capability = TrustedKanbanProposalCapability(
            proposal_ref=pending.pending_id,
            proposal_digest=pending.proposal_digest,
            platform="discord",
            chat_id="33333333333333333",
            thread_id="33333333333333333",
            authenticated_sender_id="11111111111111111",
            current_message_id="66666666666666666",
            replied_to_message_id="55555555555555555",
        )
    return set_session_vars(
        platform=capability.platform if capability is not None else "discord",
        chat_id=(
            capability.chat_id
            if capability is not None
            else "33333333333333333"
        ),
        thread_id=(
            capability.thread_id
            if capability is not None
            else "33333333333333333"
        ),
        user_id=(
            capability.authenticated_sender_id
            if capability is not None
            else "11111111111111111"
        ),
        session_id="after-reset",
        message_id=(
            capability.current_message_id
            if capability is not None
            else "66666666666666666"
        ),
        controller_role="main_controller",
        trusted_kanban_proposal=capability,
    )


def test_proposal_message_exposes_only_opaque_proposal_ref(tmp_path):
    import gateway.kanban_intake as intake

    store = PendingKanbanStore(_cfg(tmp_path).store_path)
    pending = store.put_pending(_proposal(), _binding(), _cfg(tmp_path), now=100)

    rendered = intake.render_proposal_message(pending.proposal, proposal_ref=pending.pending_id)

    assert f"proposal_ref: {pending.pending_id}" in rendered
    assert "승인/ㅇㅇ/고고" not in rendered
    assert "typed action" in rendered.lower()


def test_exact_proposal_lookup_survives_session_reset_but_not_route_or_user_change(tmp_path):
    store = PendingKanbanStore(_cfg(tmp_path).store_path)
    pending = store.put_pending(_proposal(), _binding(), _cfg(tmp_path), now=100)

    exact = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(session_key="after-reset"),
        now=101,
    )
    wrong_user = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(session_key="after-reset", user_id="99999999999999999"),
        now=101,
    )
    wrong_route = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(session_key="after-reset", thread_id="77777777777777777"),
        now=101,
    )

    assert exact.state == "one"
    assert exact.pending is not None
    assert exact.pending.pending_id == pending.pending_id
    assert wrong_user.state == "none"
    assert wrong_route.state == "none"


def test_stale_run_cleanup_cannot_invalidate_delivered_proposal(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        ["55555555555555555"],
    )

    with pytest.raises(ValueError, match="already delivered"):
        store.transition_status(
            pending.pending_id,
            expected_status="pending",
            status="invalid",
            reason_code="proposal_run_invalidated",
            invocation_key=f"delivery:v1:{pending.pending_id}:stale-run",
            require_undelivered=True,
        )

    exact = store.get_pending_capability_for_reply(
        binding,
        "55555555555555555",
    )
    assert exact.state == "one"
    assert exact.pending is not None
    assert exact.pending.status == "pending"


def test_delivery_authority_expires_across_process_epoch(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake_module

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    delivered_id = "1521423652547989692"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        [delivered_id],
    )
    assert store.get_pending_capability_for_reply(
        binding,
        delivered_id,
    ).state == "one"
    undelivered_binding = _binding(session_key="undelivered-session")
    undelivered_proposal = _proposal()
    undelivered_proposal.title = "Implement undelivered restart authority regression"
    store.put_pending(
        undelivered_proposal,
        undelivered_binding,
        cfg,
    )
    assert store.get_active_for_source(undelivered_binding).state == "one"

    monkeypatch.setattr(
        intake_module,
        "_DELIVERY_AUTHORITY_EPOCH",
        "replacement-process-epoch",
    )

    assert store.get_pending_capability_for_reply(
        binding,
        delivered_id,
    ).state == "none"
    assert store.get_active_for_source(binding).state == "none"
    assert store.get_active_for_source(undelivered_binding).state == "none"


@pytest.mark.parametrize("revocation", ["deny", "epoch"])
def test_revoked_minted_capability_cannot_reach_owner(
    tmp_path,
    monkeypatch,
    revocation,
):
    import gateway.kanban_intake as intake_module

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    delivered_id = "1521423652547989696"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        [delivered_id],
    )
    minted = store.get_pending_capability_for_reply(binding, delivered_id)
    assert minted.pending is not None
    owner_calls = []
    monkeypatch.setattr(
        intake_module,
        "execute_pending_approval",
        lambda *_args, **_kwargs: owner_calls.append("owner")
        or ApprovalResult(True, "must not happen", verified=True),
    )

    if revocation == "deny":
        store.deny_delivery_authority(pending.pending_id)
    else:
        monkeypatch.setattr(
            intake_module,
            "_DELIVERY_AUTHORITY_EPOCH",
            "replacement-process-epoch",
        )

    result = intake_module.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=binding,
        cfg=cfg,
        store=store,
        expected_proposal_digest=minted.pending.proposal_digest,
        expected_reply_message_id=delivered_id,
    )

    assert result.verified is False
    assert owner_calls == []


def test_rebind_replaces_denied_delivery_ids(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    old_id = "1521423652547989697"
    new_id = "1521423652547989698"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        [old_id],
    )
    store.deny_delivery_authority(pending.pending_id)
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        [new_id],
    )

    assert store.get_pending_capability_for_reply(binding, old_id).state == "none"
    assert store.get_pending_capability_for_reply(binding, new_id).state == "one"


def test_revocation_between_lookup_and_claim_blocks_owner(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake_module

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    delivered_id = "1521423652547989699"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        binding,
        [delivered_id],
    )
    minted = store.get_pending_capability_for_reply(binding, delivered_id)
    assert minted.pending is not None
    initial_lookup_done = threading.Event()
    release_initial_lookup = threading.Event()
    lookup_state = threading.local()
    original_lookup = store.get_active_by_proposal_ref

    def blocking_lookup(*args, **kwargs):
        result = original_lookup(*args, **kwargs)
        if not getattr(lookup_state, "seen", False):
            lookup_state.seen = True
            initial_lookup_done.set()
            release_initial_lookup.wait(timeout=5)
        return result

    monkeypatch.setattr(store, "get_active_by_proposal_ref", blocking_lookup)
    owner_calls = []
    monkeypatch.setattr(
        intake_module,
        "execute_pending_approval",
        lambda *_args, **_kwargs: owner_calls.append("owner")
        or ApprovalResult(True, "must not happen", verified=True),
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            intake_module.apply_typed_proposal_decision,
            action=APPROVAL,
            proposal_ref=pending.pending_id,
            binding=binding,
            cfg=cfg,
            store=store,
            expected_proposal_digest=minted.pending.proposal_digest,
            expected_reply_message_id=delivered_id,
        )
        assert initial_lookup_done.wait(timeout=5)
        store.deny_delivery_authority(pending.pending_id)
        release_initial_lookup.set()
        result = future.result(timeout=5)

    assert result.verified is False
    assert owner_calls == []


def test_claim_and_put_use_one_authority_then_sqlite_lock_order(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    claim_binding = _binding()
    pending = store.put_pending(_proposal(), claim_binding, cfg)
    delivered_id = "1521423652547989702"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        claim_binding,
        [delivered_id],
    )
    pending = store.get_active_by_proposal_ref(
        pending.pending_id,
        claim_binding,
    ).pending
    assert pending is not None

    put_binding = _binding(
        thread_id="2521423652547989703",
        session_key="parallel-put",
    )
    put_proposal = _proposal()
    put_proposal.title = "Implement parallel proposal lock-order regression"
    start = threading.Barrier(2)

    def claim():
        start.wait(timeout=5)
        return store.claim_delivery_authorized_execution(
            pending.pending_id,
            claim_binding,
            expected_proposal_digest=pending.proposal_digest,
            expected_reply_message_id=delivered_id,
        )

    def put():
        start.wait(timeout=5)
        return store.put_pending(put_proposal, put_binding, cfg)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim)
        put_future = pool.submit(put)
        claim_result = claim_future.result(timeout=5)
        put_result = put_future.result(timeout=5)

    assert claim_result["replayed"] is False
    claimed = store.get_active_by_proposal_ref(
        pending.pending_id,
        claim_binding,
    ).pending
    assert claimed is not None and claimed.status == "executing"
    assert put_result.status == "pending"


def test_proposal_digest_drift_fails_closed_before_owner(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET title = ? WHERE pending_id = ?",
            ("Tampered title", pending.pending_id),
        )
        conn.commit()
    monkeypatch.setattr(
        intake,
        "execute_pending_approval",
        lambda *_args, **_kwargs: pytest.fail("owner reached after digest drift"),
    )

    result = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=101,
    )

    assert result.handled is True
    assert result.verified is False
    assert "digest" in result.message.lower()


def test_typed_decision_approve_is_exact_and_idempotent_no_live(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    delivery_id = "1521423652547989693"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        _binding(),
        [delivery_id],
        now=100.5,
    )
    pending = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(),
        now=100.6,
    ).pending
    assert pending is not None
    calls = []
    monkeypatch.setattr(
        intake,
        "execute_pending_approval",
        lambda selected, _cfg: calls.append(selected.pending_id)
        or ApprovalResult(
            True,
            "synthetic no-live verified",
            task_id="t_synthetic",
            verified=True,
            action=APPROVAL,
        ),
    )
    readbacks = []
    monkeypatch.setattr(
        intake,
        "readback_executed_pending",
        lambda selected, _cfg: readbacks.append(selected.pending_id)
        or ApprovalResult(
            True,
            "synthetic canonical replay readback",
            task_id="t_synthetic",
            verified=True,
            action=APPROVAL,
        ),
    )

    first = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=101,
        expected_proposal_digest=pending.proposal_digest,
        expected_reply_message_id=delivery_id,
    )
    replay = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="another-reset"),
        cfg=cfg,
        store=store,
        now=102,
    )

    assert first.verified is True
    assert replay.verified is True
    assert replay.task_id == "t_synthetic"
    assert calls == [pending.pending_id]
    assert readbacks == [pending.pending_id]


def test_typed_decision_deny_wins_without_owner_call(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    monkeypatch.setattr(
        intake,
        "execute_pending_approval",
        lambda *_args, **_kwargs: pytest.fail("deny reached owner"),
    )

    result = intake.apply_typed_proposal_decision(
        action=DENY,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=101,
    )

    assert result.handled is True
    assert result.action == DENY
    assert store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(session_key="after-reset"),
        now=102,
    ).state == "none"


def test_tool_schema_is_closed_to_action_and_proposal_ref():
    import tools.kanban_tools as tool

    schema = tool.KANBAN_INTAKE_DECISION_SCHEMA
    params = schema["parameters"]
    assert params["required"] == ["action", "proposal_ref"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"action", "proposal_ref"}
    assert params["properties"]["action"]["enum"] == [APPROVAL, DENY]
    assert params["properties"]["proposal_ref"]["pattern"] == r"^kp_[a-f0-9]{16}$"


def test_tool_requires_host_sealed_current_main_controller(tmp_path, monkeypatch):
    import tools.kanban_tools as tool

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    pending = _bind_delivery(store, pending, binding)
    monkeypatch.setattr(
        tool,
        "load_config",
        lambda: {
            "kanban": {
                "conversational_intake": {
                    "enabled": True,
                    "default_board": "lifelog-control",
                    "store_path": str(cfg.store_path),
                }
            }
        },
    )
    monkeypatch.setattr(
        "gateway.kanban_intake.execute_pending_approval",
        lambda selected, _cfg: ApprovalResult(
            True,
            "synthetic no-live verified",
            task_id="t_synthetic",
            verified=True,
            action=APPROVAL,
        ),
    )

    missing = tool._handle_kanban_intake_decision(
        {"action": APPROVAL, "proposal_ref": pending.pending_id}
    )
    sealed_only_tokens = _bind_host_authority()
    try:
        sealed_only = tool._handle_kanban_intake_decision(
            {"action": APPROVAL, "proposal_ref": pending.pending_id}
        )
    finally:
        clear_current_turn_user_authority()
        clear_session_vars(sealed_only_tokens)
    tokens = _bind_host_authority(pending)
    try:
        mismatch = tool._handle_kanban_intake_decision(
            {"action": APPROVAL, "proposal_ref": "kp_aaaaaaaaaaaaaaaa"}
        )
        allowed = tool._handle_kanban_intake_decision(
            {"action": APPROVAL, "proposal_ref": pending.pending_id}
        )
    finally:
        clear_current_turn_user_authority()
        clear_session_vars(tokens)

    assert "authority" in missing.lower()
    assert "reply proposal capability" in sealed_only.lower()
    assert "does not match current reply capability" in mismatch.lower()
    assert "t_synthetic" in allowed


@pytest.mark.asyncio
async def test_gateway_does_not_preempt_llm_with_phrase_classifier(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    store.put_pending(_proposal(), _binding(session_key="after-reset"), cfg, now=100)
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda cfg=None: store
    event = MessageEvent(
        text="승인",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="66666666666666666",
    )

    result = await runner._maybe_handle_kanban_intake_reply(event, "after-reset")

    assert result is None


def test_latest_batched_cancel_is_a_closed_typed_deny_not_first_phrase_approval(tmp_path):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)

    # The LLM sees the entire logical batch and chooses the closed deny action.
    # The deterministic layer receives no natural-language text at all.
    result = intake.apply_typed_proposal_decision(
        action=DENY,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=101,
    )

    assert result.action == DENY
    assert result.verified is False


def test_blocked_only_rejects_triage_at_store_and_final_owner(tmp_path):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    triage = _proposal()
    triage.proposed_status = "triage"
    with pytest.raises(ValueError, match="status"):
        store.put_pending(triage, _binding(), cfg)

    pending = store.put_pending(_proposal(), _binding(), cfg)
    pending.proposal.proposed_status = "triage"
    result = intake.execute_pending_approval(pending, cfg)
    assert result.verified is False
    assert "unsupported status" in result.message


def test_expired_executing_proposal_remains_reconcilable(tmp_path, monkeypatch):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="executing",
        reason_code="approval_execution_claimed",
        invocation_key=f"typed:v1:{pending.pending_id}:claim",
        now=101,
    )
    reconciled = []
    monkeypatch.setattr(
        intake,
        "reconcile_or_resume_pending_execution",
        lambda selected, _cfg, _store: reconciled.append(selected.pending_id)
        or ApprovalResult(True, "reconciled", verified=True, action=APPROVAL),
    )

    result = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=100 + cfg.proposal_ttl_seconds + 1,
    )

    assert result.verified is True
    assert reconciled == [pending.pending_id]


def test_pre_digest_database_migrates_and_stale_policy_needs_revalidation(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version=? WHERE pending_id=?",
            ("kanban-intake-policy/v3", pending.pending_id),
        )
        conn.commit()
    assert cfg.store_path is not None
    with sqlite3.connect(cfg.store_path) as conn:
        conn.execute("ALTER TABLE kanban_intake_pending DROP COLUMN proposal_digest")
        conn.commit()

    result = __import__(
        "gateway.kanban_intake", fromlist=["apply_typed_proposal_decision"]
    ).apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=101,
    )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, proposal_digest FROM kanban_intake_pending WHERE pending_id=?",
            (pending.pending_id,),
        ).fetchone()
    assert result.verified is False
    assert row[0] == "needs_revalidation"
    assert row[1] == ""


def test_legacy_blank_digest_does_not_block_first_post_upgrade_detection(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    first = store.put_pending(_proposal(), _binding(), cfg, now=100)
    with store.connect() as conn:
        conn.execute(
            "UPDATE kanban_intake_pending SET policy_version=? WHERE pending_id=?",
            ("kanban-intake-policy/v3", first.pending_id),
        )
        conn.commit()
    assert cfg.store_path is not None
    with sqlite3.connect(cfg.store_path) as conn:
        conn.execute("ALTER TABLE kanban_intake_pending DROP COLUMN proposal_digest")
        conn.commit()

    fresh = _proposal()
    fresh.body["acceptance_criteria"] = ["post-upgrade proposal remains usable"]
    created = PendingKanbanStore(cfg.store_path).put_pending(
        fresh,
        _binding(),
        cfg,
        now=101,
    )
    assert created.pending_id != first.pending_id
    assert created.status == "pending"


def test_expired_pending_is_not_returned_by_redetection_dedup(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    first = store.put_pending(_proposal(), _binding(), cfg, now=100)
    second = store.put_pending(
        _proposal(),
        _binding(session_key="after-reset"),
        cfg,
        now=100 + cfg.proposal_ttl_seconds + 1,
    )
    assert second.pending_id != first.pending_id
    assert second.status == "pending"


def test_effect_idempotency_is_stable_across_detection_and_session_reset(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    first = store.put_pending(_proposal(), _binding(session_key="s1"), cfg, now=100)
    second_proposal = _proposal()
    second_proposal.source_ref = "different-detection-ref"
    second_proposal.body["source_ref"] = "different-detection-ref"
    second = store.put_pending(
        second_proposal,
        _binding(session_key="s2"),
        cfg,
        now=101,
    )
    assert first.proposal.idempotency_key == second.proposal.idempotency_key
    assert first.pending_id == second.pending_id

    different = _proposal()
    different.body["acceptance_criteria"] = ["different card contract"]
    third = store.put_pending(
        different,
        _binding(session_key="s3"),
        cfg,
        now=102,
    )
    assert third.proposal.idempotency_key != first.proposal.idempotency_key
    assert third.pending_id != first.pending_id

    different_why = _proposal()
    different_why.why = "Different deterministic card outcome rationale"
    fourth = store.put_pending(
        different_why,
        _binding(session_key="s4"),
        cfg,
        now=103,
    )
    assert fourth.proposal.idempotency_key != first.proposal.idempotency_key
    assert fourth.pending_id != first.pending_id


@pytest.mark.asyncio
async def test_gateway_binds_exact_capability_only_for_own_message_reply(tmp_path):
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding()
    pending = store.put_pending(_proposal(), binding, cfg)
    pending = _bind_delivery(store, pending, binding)
    second_proposal = _proposal()
    second_proposal.body["acceptance_criteria"] = ["different exact target"]
    second_binding = _binding(session_key="s2")
    second = store.put_pending(second_proposal, second_binding, cfg)
    assert store.bind_outbound_proposal_messages(
        second.pending_id,
        second_binding,
        ["77777777777777777"],
    )
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda cfg=None: store
    event = MessageEvent(
        text="current semantic decision",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="66666666666666666",
        reply_to_message_id="55555555555555555",
        reply_to_text="long prior assistant message",
        reply_to_author_id="88888888888888888",
        reply_to_is_own_message=True,
    )

    capability = await runner._trusted_kanban_proposal_for_reply(
        event,
        "after-reset",
    )
    event.reply_to_message_id = "99999999999999999"
    unrelated = await runner._trusted_kanban_proposal_for_reply(
        event,
        "after-reset",
    )
    event.reply_to_message_id = "77777777777777777"
    second_capability = await runner._trusted_kanban_proposal_for_reply(
        event,
        "after-reset",
    )
    event.reply_to_message_id = "55555555555555555"
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="executing",
        reason_code="approval_execution_claimed",
        invocation_key=f"typed:v1:{pending.pending_id}:claim",
    )
    executing_capability = await runner._trusted_kanban_proposal_for_reply(
        event,
        "after-reset",
    )
    event.reply_to_is_own_message = False
    absent = await runner._trusted_kanban_proposal_for_reply(event, "after-reset")

    assert capability is not None
    assert capability.proposal_ref == pending.pending_id
    assert capability.proposal_digest == pending.proposal_digest
    assert unrelated is None
    assert second_capability is not None
    assert second_capability.proposal_ref == second.pending_id
    assert executing_capability is not None
    assert executing_capability.proposal_ref == pending.pending_id
    assert absent is None


def test_executed_replay_uses_canonical_temp_card_readback(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    import gateway.kanban_intake as intake

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    board = "lifelog-control"
    kb.create_board(board, name="Lifelog Control")
    cfg = KanbanIntakeConfig(
        enabled=True,
        default_board=board,
        store_path=tmp_path / "pending.db",
    )
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg)
    delivery_id = "1521423652547989694"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        _binding(),
        [delivery_id],
    )
    pending = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(),
    ).pending
    assert pending is not None

    first = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="first"),
        cfg=cfg,
        store=store,
        expected_proposal_digest=pending.proposal_digest,
        expected_reply_message_id=delivery_id,
    )
    replay = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="reset"),
        cfg=cfg,
        store=store,
    )
    deny_after = intake.apply_typed_proposal_decision(
        action=DENY,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="reset"),
        cfg=cfg,
        store=store,
    )
    conn = kb.connect(board=board)
    try:
        tasks = kb.list_tasks(conn, include_archived=True)
    finally:
        conn.close()

    assert first.verified is True
    assert replay.verified is True
    assert replay.task_id == first.task_id
    assert deny_after.verified is False
    assert "취소하지 않았다" in deny_after.message
    assert len(tasks) == 1
    assert tasks[0].status == "blocked"


@pytest.mark.parametrize("card_committed_before_crash", [False, True])
def test_real_post_ttl_crash_recovery_is_idempotent(
    tmp_path,
    monkeypatch,
    card_committed_before_crash,
):
    from hermes_cli import kanban_db as kb
    import gateway.kanban_intake as intake

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    kb.create_board("lifelog-control", name="Lifelog Control")
    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg, now=100)
    store.transition_status(
        pending.pending_id,
        expected_status="pending",
        status="executing",
        reason_code="approval_execution_claimed",
        invocation_key=f"typed:v1:{pending.pending_id}:claim",
        now=101,
    )
    if card_committed_before_crash:
        committed = intake.execute_pending_approval(pending, cfg)
        assert committed.verified is True

    recovered = intake.apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref=pending.pending_id,
        binding=_binding(session_key="after-reset"),
        cfg=cfg,
        store=store,
        now=100 + cfg.proposal_ttl_seconds + 1,
    )
    conn = kb.connect(board="lifelog-control")
    try:
        tasks = kb.list_tasks(conn, include_archived=True)
    finally:
        conn.close()

    assert recovered.verified is True
    assert len(tasks) == 1
    assert tasks[0].status == "blocked"


@pytest.mark.parametrize("audited", [True, False])
@pytest.mark.parametrize(
    "actions",
    [(APPROVAL, APPROVAL), (APPROVAL, DENY), (DENY, DENY)],
)
def test_concurrent_decision_matrix_is_controlled(
    tmp_path,
    monkeypatch,
    audited,
    actions,
):
    import gateway.kanban_intake as intake

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    pending = store.put_pending(_proposal(), _binding(), cfg)
    delivery_id = "1521423652547989695"
    assert store.bind_outbound_proposal_messages(
        pending.pending_id,
        _binding(),
        [delivery_id],
    )
    pending = store.get_active_by_proposal_ref(
        pending.pending_id,
        _binding(),
    ).pending
    assert pending is not None
    with store.connect() as conn:
        if audited:
            migrate_transition_audit(conn, dry_run=False)
            assert transition_audit_ready(conn) is True
        else:
            assert transition_audit_ready(conn) is False
    owner_calls = []
    owner_lock = threading.Lock()

    def _owner(selected, _cfg):
        with owner_lock:
            owner_calls.append(selected.pending_id)
        return ApprovalResult(
            True,
            "synthetic controlled owner",
            task_id="t_synthetic",
            verified=True,
            action=APPROVAL,
        )

    monkeypatch.setattr(intake, "execute_pending_approval", _owner)
    barrier = threading.Barrier(2)
    lookup_state = threading.local()
    original_lookup = store.get_active_by_proposal_ref

    def _lookup(*args, **kwargs):
        result = original_lookup(*args, **kwargs)
        if (
            result.pending is not None
            and result.pending.status == "pending"
            and not getattr(lookup_state, "barrier_seen", False)
        ):
            lookup_state.barrier_seen = True
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(store, "get_active_by_proposal_ref", _lookup)

    def _decide(action):
        return intake.apply_typed_proposal_decision(
            action=action,
            proposal_ref=pending.pending_id,
            binding=_binding(session_key="race"),
            cfg=cfg,
            store=store,
            expected_proposal_digest=pending.proposal_digest,
            expected_reply_message_id=delivery_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_decide, action) for action in actions]
        results = [future.result(timeout=10) for future in futures]
    with store.connect() as conn:
        final_status = conn.execute(
            "SELECT status FROM kanban_intake_pending WHERE pending_id=?",
            (pending.pending_id,),
        ).fetchone()[0]

    assert all(isinstance(result, ApprovalResult) for result in results)
    assert len(owner_calls) <= 1
    if actions == (APPROVAL, APPROVAL):
        assert final_status == "executed"
        assert len(owner_calls) == 1
    elif actions == (DENY, DENY):
        assert final_status == "denied"
        assert owner_calls == []
    else:
        assert final_status in {"executed", "denied"}
        assert len(owner_calls) == (1 if final_status == "executed" else 0)


@pytest.mark.asyncio
async def test_discord_batch_uses_latest_event_provenance_for_cancel():
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    object.__setattr__(adapter, "config", SimpleNamespace(extra={}))
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 60.0
    adapter._text_batch_split_delay_seconds = 60.0
    first = MessageEvent(
        text="approve",
        source=_source(),
        message_id="66666666666666666",
        reply_to_message_id="55555555555555555",
        reply_to_text="proposal",
        reply_to_author_id="88888888888888888",
        reply_to_is_own_message=True,
    )
    latest_source = _source()
    latest_source.message_id = "77777777777777777"
    latest = MessageEvent(
        text="cancel",
        source=latest_source,
        message_id="77777777777777777",
    )

    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(latest)
    key = adapter._text_batch_key(first)
    batched = adapter._pending_text_batches[key]
    try:
        assert batched.text == "approve\ncancel"
        assert batched.message_id == latest.message_id
        assert batched.source.message_id == latest.message_id
        assert batched.reply_to_message_id is None
        assert batched.reply_to_is_own_message is False
        assert batched.trusted_batch_reply_to_message_id == "55555555555555555"
        assert batched.trusted_batch_reply_to_is_own_message is True
    finally:
        for task in adapter._pending_text_batch_tasks.values():
            task.cancel()
        await asyncio.gather(
            *adapter._pending_text_batch_tasks.values(),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_adapter_batch_target_reaches_closed_deny_tool(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    import tools.kanban_tools as tool

    cfg = _cfg(tmp_path)
    store = PendingKanbanStore(cfg.store_path)
    binding = _binding(session_key="after-reset")
    pending = store.put_pending(_proposal(), binding, cfg)
    pending = _bind_delivery(store, pending, binding)
    adapter = object.__new__(DiscordAdapter)
    object.__setattr__(adapter, "config", SimpleNamespace(extra={}))
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 60.0
    adapter._text_batch_split_delay_seconds = 60.0
    first = MessageEvent(
        text="approve",
        source=_source(),
        message_id="66666666666666666",
        reply_to_message_id="55555555555555555",
        reply_to_is_own_message=True,
    )
    latest_source = _source()
    latest_source.message_id = "77777777777777777"
    latest = MessageEvent(
        text="actually cancel",
        source=latest_source,
        message_id="77777777777777777",
    )
    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(latest)
    key = adapter._text_batch_key(first)
    batched = adapter._pending_text_batches[key]
    runner = object.__new__(GatewayRunner)
    runner._kanban_intake_config = lambda: cfg
    runner._kanban_intake_store = lambda cfg=None: store
    capability = await runner._trusted_kanban_proposal_for_reply(
        batched,
        "after-reset",
    )
    assert capability is not None
    monkeypatch.setattr(
        tool,
        "load_config",
        lambda: {
            "kanban": {
                "conversational_intake": {
                    "enabled": True,
                    "default_board": "lifelog-control",
                    "store_path": str(cfg.store_path),
                }
            }
        },
    )
    tokens = _bind_host_authority(capability=capability)
    try:
        result = tool._handle_kanban_intake_decision(
            {"action": DENY, "proposal_ref": pending.pending_id}
        )
    finally:
        clear_current_turn_user_authority()
        clear_session_vars(tokens)
        for task in adapter._pending_text_batch_tasks.values():
            task.cancel()
        await asyncio.gather(
            *adapter._pending_text_batch_tasks.values(),
            return_exceptions=True,
        )
    with store.connect() as conn:
        status = conn.execute(
            "SELECT status FROM kanban_intake_pending WHERE pending_id=?",
            (pending.pending_id,),
        ).fetchone()[0]

    assert '"ok": true' in result.lower()
    assert status == "denied"
