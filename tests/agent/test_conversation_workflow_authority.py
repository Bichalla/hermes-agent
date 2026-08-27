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


def _host_turn_context(user_message: str) -> SimpleNamespace:
    return SimpleNamespace(
        user_message=user_message,
        original_user_message=user_message,
        messages=[{"role": "user", "content": user_message}],
        conversation_history=[],
        active_system_prompt="system",
        effective_task_id="task-a",
        turn_id="turn-a",
        current_turn_user_idx=0,
        should_review_memory=False,
        plugin_user_context=None,
        ext_prefetch_cache=None,
        preflight_compression_blocked=False,
    )


def _host_turn_agent(**overrides) -> SimpleNamespace:
    values = {
        "_background_review_agent": None,
        "_try_refresh_env_client_credentials": lambda: None,
        "_last_compaction_in_place": False,
        "_last_compression_attempt_recorded": False,
        "_last_compression_attempt_in_place": None,
        "session_id": "session-a",
        "platform": "discord",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_host_adapter_runs_once_post_bind_and_returns_before_provider_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    user_message = "AUTHORIZE_HERMES_CHANGE_GATE_CLAIM " + "a" * 64

    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        lambda *_args, **_kwargs: order.append("build")
        or _host_turn_context(user_message),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_bind_workflow_authority_for_turn",
        lambda *_args, **_kwargs: order.append("bind") or object(),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_invoke_current_turn_change_gate_release",
        lambda: order.append("adapter")
        or SimpleNamespace(status="issued", terminal=True),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_finalize_change_gate_host_turn",
        lambda *_args, **_kwargs: order.append("terminal")
        or {"final_response": "issued", "api_calls": 0},
    )

    result = conversation_loop._run_conversation_inner(
        _host_turn_agent(),
        user_message,
    )

    assert result == {"final_response": "issued", "api_calls": 0}
    assert order == ["build", "bind", "adapter", "terminal"]


def test_host_adapter_exception_fails_closed_before_provider_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_message = "AUTHORIZE_HERMES_CHANGE_GATE_G4 " + "b" * 64
    terminal_inputs: list[object] = []

    monkeypatch.setattr(
        conversation_loop,
        "build_turn_context",
        lambda *_args, **_kwargs: _host_turn_context(user_message),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_bind_workflow_authority_for_turn",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_invoke_current_turn_change_gate_release",
        lambda: (_ for _ in ()).throw(RuntimeError("owner unavailable")),
    )

    def _terminal(_agent, *, host_result, **_kwargs):
        terminal_inputs.append(host_result)
        return {"final_response": "failed closed", "api_calls": 0}

    monkeypatch.setattr(
        conversation_loop,
        "_finalize_change_gate_host_turn",
        _terminal,
    )

    result = conversation_loop._run_conversation_inner(
        _host_turn_agent(),
        user_message,
    )

    assert result["api_calls"] == 0
    assert terminal_inputs == [None]


def test_host_terminal_persists_one_bounded_response_without_generic_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[list[dict]] = []
    cleared_sidecars: list[tuple[str, object, str]] = []
    messages = [
        {
            "role": "user",
            "content": "authorization",
            "api_content": "authorization\n\nunsent plugin context",
        }
    ]
    agent = _host_turn_agent(
        _persist_session=lambda current, _history: persisted.append(list(current)),
        _apply_persist_user_message_override=lambda _messages: None,
        _session_db=SimpleNamespace(
            set_latest_user_api_content=lambda *args: cleared_sidecars.append(args)
        ),
        clear_interrupt=lambda: None,
        model="fixture-model",
        provider="fixture-provider",
        base_url="https://fixture.invalid",
        request_overrides={},
        context_compressor=None,
    )
    monkeypatch.setattr(
        conversation_loop,
        "finalize_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic finalizer must remain unreachable")
        ),
    )

    result = conversation_loop._finalize_change_gate_host_turn(
        agent,
        host_result=SimpleNamespace(
            status="zero_candidate",
            task_id=None,
            purpose="CLAIM",
            terminal=True,
        ),
        messages=messages,
        conversation_history=[],
    )

    assert result["api_calls"] == 0
    assert result["turn_exit_reason"] == "change_gate_host_adapter(zero_candidate)"
    assert result["completed"] is True
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "api_content" not in messages[0]
    assert cleared_sidecars == [("session-a", "authorization", "")]
    assert persisted == [messages]
