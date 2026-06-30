from gateway.kanban_intake import (
    APPROVAL,
    DENY,
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    classify_reply,
    handle_reply,
    validate_proposal,
)


def cfg(tmp_path):
    return KanbanIntakeConfig(enabled=True, default_board="lifelog-control", store_path=tmp_path / "pending.db")


def binding():
    return SourceBinding(platform="discord", chat_id="c1", thread_id="t1", user_id="u1", session_key="s1")


def valid_proposal():
    return KanbanCardProposal(
        board="lifelog-control",
        title="Implement local guardrail",
        body={"source_ref": "kp_safe", "acceptance_criteria": ["focused tests pass"]},
        source_ref="kp_safe",
        user_id="u1",
    )


def test_short_reply_classifier(tmp_path):
    c = cfg(tmp_path)
    assert classify_reply("승인", c) == APPROVAL
    assert classify_reply("ㅇㅇ", c) == APPROVAL
    assert classify_reply("고고", c) == APPROVAL
    assert classify_reply("그렇게 해", c) == APPROVAL
    assert classify_reply("취소", c) == DENY
    assert classify_reply("보류", c) == DENY
    assert classify_reply("그냥 설명해줘", c) == "none"


def test_approval_without_pending_is_not_handled(tmp_path):
    c = cfg(tmp_path)
    result = handle_reply("승인", binding(), c, PendingKanbanStore(c.store_path))
    assert result.handled is False


def test_deny_marks_pending_without_mutation(tmp_path):
    c = cfg(tmp_path)
    store = PendingKanbanStore(c.store_path)
    store.put_pending(valid_proposal(), binding(), c)
    result = handle_reply("취소", binding(), c, store)
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
