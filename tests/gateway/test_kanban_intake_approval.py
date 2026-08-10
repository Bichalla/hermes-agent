import gateway.kanban_intake as intake
from gateway.kanban_intake import (
    APPROVAL,
    DENY,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    apply_typed_proposal_decision,
    validate_proposal,
)


def cfg(tmp_path):
    return KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")


def binding():
    return SourceBinding(platform="discord", chat_id="c1", thread_id="t1", user_id="u1", session_key="s1")


def valid_proposal():
    return KanbanCardProposal(
        board="lifelog-control",
        title="Implement local guardrail scope",
        body={"source_ref": "kp_safe", "acceptance_criteria": ["focused tests pass"]},
        source_ref="kp_safe",
        user_id="u1",
    )


def test_phrase_classifier_is_not_an_effect_authority():
    assert not hasattr(intake, "classify_reply")
    assert not hasattr(intake, "handle_reply")


def test_approval_without_exact_pending_fails_closed(tmp_path):
    c = cfg(tmp_path)
    result = apply_typed_proposal_decision(
        action=APPROVAL,
        proposal_ref="kp_0000000000000000",
        binding=binding(),
        cfg=c,
        store=PendingKanbanStore(c.store_path),
    )
    assert result.handled is True
    assert result.verified is False
    assert "proposal_ref" in result.message


def test_deny_marks_exact_pending_without_card_mutation(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    pending = store.put_pending(valid_proposal(), binding(), c)
    result = apply_typed_proposal_decision(
        action=DENY,
        proposal_ref=pending.pending_id,
        binding=binding(),
        cfg=c,
        store=store,
    )
    assert result.handled is True
    assert result.action == DENY
    assert store.get_active_for_source(binding()).state == "none"


def test_invalid_status_missing_board_and_sensitive_payload_fail_closed(tmp_path):
    c = cfg(tmp_path)
    p = valid_proposal()
    p.proposed_status = "ready"
    assert validate_proposal(p, c)[0] is False
    p = valid_proposal()
    p.board = ""
    c2 = KanbanIntakeConfig(enabled=True, default_board="")
    assert validate_proposal(p, c2)[0] is False
    p = valid_proposal()
    p.title = "아이 fever 기록 원문"
    assert validate_proposal(p, c)[0] is False
    p = valid_proposal()
    p.body = {"chat_id": "1234567890123"}
    assert validate_proposal(p, c)[0] is False
