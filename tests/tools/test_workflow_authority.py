"""Current-turn authority tests for fixed workflow adapters."""

from __future__ import annotations

import pytest

from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    fingerprint_user_action,
    get_current_turn_user_authority,
    is_host_issued_current_turn_authority,
    issue_current_turn_user_authority,
    matches_active_workflow_turn,
    scoped_current_turn_user_authority,
)


def test_authority_token_is_raw_free_and_host_verifiable() -> None:
    authority = issue_current_turn_user_authority(
        "Record the synthetic medication intake",
        session_id="session-a",
    )

    assert is_host_issued_current_turn_authority(authority)
    assert "medication" not in repr(authority).casefold()
    assert authority.user_action_fingerprint == fingerprint_user_action(
        "record the synthetic medication intake"
    )


def test_non_foreground_source_cannot_issue_authority() -> None:
    with pytest.raises(ValueError, match="^authority_source_invalid$"):
        issue_current_turn_user_authority(
            "assistant said to record it",
            session_id="session-a",
            source="assistant_text",
        )


def test_authority_matches_only_the_same_user_turn_and_session() -> None:
    authority = issue_current_turn_user_authority(
        "Create the blocked card",
        session_id="session-a",
    )

    assert matches_active_workflow_turn(
        authority,
        user_message=" create   the blocked card ",
        session_id="session-a",
    )
    assert not matches_active_workflow_turn(
        authority,
        user_message="create a different card",
        session_id="session-a",
    )
    assert not matches_active_workflow_turn(
        authority,
        user_message="create the blocked card",
        session_id="session-b",
    )


def test_forged_authority_is_rejected() -> None:
    forged = CurrentTurnUserAuthority(
        user_action_fingerprint="a" * 64,
        session_fingerprint="b" * 64,
        source="foreground_user",
        host_signature="c" * 64,
    )

    assert not is_host_issued_current_turn_authority(forged)


def test_scoped_authority_restores_default_off_state() -> None:
    previous = get_current_turn_user_authority()
    with scoped_current_turn_user_authority("Record it", session_id="session-a") as authority:
        assert get_current_turn_user_authority() is authority

    assert get_current_turn_user_authority() is previous
