"""No-live registered local workflow interface tests."""

from __future__ import annotations

import pytest

from agent.workflow_action_policy import AuthorityMode, CapabilityDecision, WorkflowEffect
from tools.registered_local_workflow import (
    ExternalPluginContract,
    SyntheticOwnerInterface,
    WorkflowDispatchRequest,
    build_deterministic_idempotency_key,
    constant_readback_fingerprint,
    evaluate_no_live_workflow_dispatch,
    validate_external_plugin_contract,
)
from tools.workflow_authority import issue_current_turn_user_authority


def _owner(
    capability_id: str,
    operation: str,
    input_schema_id: str,
    result_schema_id: str,
) -> SyntheticOwnerInterface:
    return SyntheticOwnerInterface(
        owner_id="synthetic-owner",
        capability_id=capability_id,
        actions=frozenset({operation}),
        input_schema_id=input_schema_id,
        result_schema_id=result_schema_id,
        readback_supported=True,
        idempotency_supported=True,
    )


def _request(
    *,
    capability_id: str,
    operation: str,
    effect: WorkflowEffect,
    input_schema_id: str,
    result_schema_id: str,
    target: str = "synthetic-target",
    authority_mode: AuthorityMode = AuthorityMode.FOREGROUND_CURRENT_TURN,
) -> WorkflowDispatchRequest:
    authority = issue_current_turn_user_authority(
        f"Run {operation} for {target}",
        session_id="session-a",
    )
    return WorkflowDispatchRequest(
        capability_id=capability_id,
        operation=operation,
        effect=effect,
        input_schema_id=input_schema_id,
        target=target,
        authority=authority,
        owner=_owner(capability_id, operation, input_schema_id, result_schema_id),
        idempotency_key=build_deterministic_idempotency_key(
            capability_id=capability_id,
            operation=operation,
            target=target,
            authority=authority,
        ),
        authority_mode=authority_mode,
    )


def test_lifelog_owner_interface_allows_fixed_record_with_authority() -> None:
    result = evaluate_no_live_workflow_dispatch(
        _request(
            capability_id="lifelog.medication-intake.v1",
            operation="medication_intake_record",
            effect=WorkflowEffect.CREATE,
            input_schema_id="registered-medication-claim/v2",
            result_schema_id="registered-local-workflow-result/v1",
            target="person.synthetic:medication",
        )
    )

    assert result.decision is CapabilityDecision.ALLOW
    assert result.readback_required
    assert result.idempotency_required
    assert not result.uncertain_outcome


def test_company_work_os_owner_interface_allows_fixed_record_with_authority() -> None:
    result = evaluate_no_live_workflow_dispatch(
        _request(
            capability_id="company-work-os.operating-record.v1",
            operation="company_work_os_operating_record",
            effect=WorkflowEffect.CREATE,
            input_schema_id="company-work-os-operating-record/v1",
            result_schema_id="company-work-os-operating-record-result/v1",
            target="company-work-os:synthetic",
        )
    )

    assert result.decision is CapabilityDecision.ALLOW


def test_review_ledger_owner_interface_allows_main_fixed_protocol_shape() -> None:
    result = evaluate_no_live_workflow_dispatch(
        _request(
            capability_id="review-ledger.history.v1",
            operation="record_result",
            effect=WorkflowEffect.UPDATE,
            input_schema_id="review-ledger-controller/v1",
            result_schema_id="review-ledger-controller-result/v1",
            target="review-ledger:synthetic",
            authority_mode=AuthorityMode.MAIN_CONTROLLER,
        )
    )

    assert result.decision is CapabilityDecision.ALLOW


def test_kanban_status_memory_interface_allows_thin_status_memory_contract() -> None:
    result = evaluate_no_live_workflow_dispatch(
        _request(
            capability_id="kanban.status-memory.v1",
            operation="kanban_status_memory_comment",
            effect=WorkflowEffect.CREATE,
            input_schema_id="kanban-status-memory/v1",
            result_schema_id="kanban-status-memory-result/v1",
            target="kanban:task-synthetic",
        )
    )

    assert result.decision is CapabilityDecision.ALLOW


def test_missing_authority_denies_fixed_workflow_dispatch() -> None:
    request = _request(
        capability_id="lifelog.medication-intake.v1",
        operation="medication_intake_record",
        effect=WorkflowEffect.CREATE,
        input_schema_id="registered-medication-claim/v2",
        result_schema_id="registered-local-workflow-result/v1",
    )
    request = WorkflowDispatchRequest(
        capability_id=request.capability_id,
        operation=request.operation,
        effect=request.effect,
        input_schema_id=request.input_schema_id,
        target=request.target,
        authority=None,
        owner=request.owner,
        idempotency_key=request.idempotency_key,
    )

    result = evaluate_no_live_workflow_dispatch(request)

    assert result.decision is CapabilityDecision.DENY_SCHEMA_INVALID
    assert result.uncertain_outcome


def test_model_supplied_unknown_action_is_not_dispatchable() -> None:
    request = _request(
        capability_id="lifelog.medication-intake.v1",
        operation="medication_intake_record",
        effect=WorkflowEffect.CREATE,
        input_schema_id="registered-medication-claim/v2",
        result_schema_id="registered-local-workflow-result/v1",
    )
    request = WorkflowDispatchRequest(
        capability_id=request.capability_id,
        operation="open_arbitrary_path",
        effect=request.effect,
        input_schema_id=request.input_schema_id,
        target=request.target,
        authority=request.authority,
        owner=request.owner,
        idempotency_key=request.idempotency_key,
    )

    result = evaluate_no_live_workflow_dispatch(request)

    assert result.decision is CapabilityDecision.DENY_UNREGISTERED_ACTION


def test_personal_context_plugin_contract_registers_no_live_import_shape() -> None:
    contract = ExternalPluginContract(
        plugin_id="lifelog-context-broker",
        tool_name="lifelog_context_read",
        input_schema_id="lifelog-context-read/v1",
        result_schema_id="lifelog-context-read-result/v1",
        privacy_projection="typed-redacted-context",
    )

    assert validate_external_plugin_contract(contract)


def test_readback_fingerprint_is_deterministic_and_no_network() -> None:
    result = {
        "schema": "lifelog-medication-intake-result/v1",
        "status": "accepted",
        "receipt": "synthetic",
    }

    assert constant_readback_fingerprint(result) == constant_readback_fingerprint(dict(result))
