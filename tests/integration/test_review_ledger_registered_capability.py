"""Legacy review-ledger controller action names remain unregistered."""

from __future__ import annotations

import pytest

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    WorkflowEffect,
    evaluate_registered_capability,
    registered_capability_catalog,
)


@pytest.mark.parametrize(
    "action,effect",
    [
        ("start-or-reconcile", WorkflowEffect.CREATE),
        ("record-or-reconcile", WorkflowEffect.UPDATE),
    ],
)
def test_legacy_review_ledger_controller_actions_are_not_registered(action, effect):
    assert "review-ledger.history.v1" in registered_capability_catalog()
    assert evaluate_registered_capability(
        "review-ledger.history.v1",
        action,
        effect,
        schema_valid=True,
        authority_mode=AuthorityMode.MAIN_CONTROLLER,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.DENY_UNREGISTERED_ACTION
