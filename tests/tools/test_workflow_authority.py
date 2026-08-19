"""Host-bound current-turn workflow authority tests."""

from __future__ import annotations

import pytest

import tools.workflow_authority as authority_module
from gateway.session_context import (
    get_session_controller_role,
    get_trusted_current_user_text,
)
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
    get_current_turn_user_authority,
    is_host_issued_current_turn_authority,
    matches_active_workflow_turn,
    matches_current_workflow_session,
    _scoped_test_current_turn_user_authority,
)


def test_authority_is_host_bound_raw_free_and_external_plugin_compatible() -> None:
    with _scoped_test_current_turn_user_authority(
        "Record the synthetic medication intake",
        session_id="session-a",
        turn_id="turn-a",
    ) as authority:
        assert is_host_issued_current_turn_authority(authority)
        assert authority.source_role == "user"
        assert authority.turn_id == "turn-a"
        assert authority.session_scope == "session-a"
        assert authority.user_message_index == 0
        assert authority.user_action_fingerprint == fingerprint_user_action(
            "record the synthetic medication intake"
        )
        assert "medication" not in repr(authority).casefold()
        assert matches_active_workflow_turn(authority)
        assert matches_current_workflow_session(authority)
        assert get_trusted_current_user_text() == "Record the synthetic medication intake"
        assert get_session_controller_role() == "main_controller"


def test_no_public_arbitrary_text_issuer_exists() -> None:
    assert not hasattr(authority_module, "issue_current_turn_user_authority")
    assert not hasattr(authority_module, "scoped_current_turn_user_authority")
    assert "_scoped_test_current_turn_user_authority" not in authority_module.__all__


def test_authority_matches_only_same_turn_text_and_session() -> None:
    with _scoped_test_current_turn_user_authority(
        "Create the blocked card",
        session_id="session-a",
        turn_id="turn-a",
    ) as authority:
        assert matches_active_workflow_turn(
            authority,
            user_message=" create   the blocked card ",
            session_id="session-a",
        )
        assert not matches_active_workflow_turn(
            authority,
            user_message="create a different card",
        )
        assert not matches_active_workflow_turn(authority, session_id="session-b")


def test_forged_authority_is_rejected() -> None:
    forged = CurrentTurnUserAuthority(
        turn_id="turn-a",
        source_role="user",
        session_scope="session-a",
        platform_scope="manual",
        user_message_index=0,
        user_action_fingerprint="a" * 64,
        host_signature="b" * 64,
    )
    assert not is_host_issued_current_turn_authority(forged)
    assert not matches_active_workflow_turn(forged)


@pytest.mark.parametrize(
    "surface",
    (
        "api_server",
        "background",
        "cron",
        "delegate",
        "gateway",
        "kanban",
        "local",
        "msgraph_webhook",
        "review",
        "subagent",
        "tool",
        "webhook",
    ),
)
def test_non_foreground_surfaces_cannot_bind(surface: str) -> None:
    with pytest.raises(ValueError, match="^authority_surface_invalid$"):
        authority_module._bind_host_current_turn_user_authority(
            "synthetic user text",
            turn_id="turn-a",
            session_scope="session-a",
            platform_scope=surface,
            user_message_index=0,
        )


def test_scoped_authority_clears_all_private_turn_context() -> None:
    clear_current_turn_user_authority()
    with _scoped_test_current_turn_user_authority("Record it", session_id="session-a"):
        assert get_current_turn_user_authority() is not None
    assert get_current_turn_user_authority() is None
    assert get_trusted_current_user_text() is None
    assert get_session_controller_role() == ""


@pytest.mark.parametrize(
    ("source", "session_id"),
    (("discord", "session-a"), ("tui", "session-b")),
)
def test_authority_rejects_independently_bound_session_mismatch(
    source: str,
    session_id: str,
) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars

    with _scoped_test_current_turn_user_authority(
        "Record it",
        session_id="session-a",
        platform_scope="tui",
    ) as authority:
        tokens = set_session_vars(source=source, session_id=session_id)
        try:
            assert not matches_current_workflow_session(authority)
        finally:
            clear_session_vars(tokens)
