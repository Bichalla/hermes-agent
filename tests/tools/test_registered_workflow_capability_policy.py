"""Executable decision matrix for registered bounded-autonomy capabilities."""

from __future__ import annotations

import pytest

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    IdempotencyMode,
    RegisteredCapability,
    WorkflowEffect,
    evaluate_registered_capability,
    registered_capability_catalog,
    validate_registered_capability,
)


def test_initial_catalog_is_closed_and_versioned():
    catalog = registered_capability_catalog()
    assert set(catalog) == {
        "kanban.status-memory.v1",
        "kanban-intake.pending-soft-delete.v1",
    }
    assert catalog["kanban-intake.pending-soft-delete.v1"].effects == frozenset(
        {WorkflowEffect.READ, WorkflowEffect.SOFT_DELETE, WorkflowEffect.RESTORE}
    )
    for capability in catalog.values():
        validate_registered_capability(capability)
        assert "/" not in capability.adapter_id
        assert ".py" not in capability.adapter_id


@pytest.mark.parametrize(
    "operation,effect",
    [
        ("pending_read", WorkflowEffect.READ),
        ("pending_soft_delete", WorkflowEffect.SOFT_DELETE),
        ("pending_restore", WorkflowEffect.RESTORE),
    ],
)
def test_registered_operation_effect_pairs_allow(operation, effect):
    assert evaluate_registered_capability(
        "kanban-intake.pending-soft-delete.v1",
        operation,
        effect,
        schema_valid=True,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        owner_ready=True,
        target_valid=True,
        restore_contract_valid=True,
    ) is CapabilityDecision.ALLOW


def test_status_memory_uses_wired_operation_name_and_exact_authority_modes():
    assert evaluate_registered_capability(
        "kanban.status-memory.v1",
        "kanban_status_memory_comment",
        WorkflowEffect.CREATE,
        schema_valid=True,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.ALLOW
    assert evaluate_registered_capability(
        "kanban.status-memory.v1",
        "kanban_status_memory_comment",
        WorkflowEffect.CREATE,
        schema_valid=True,
        authority_mode=AuthorityMode.LOCAL_READ_BOUNDARY,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.DENY_AUTHORITY_MISSING


@pytest.mark.parametrize("operation", ["pending_soft_delete", "pending_restore"])
def test_local_read_authority_cannot_mutate_pending(operation):
    effect = (
        WorkflowEffect.SOFT_DELETE
        if operation == "pending_soft_delete"
        else WorkflowEffect.RESTORE
    )
    assert evaluate_registered_capability(
        "kanban-intake.pending-soft-delete.v1",
        operation,
        effect,
        schema_valid=True,
        authority_mode=AuthorityMode.LOCAL_READ_BOUNDARY,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.DENY_AUTHORITY_MISSING


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"represented_live_or_external_boundary": True}, CapabilityDecision.HARD_BLOCK),
        ({"schema_valid": False}, CapabilityDecision.DENY_SCHEMA_INVALID),
        ({"authority_mode": None}, CapabilityDecision.DENY_AUTHORITY_MISSING),
        ({"owner_ready": False}, CapabilityDecision.DENY_OWNER_UNAVAILABLE),
        ({"target_valid": False}, CapabilityDecision.DENY_TARGET_MISMATCH),
        ({"restore_contract_valid": False}, CapabilityDecision.DENY_SOFT_DELETE_NOT_RESTORABLE),
    ],
)
def test_decision_precedence_rows(kwargs, expected):
    defaults = dict(
        schema_valid=True,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        owner_ready=True,
        target_valid=True,
        restore_contract_valid=True,
        represented_live_or_external_boundary=False,
    )
    defaults.update(kwargs)
    assert evaluate_registered_capability(
        "kanban-intake.pending-soft-delete.v1",
        "pending_soft_delete",
        WorkflowEffect.SOFT_DELETE,
        **defaults,
    ) is expected


def test_unknown_or_cross_effect_operation_denies_unregistered():
    assert evaluate_registered_capability(
        "unknown.v1",
        "pending_read",
        WorkflowEffect.READ,
        schema_valid=False,
        authority_mode=None,
        owner_ready=False,
        target_valid=False,
    ) is CapabilityDecision.DENY_UNREGISTERED_ACTION
    assert evaluate_registered_capability(
        "kanban-intake.pending-soft-delete.v1",
        "pending_restore",
        WorkflowEffect.SOFT_DELETE,
        schema_valid=True,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.DENY_UNREGISTERED_ACTION


@pytest.mark.parametrize(
    "capability_id,operation",
    [
        ("lifelog.confirmed-record.v1", "lifelog_diet_confirmed_create"),
        ("review-ledger.history.v1", "start-or-reconcile"),
    ],
)
def test_phase1_excluded_owner_capabilities_deny(capability_id, operation):
    assert evaluate_registered_capability(
        capability_id,
        operation,
        WorkflowEffect.CREATE,
        schema_valid=True,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        owner_ready=True,
        target_valid=True,
    ) is CapabilityDecision.DENY_UNREGISTERED_ACTION


def test_definition_rejects_paths_empty_sets_and_soft_delete_without_restore():
    base = dict(
        capability_id="example.capability.v1",
        effects=frozenset({WorkflowEffect.CREATE}),
        authority_modes=frozenset({AuthorityMode.FOREGROUND_CURRENT_TURN}),
        adapter_id="kanban-status-memory",
        input_schema_id="example-input/v1",
        result_schema_id="example-result/v1",
        idempotency=IdempotencyMode.OWNER_ATOMIC,
        readback_required=True,
        soft_delete_restore_required=False,
    )
    validate_registered_capability(RegisteredCapability(**base))
    with pytest.raises(ValueError, match="adapter"):
        validate_registered_capability(RegisteredCapability(**{**base, "adapter_id": "scripts/x.py"}))
    with pytest.raises(ValueError, match="effects"):
        validate_registered_capability(RegisteredCapability(**{**base, "effects": frozenset()}))
    with pytest.raises(ValueError, match="restore"):
        validate_registered_capability(
            RegisteredCapability(
                **{
                    **base,
                    "effects": frozenset({WorkflowEffect.SOFT_DELETE}),
                    "soft_delete_restore_required": False,
                }
            )
        )


def test_definition_requires_exact_primitives():
    capability = RegisteredCapability(
        capability_id="example.capability.v1",
        effects=frozenset({WorkflowEffect.CREATE}),
        authority_modes=frozenset({AuthorityMode.FOREGROUND_CURRENT_TURN}),
        adapter_id="kanban-status-memory",
        input_schema_id="example-input/v1",
        result_schema_id="example-result/v1",
        idempotency=IdempotencyMode.OWNER_ATOMIC,
        readback_required=1,  # type: ignore[arg-type]
        soft_delete_restore_required=False,
    )
    with pytest.raises(TypeError, match="readback_required"):
        validate_registered_capability(capability)
