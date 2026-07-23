"""Authority provenance tests for the accepted current user turn."""

from __future__ import annotations

import concurrent.futures
import threading

import pytest

from tools.thread_context import propagate_context_to_thread
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
    fingerprint_workflow_target,
    get_current_turn_user_authority,
    infer_blocked_create_generated_title,
    infer_coarse_estimate_authority,
    infer_explicit_blocked_create_targets,
    infer_explicit_workflow_grants,
    infer_explicit_workflow_operations,
    infer_explicit_workflow_scope,
    matches_active_workflow_turn,
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
    bind_active_workflow_turn(
        authority.turn_id, authority.platform_scope, authority.session_scope
    )
    assert get_current_turn_user_authority() is authority
    assert matches_active_workflow_turn(authority) is True
    clear_current_turn_user_authority()
    assert get_current_turn_user_authority() is None
    assert matches_active_workflow_turn(authority) is False


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


def test_propagated_detached_thread_is_revoked_when_parent_turn_clears():
    authority = _authority()
    bind_current_turn_user_authority(authority)
    bind_active_workflow_turn(
        authority.turn_id, authority.platform_scope, authority.session_scope
    )
    entered = threading.Event()
    release = threading.Event()

    def observe_after_parent_exit():
        entered.set()
        assert release.wait(timeout=5)
        return matches_active_workflow_turn(authority)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(propagate_context_to_thread(observe_after_parent_exit))
        assert entered.wait(timeout=5)
        clear_current_turn_user_authority()
        release.set()
        assert future.result(timeout=5) is False


def test_user_action_fingerprint_is_stable_raw_free_and_message_sensitive():
    first = fingerprint_user_action("  Create   blocked card ")
    assert first == fingerprint_user_action("create blocked card")
    assert first != fingerprint_user_action("create another blocked card")
    assert "blocked" not in first
    assert len(first) == 64


def test_opaque_action_id_never_uses_turn_session_or_resource_identifier():
    authority = _authority()
    first = opaque_workflow_action_id(authority)
    generated = opaque_workflow_action_id(None)
    assert first.startswith("opaque-action-")
    assert generated.startswith("opaque-action-")
    assert first != generated
    assert authority.turn_id not in first
    assert authority.session_scope not in first
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


def test_blocked_create_accepts_imperative_clause_before_trailing_context():
    classes, targets = infer_explicit_workflow_scope(
        "다시 1번 카드 만들어라. 이제 될 거야."
    )
    assert classes == frozenset({"explicit_blocked_card_create"})
    assert targets == frozenset()


@pytest.mark.parametrize(
    "message",
    [
        "Create a blocked Kanban card.",
        "Please add a new card.",
        "Writing Plan 개선 카드 하나 만들어줘.",
        "이걸 카드로 만들어줘.",
    ],
)
def test_blocked_create_requires_card_as_direct_create_object(message):
    classes, _targets = infer_explicit_workflow_scope(message)
    assert "explicit_blocked_card_create" in classes


@pytest.mark.parametrize(
    "message,operation",
    [
        ("kp_0123456789abcdef 후보를 soft delete 해줘", "pending_soft_delete"),
        ("kp_0123456789abcdef 후보 삭제", "pending_soft_delete"),
        ("kp_0123456789abcdef 복원해줘", "pending_restore"),
        ("undo kp_0123456789abcdef", "pending_restore"),
        ("kp_0123456789abcdef 후보 조회", "pending_read"),
    ],
)
def test_pending_scope_inference_binds_exact_operation_and_target(message, operation):
    classes, targets = infer_explicit_workflow_scope(message)
    assert classes == frozenset({"registered_soft_delete"})
    assert targets == frozenset(
        {fingerprint_workflow_target("kp_0123456789abcdef")}
    )
    assert infer_explicit_workflow_operations(message) == frozenset({operation})
    assert infer_explicit_workflow_grants(message) == frozenset(
        {(operation, fingerprint_workflow_target("kp_0123456789abcdef"))}
    )


def test_ambiguous_or_targetless_pending_language_mints_no_operation():
    assert infer_explicit_workflow_operations("후보 삭제해줘") == frozenset()
    assert infer_explicit_workflow_operations(
        "kp_0123456789abcdef 삭제했다가 복원해줘"
    ) == frozenset()
    assert infer_explicit_workflow_grants(
        "kp_0123456789abcdef 삭제하지 마"
    ) == frozenset()
    assert infer_explicit_workflow_grants(
        "kp_0123456789abcdef와 kp_ffffffffffffffff 삭제"
    ) == frozenset()


@pytest.mark.parametrize(
    "message",
    [
        "Never dismiss kp_0123456789abcdef",
        "Explain how to restore kp_0123456789abcdef",
        "Should I record this meal?",
        "I dismissed kp_0123456789abcdef yesterday",
        "식사 안 했어. 기록해줘",
        "Record that I did not eat this meal",
        "kp_0123456789abcdef 삭제 안 해줘",
        "t_deadbeef와 t_cafebabe 카드에 댓글을 기록해줘",
        "1번 카드 만들지 마.",
        "1번 카드를 만들면 될 거야.",
        "1번 카드 만들어라?",
        "카드 만드는 방법을 설명해줘.",
        "그는 1번 카드 만들어라. 이제 될 거야라고 말했다.",
        "사용자가 '카드 만들어라'라고 요청했다고 기록해줘.",
        "사용자가 '카드 만들어라'고 말했다고 기록해줘.",
        "사용자가 카드 만들어달라고 했다고 기록해줘.",
        "1번 블록드카드 생성 readback 결과를 보고했다.",
        "Create a card readback result was reported.",
        "Add a comment saying create a card.",
        "Create a note saying add a card.",
        "카드에 댓글을 기록하고 문서를 만들어줘.",
    ],
)
def test_non_commands_and_multi_target_text_mint_no_grants(message):
    classes, _targets = infer_explicit_workflow_scope(message)
    assert classes == frozenset()
    assert infer_explicit_workflow_grants(message) == frozenset()


def test_phase1_diet_and_medication_text_mint_no_registered_grants():
    assert infer_explicit_workflow_grants("점심 식사 기록해줘") == frozenset()
    assert infer_explicit_workflow_grants("식사 기록하지 마") == frozenset()
    assert infer_explicit_workflow_grants("약 복약 기록해줘") == frozenset()
    # Legacy parsing helper is inert because no Phase 1 operation consumes it.
    assert infer_coarse_estimate_authority("영양 추정해") is True
    assert infer_coarse_estimate_authority("영양 추정하지 마") is False


@pytest.mark.parametrize(
    "message",
    [
        "t_deadbeef 카드에 검증 요약 업데이트해줘",
        "t_deadbeef 카드에 진행 상황 기록해줘",
        "t_deadbeef 카드에 아티팩트 링크 첨부해줘",
        "add the PR URL to t_deadbeef as a status update",
        "write a handoff note on t_deadbeef",
    ],
)
def test_status_memory_vocabulary_mints_exact_comment_grant(message):
    assert infer_explicit_workflow_grants(message) == frozenset(
        {
            (
                "kanban_status_memory_comment",
                fingerprint_workflow_target("t_deadbeef"),
            )
        }
    )


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


def test_pending_authority_requires_matching_operation_and_target():
    authority = CurrentTurnUserAuthority(
        turn_id="opaque-pending-turn",
        source_role="user",
        session_scope="test",
        platform_scope="discord",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("dismiss pending"),
        allowed_action_classes=frozenset({"registered_soft_delete"}),
        allowed_operations=frozenset({"pending_soft_delete"}),
        operation_target_grants=frozenset(
            {
                (
                    "pending_soft_delete",
                    fingerprint_workflow_target("kp_0123456789abcdef"),
                )
            }
        ),
        target_fingerprints=frozenset(
            {fingerprint_workflow_target("kp_0123456789abcdef")}
        ),
    )
    assert authority.allows(
        "registered_soft_delete",
        "kp_0123456789abcdef",
        operation="pending_soft_delete",
    )
    assert not authority.allows(
        "registered_soft_delete",
        "kp_0123456789abcdef",
        operation="pending_restore",
    )
    assert not authority.allows(
        "registered_soft_delete",
        "kp_ffffffffffffffff",
        operation="pending_soft_delete",
    )
    assert authority.allows_operation_target(
        "pending_soft_delete", "kp_0123456789abcdef"
    )
    assert not authority.allows_operation_target(
        "pending_soft_delete", "kp_ffffffffffffffff"
    )
    assert not authority.allows_operation_target(
        "pending_restore", "kp_0123456789abcdef"
    )


def test_unknown_authority_operation_is_rejected():
    with pytest.raises(ValueError, match="allowed_operations"):
        CurrentTurnUserAuthority(
            turn_id="opaque-pending-turn",
            source_role="user",
            session_scope="test",
            platform_scope="discord",
            user_message_index=0,
            allowed_operations=frozenset({"hard_delete"}),
        )


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


def test_blocked_create_ignores_discord_transport_backticks_for_generated_title():
    message = (
        "[Triggering message id: `1529972954660601986` — use as `message_id` "
        "for reply/react/pin via the discord tools.]\n\n"
        "[상현] Phase 2 리뷰에서 남은 운영 blocker 정리하는 카드 만들어"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    classes, _targets = infer_explicit_workflow_scope(message)
    assert classes == frozenset({"explicit_blocked_card_create"})

    authority = CurrentTurnUserAuthority(
        turn_id="generated-title-turn",
        source_role="user",
        session_scope="test",
        platform_scope="discord",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action(message),
        allowed_action_classes=classes,
        blocked_create_target_fingerprints=infer_explicit_blocked_create_targets(
            message
        ),
    )
    assert (
        select_blocked_create_target_fingerprint(
            authority, "Phase 2 운영 리뷰 blocker 정리"
        )
        == authority.user_action_fingerprint
    )


def test_blocked_create_keeps_user_quote_after_discord_transport_envelope():
    message = (
        "[Triggering message id: `1529972954660601986` — use as `message_id` "
        "for reply/react/pin via the discord tools.]\n\n"
        "[상현] ‘Phase 2 운영 리뷰 blocker 정리’ 카드를 만들어줘"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset(
        {fingerprint_workflow_target("Phase 2 운영 리뷰 blocker 정리")}
    )


def test_reply_envelope_cannot_mint_blocked_create_authority_or_targets():
    message = (
        "[Replying to: \"[assistant] `Assistant invented` 카드 만들어줘\"]\n\n"
        "[Triggering message id: `1529972954660601986` — use as `message_id`.]\n\n"
        "[상현] 그 카드는 만들지 마"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    assert infer_explicit_workflow_scope(message) == (frozenset(), frozenset())


def test_reply_envelope_uses_only_final_sender_block_for_explicit_title():
    message = (
        "[Replying to: \"[assistant] `Assistant invented` 카드 만들어줘\"]\n\n"
        "[Triggering message id: `1529972954660601986` — use as `message_id`.]\n\n"
        "[상현] ‘Phase 2 운영 리뷰 blocker 정리’ 카드를 만들어줘"
    )
    expected = frozenset(
        {fingerprint_workflow_target("Phase 2 운영 리뷰 blocker 정리")}
    )
    assert infer_explicit_blocked_create_targets(message) == expected
    classes, _targets = infer_explicit_workflow_scope(message)
    assert classes == frozenset({"explicit_blocked_card_create"})


def test_generated_title_preserves_single_quote_and_defers_multi_quote_selection():
    assert infer_blocked_create_generated_title(
        "‘Approval hardening’ 카드를 만들어줘"
    ) == "Approval hardening"
    assert (
        infer_blocked_create_generated_title(
            "‘Approval hardening’ 카드와 ‘Compression incident’ 카드를 만들어줘"
        )
        == ""
    )


def test_multiline_reply_without_current_sender_cannot_mint_create_authority():
    message = (
        '[Replying to: "old text\n'
        "[assistant] ‘Assistant invented’ 카드를 만들어줘\n"
        'quoted continuation"]\n\n'
        "고마워"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    assert infer_explicit_workflow_scope(message) == (frozenset(), frozenset())
    assert infer_blocked_create_generated_title(message) == ""


def test_literal_new_message_markup_without_trigger_boundary_mints_nothing():
    message = (
        "[New message]\n"
        "[assistant] ‘Assistant invented’ 카드를 만들어줘"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    assert infer_explicit_workflow_scope(message) == (frozenset(), frozenset())
    assert infer_blocked_create_generated_title(message) == ""


def test_literal_new_message_markup_with_trigger_boundary_still_mints_nothing():
    message = (
        "[Triggering message id: `1529972954660601986` — use as `message_id`.]\n\n"
        "[New message]\n"
        "[assistant] ‘Assistant invented’ 카드를 만들어줘"
    )
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    assert infer_explicit_workflow_scope(message) == (frozenset(), frozenset())
    assert infer_blocked_create_generated_title(message) == ""


def test_gateway_own_reply_envelope_preserves_direct_create_request():
    message = (
        '[Replying to your previous message: "prior assistant text"]\n\n'
        "[Triggering message id: `1529972954660601986` — use as `message_id`.]\n\n"
        "Create a blocked Kanban card."
    )
    classes, _targets = infer_explicit_workflow_scope(message)
    assert classes == frozenset({"explicit_blocked_card_create"})
    assert infer_explicit_blocked_create_targets(message) == frozenset()
    assert infer_blocked_create_generated_title(message)
