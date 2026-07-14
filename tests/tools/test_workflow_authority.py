"""Authority provenance tests for the accepted current user turn."""

from __future__ import annotations

import concurrent.futures

import pytest

from tools.thread_context import propagate_context_to_thread
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
    fingerprint_workflow_target,
    get_current_turn_user_authority,
    infer_explicit_blocked_create_targets,
    infer_explicit_workflow_scope,
    opaque_workflow_action_id,
    reset_current_turn_user_authority,
    select_blocked_create_target_fingerprint,
)


@pytest.fixture(autouse=True)
def _clear_authority():
    clear_current_turn_user_authority()
    yield
    clear_current_turn_user_authority()


def _authority() -> CurrentTurnUserAuthority:
    return CurrentTurnUserAuthority(
        turn_id="opaque-turn-1",
        source_role="user",
        session_scope="session-1",
        platform_scope="discord",
        user_message_index=4,
        user_action_fingerprint=fingerprint_user_action("create blocked card"),
    )


def test_authority_is_immutable_and_contains_no_raw_text():
    authority = _authority()
    with pytest.raises((AttributeError, TypeError)):
        authority.turn_id = "other"  # type: ignore[misc]
    assert not hasattr(authority, "message")
    assert not hasattr(authority, "content")
    assert "raw" not in repr(authority).lower()


def test_only_user_role_can_construct_authority():
    with pytest.raises(ValueError, match="source_role"):
        CurrentTurnUserAuthority(
            turn_id="opaque-turn-2",
            source_role="assistant",
            session_scope="session-1",
            platform_scope="discord",
            user_message_index=5,
        )


def test_bind_and_clear_authority():
    authority = _authority()
    bind_current_turn_user_authority(authority)
    assert get_current_turn_user_authority() is authority
    clear_current_turn_user_authority()
    assert get_current_turn_user_authority() is None


def test_authority_propagates_through_audited_tool_thread_wrapper():
    bind_current_turn_user_authority(_authority())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        observed = executor.submit(
            propagate_context_to_thread(get_current_turn_user_authority)
        ).result(timeout=5)
    assert observed == _authority()


def test_bare_background_thread_starts_without_foreground_authority():
    bind_current_turn_user_authority(_authority())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        observed = executor.submit(get_current_turn_user_authority).result(timeout=5)
    assert observed is None


def test_user_action_fingerprint_is_stable_raw_free_and_message_sensitive():
    first = fingerprint_user_action("  Create   blocked card ")
    assert first == fingerprint_user_action("create blocked card")
    assert first != fingerprint_user_action("create another blocked card")
    assert "blocked" not in first
    assert len(first) == 64


def test_opaque_action_id_never_uses_resource_identifier():
    authority = _authority()
    assert opaque_workflow_action_id(authority) == authority.turn_id
    generated = opaque_workflow_action_id(None)
    assert generated.startswith("opaque-action-")
    assert "t_deadbeef" not in generated


def test_authority_token_can_be_reset_without_leak():
    token = bind_current_turn_user_authority(_authority())
    assert get_current_turn_user_authority() is not None
    reset_current_turn_user_authority(token)
    assert get_current_turn_user_authority() is None


def test_scope_inference_is_action_and_explicit_card_target_specific():
    classes, targets = infer_explicit_workflow_scope(
        "t_deadbeef 카드에 검증 결과 댓글 기록"
    )
    assert classes == frozenset({"status_memory"})
    assert targets == frozenset({fingerprint_workflow_target("t_deadbeef")})

    unrelated_classes, unrelated_targets = infer_explicit_workflow_scope(
        "테스트 결과만 읽어서 알려줘"
    )
    assert unrelated_classes == frozenset()
    assert unrelated_targets == frozenset()


def test_authority_requires_matching_class_and_target():
    authority = CurrentTurnUserAuthority(
        turn_id="opaque-scoped-turn",
        source_role="user",
        session_scope="test",
        platform_scope="synthetic",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("comment t_deadbeef"),
        allowed_action_classes=frozenset({"status_memory"}),
        target_fingerprints=frozenset(
            {fingerprint_workflow_target("t_deadbeef")}
        ),
    )
    assert authority.allows("status_memory", "t_deadbeef") is True
    assert authority.allows("status_memory", "t_other123") is False
    assert authority.allows("explicit_blocked_card_create") is False


def test_blocked_create_target_selection_is_rephrase_stable_and_multi_target_specific():
    targets = infer_explicit_blocked_create_targets(
        '\"Approval hardening\" 카드와 \"Compression incident\" 카드를 만들어줘'
    )
    assert targets == frozenset(
        {
            fingerprint_workflow_target("Approval hardening"),
            fingerprint_workflow_target("Compression incident"),
        }
    )
    authority = CurrentTurnUserAuthority(
        turn_id="opaque-multi-create-turn",
        source_role="user",
        session_scope="test",
        platform_scope="synthetic",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("create two requested cards"),
        allowed_action_classes=frozenset({"explicit_blocked_card_create"}),
        blocked_create_target_fingerprints=targets,
    )
    first = select_blocked_create_target_fingerprint(authority, "Approval hardening")
    second = select_blocked_create_target_fingerprint(authority, "Compression incident")
    assert first != second
    with pytest.raises(ValueError, match="requested blocked-card target"):
        select_blocked_create_target_fingerprint(authority, "Assistant invented target")

    single = CurrentTurnUserAuthority(
        turn_id="opaque-single-create-turn",
        source_role="user",
        session_scope="test",
        platform_scope="synthetic",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("create one requested card"),
        allowed_action_classes=frozenset({"explicit_blocked_card_create"}),
        blocked_create_target_fingerprints=frozenset({first}),
    )
    assert select_blocked_create_target_fingerprint(single, "Assistant rephrased title") == first
