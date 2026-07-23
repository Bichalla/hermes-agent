"""Whole-turn workflow-authority cleanup contract."""

from __future__ import annotations

import pytest

import agent.conversation_loop as loop
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    get_current_turn_user_authority,
    matches_active_workflow_turn,
)


def _bind() -> CurrentTurnUserAuthority:
    authority = CurrentTurnUserAuthority(
        turn_id="turn-cleanup-test",
        source_role="user",
        session_scope="session-cleanup-test",
        platform_scope="discord",
        user_message_index=0,
    )
    bind_current_turn_user_authority(authority)
    bind_active_workflow_turn(
        authority.turn_id, authority.platform_scope, authority.session_scope
    )
    return authority


@pytest.fixture(autouse=True)
def _clear():
    clear_current_turn_user_authority()
    yield
    clear_current_turn_user_authority()


def test_public_wrapper_clears_authority_after_any_early_return(monkeypatch):
    authority = _bind()
    monkeypatch.setattr(loop, "_run_conversation_inner", lambda *_a, **_k: {"ok": True})

    assert loop.run_conversation(object(), "test") == {"ok": True}
    assert get_current_turn_user_authority() is None
    assert matches_active_workflow_turn(authority) is False


def test_public_wrapper_clears_authority_after_any_exception(monkeypatch):
    authority = _bind()

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic early failure")

    monkeypatch.setattr(loop, "_run_conversation_inner", fail)
    with pytest.raises(RuntimeError, match="synthetic early failure"):
        loop.run_conversation(object(), "test")
    assert get_current_turn_user_authority() is None
    assert matches_active_workflow_turn(authority) is False
