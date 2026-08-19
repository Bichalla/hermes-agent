"""Conversation-host binding and revocation for workflow authority."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.conversation_loop as conversation_loop
from gateway.session_context import (
    clear_session_vars,
    get_session_controller_role,
    get_trusted_current_user_text,
    set_session_vars,
)
from tools.workflow_authority import (
    _bind_host_current_turn_user_authority,
    get_current_turn_user_authority,
)


def test_foreground_conversation_turn_binds_host_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.delegation_context.is_delegated_child_context",
        lambda: False,
    )
    tokens = set_session_vars(platform="discord", session_id="session-a")
    try:
        authority = conversation_loop._bind_workflow_authority_for_turn(
            SimpleNamespace(session_id="session-a", platform="discord"),
            original_user_message="Record the current intake",
            turn_id="turn-a",
            current_turn_user_idx=3,
            persist_user_display_kind=None,
        )
        assert authority is get_current_turn_user_authority()
        assert authority.turn_id == "turn-a"
        assert authority.user_message_index == 3
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("display_kind", ("internal_notification", "auto_continue"))
def test_synthetic_conversation_turn_does_not_bind_authority(
    monkeypatch: pytest.MonkeyPatch,
    display_kind: str,
) -> None:
    monkeypatch.setattr(
        "agent.delegation_context.is_delegated_child_context",
        lambda: False,
    )
    tokens = set_session_vars(platform="discord", session_id="session-a")
    try:
        assert conversation_loop._bind_workflow_authority_for_turn(
            SimpleNamespace(session_id="session-a", platform="discord"),
            original_user_message="synthetic system notice",
            turn_id="turn-a",
            current_turn_user_idx=0,
            persist_user_display_kind=display_kind,
        ) is None
        assert get_current_turn_user_authority() is None
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize(
    "agent_fields",
    (
        {"_skip_mcp_refresh": True},
        {"_memory_write_origin": "background_review"},
        {"session_id": "bg_123456"},
        {"session_id": "preview_123456"},
    ),
)
def test_internal_agent_turn_does_not_bind_authority(
    monkeypatch: pytest.MonkeyPatch,
    agent_fields: dict[str, object],
) -> None:
    monkeypatch.setattr(
        "agent.delegation_context.is_delegated_child_context",
        lambda: False,
    )
    values: dict[str, object] = {"session_id": "session-a", "platform": "tui"}
    values.update(agent_fields)
    agent = SimpleNamespace(**values)
    assert conversation_loop._bind_workflow_authority_for_turn(
        agent,
        original_user_message="internal generated prompt",
        turn_id="turn-internal",
        current_turn_user_idx=0,
        persist_user_display_kind=None,
    ) is None
    assert get_current_turn_user_authority() is None


def test_delegated_child_turn_does_not_bind_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.delegation_context.is_delegated_child_context",
        lambda: True,
    )
    assert conversation_loop._bind_workflow_authority_for_turn(
        SimpleNamespace(session_id="session-a", platform="discord"),
        original_user_message="delegated prompt",
        turn_id="turn-delegated",
        current_turn_user_idx=0,
        persist_user_display_kind=None,
    ) is None
    assert get_current_turn_user_authority() is None


def test_run_conversation_revokes_authority_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_after_binding(*_args, **_kwargs):
        _bind_host_current_turn_user_authority(
            "private foreground text",
            turn_id="turn-a",
            session_scope="session-a",
            platform_scope="manual",
            user_message_index=0,
        )
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(conversation_loop, "_run_conversation_inner", fail_after_binding)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        conversation_loop.run_conversation(SimpleNamespace(), "hello")
    assert get_current_turn_user_authority() is None


def test_run_conversation_revokes_authority_and_raw_text_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def succeed_after_binding(*_args, **_kwargs):
        _bind_host_current_turn_user_authority(
            "private foreground text",
            turn_id="turn-success",
            session_scope="session-success",
            platform_scope="manual",
            user_message_index=0,
        )
        return {"final_response": "done"}

    monkeypatch.setattr(conversation_loop, "_run_conversation_inner", succeed_after_binding)
    assert conversation_loop.run_conversation(SimpleNamespace(), "hello") == {
        "final_response": "done"
    }
    assert get_current_turn_user_authority() is None
    assert get_trusted_current_user_text() is None
    assert get_session_controller_role() == ""
