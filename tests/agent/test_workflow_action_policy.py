"""Pure policy tests for registered low-risk workflow actions."""

from __future__ import annotations

import pytest

from agent.workflow_action_policy import (
    WorkflowAction,
    WorkflowActionClass,
    build_blocked_card_idempotency_key,
    classify_workflow_action,
)


def test_existing_card_verification_note_is_status_memory():
    action = WorkflowAction(
        operation="kanban.comment",
        target_exists=True,
        changes_lifecycle=False,
        publishes_external=False,
    )
    assert classify_workflow_action(action) is WorkflowActionClass.STATUS_MEMORY


@pytest.mark.parametrize(
    "operation",
    (
        "kanban.create_ready",
        "kanban.dispatch",
        "kanban.unblock",
        "kanban.complete",
        "kanban.archive",
        "kanban.delete",
        "kanban.change_status",
        "kanban.change_assignee",
        "kanban.change_priority",
    ),
)
def test_lifecycle_and_assignment_actions_require_approval(operation: str):
    action = WorkflowAction(operation=operation, target_exists=True, changes_lifecycle=True)
    assert (
        classify_workflow_action(action)
        is WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
    )


def test_explicit_blocked_card_create_has_narrow_class():
    action = WorkflowAction(
        operation="kanban.create_blocked",
        explicit_blocked_card_create=True,
        deterministic_idempotency_key=True,
    )
    assert (
        classify_workflow_action(action)
        is WorkflowActionClass.EXPLICIT_BLOCKED_CARD_CREATE
    )


def test_blocked_card_without_idempotency_key_fails_closed():
    action = WorkflowAction(
        operation="kanban.create_blocked",
        explicit_blocked_card_create=True,
        deterministic_idempotency_key=False,
    )
    assert (
        classify_workflow_action(action)
        is WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
    )


def test_registered_confirmed_local_record_is_trusted():
    action = WorkflowAction(
        operation="lifelog.record",
        registered_recorder=True,
        confirmed_fact=True,
        exact_target=True,
    )
    assert classify_workflow_action(action) is WorkflowActionClass.TRUSTED_LOCAL_RECORD


def test_blocked_card_idempotency_key_is_stable_and_scope_sensitive():
    first = build_blocked_card_idempotency_key(
        board="Lifelog-Control",
        exact_user_action="  Create   a blocked tracking card  ",
        target_scope="hermes-agent",
    )
    retry = build_blocked_card_idempotency_key(
        board="lifelog-control",
        exact_user_action="create a blocked tracking card",
        target_scope="hermes-agent",
    )
    different_board = build_blocked_card_idempotency_key(
        board="other-board",
        exact_user_action="create a blocked tracking card",
        target_scope="hermes-agent",
    )
    different_action = build_blocked_card_idempotency_key(
        board="lifelog-control",
        exact_user_action="create a different blocked card",
        target_scope="hermes-agent",
    )
    different_target = build_blocked_card_idempotency_key(
        board="lifelog-control",
        exact_user_action="create a blocked tracking card",
        target_scope="other-tenant",
    )

    assert first == retry
    assert first.startswith("blocked-card:v1:")
    assert len({first, different_board, different_action, different_target}) == 4


@pytest.mark.parametrize("confirmed,exact", ((False, True), (True, False), (False, False)))
def test_unconfirmed_or_inexact_record_fails_closed(confirmed: bool, exact: bool):
    action = WorkflowAction(
        operation="lifelog.record",
        registered_recorder=True,
        confirmed_fact=confirmed,
        exact_target=exact,
    )
    assert (
        classify_workflow_action(action)
        is WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
    )


def test_unknown_db_action_requires_approval():
    action = WorkflowAction(operation="db.unknown", unknown_db_action=True)
    assert (
        classify_workflow_action(action)
        is WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
    )


@pytest.mark.parametrize(
    "action",
    (
        WorkflowAction(operation="message.send", publishes_external=True),
        WorkflowAction(operation="credential.read", reads_credentials=True),
        WorkflowAction(operation="file.destroy", destructive=True),
    ),
)
def test_public_credential_and_destructive_actions_are_highest_risk(action):
    assert classify_workflow_action(action) is WorkflowActionClass.DESTRUCTIVE_OR_PUBLIC


def test_read_only_action_is_read_only():
    action = WorkflowAction(operation="kanban.show", read_only=True)
    assert classify_workflow_action(action) is WorkflowActionClass.READ_ONLY


def test_unknown_operation_defaults_to_approval_required():
    assert (
        classify_workflow_action(WorkflowAction(operation="unknown.operation"))
        is WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
    )


def test_model_supplied_authority_is_not_part_of_policy_input():
    with pytest.raises(TypeError):
        WorkflowAction(operation="kanban.comment", latest_user_explicit=True)
