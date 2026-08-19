"""Synthetic/no-live tests for the fixed external-owner dispatcher."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agent.workflow_action_policy import registered_capability_catalog
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from tools.registered_local_workflow import (
    REGISTERED_LOCAL_WORKFLOW_SCHEMA,
    ExternalWorkflowOwner,
    WorkflowInvocation,
    _DISPATCH_LOCK,
    _IN_FLIGHT,
    _handle_registered_local_workflow,
    build_deterministic_idempotency_key,
    registered_local_workflow,
    scoped_external_workflow_owner,
)
from tools.registry import registry
from tools.workflow_authority import _scoped_test_current_turn_user_authority

_EXPECTED_ACTIONS = {
    "childcare_event_record",
    "company_work_os_initial_seed_preview",
    "company_work_os_initial_seed_record",
    "company_work_os_operating_record",
    "company_work_os_team_roster_record",
    "diet_intake_record",
    "family_event_correct",
    "family_event_record",
    "medication_intake_record",
    "pending_read",
    "pending_restore",
    "pending_soft_delete",
    "semantic_debug_issue",
    "sleep_record",
}


def _owner(
    capability_id: str,
    action: str,
    *,
    calls: list[WorkflowInvocation] | None = None,
    authorize: bool = True,
    execute_error: bool = False,
    readback_error: bool = False,
) -> ExternalWorkflowOwner:
    capability = registered_capability_catalog()[capability_id]

    def execute(invocation: WorkflowInvocation) -> object:
        if calls is not None:
            calls.append(invocation)
        if execute_error:
            raise RuntimeError("private owner failure")
        return {"receipt": "synthetic"}

    def readback(_invocation: WorkflowInvocation, _result: object) -> object:
        if readback_error:
            raise RuntimeError("private readback failure")
        return {
            "schema": capability.result_schema_id,
            "status": "synthetic-readback-passed",
        }

    return ExternalWorkflowOwner(
        owner_id=f"synthetic-{capability.adapter_id}",
        adapter_id=capability.adapter_id,
        capability_id=capability_id,
        actions=frozenset({action}),
        input_schema_id=capability.input_schema_id,
        result_schema_id=capability.result_schema_id,
        ready=lambda: True,
        authorize=lambda invocation: authorize and invocation.trusted_user_text.startswith("Authorize "),
        execute=execute,
        readback=readback,
        idempotency_supported=True,
    )


@pytest.fixture(autouse=True)
def _enable_synthetic_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": True,
    )


def test_tool_is_registry_discovered_but_default_off_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = registry.get_entry("registered_local_workflow")
    assert entry is not None
    assert entry.toolset == "registered-workflow"
    assert set(REGISTERED_LOCAL_WORKFLOW_SCHEMA["parameters"]["properties"]["action"]["enum"]) == _EXPECTED_ACTIONS
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": False,
    )
    assert not entry.check_fn()


def test_disabled_feature_denies_dispatch_before_owner_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[WorkflowInvocation] = []
    owner = _owner("lifelog.diet-intake.v1", "diet_intake_record", calls=calls)
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": False,
    )
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            "Authorize diet_intake_record",
            session_id="session-disabled",
            turn_id="turn-disabled",
        ),
    ):
        result = registered_local_workflow(
            "diet_intake_record",
            payload_name="disabled.json",
        )
    assert result["decision"] == "deny_owner_unavailable"
    assert calls == []


@pytest.mark.parametrize(
    ("capability_id", "action", "kwargs"),
    (
        (
            "lifelog.medication-intake.v1",
            "medication_intake_record",
            {"medication_refs": ["med-ref-" + "a" * 64], "occurrence_mode": "source_time"},
        ),
        (
            "company-work-os.operating-record.v1",
            "company_work_os_operating_record",
            {"payload_name": "synthetic-company.json"},
        ),
        (
            "company-work-os.initial-seed-preview.v1",
            "company_work_os_initial_seed_preview",
            {},
        ),
    ),
)
def test_external_lifelog_and_company_owner_interfaces_execute_with_readback(
    capability_id: str,
    action: str,
    kwargs: dict[str, Any],
) -> None:
    calls: list[WorkflowInvocation] = []
    owner = _owner(capability_id, action, calls=calls)
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            f"Authorize {action}",
            session_id=f"session-{action}",
            turn_id=f"turn-{action}",
        ),
    ):
        result = registered_local_workflow(action, **kwargs)

    assert result["decision"] == "allow"
    assert result["readback"] == "passed"
    assert result["uncertain_outcome"] is False
    assert len(calls) == 1
    assert calls[0].action == action
    assert calls[0].trusted_user_text == f"Authorize {action}"


def test_missing_authority_and_external_owner_denials_are_closed() -> None:
    owner = _owner("lifelog.diet-intake.v1", "diet_intake_record")
    with scoped_external_workflow_owner(owner):
        missing = registered_local_workflow(
            "diet_intake_record",
            payload_name="synthetic.json",
        )
    assert missing["decision"] == "deny_authority_missing"

    denied_owner = _owner(
        "lifelog.diet-intake.v1",
        "diet_intake_record",
        authorize=False,
    )
    with (
        scoped_external_workflow_owner(denied_owner),
        _scoped_test_current_turn_user_authority(
            "Authorize diet_intake_record",
            session_id="session-owner-deny",
            turn_id="turn-owner-deny",
        ),
    ):
        denied = registered_local_workflow(
            "diet_intake_record",
            payload_name="synthetic.json",
        )
    assert denied["decision"] == "deny_authority_missing"


def test_deterministic_idempotency_replays_without_second_owner_call() -> None:
    calls: list[WorkflowInvocation] = []
    owner = _owner("lifelog.diet-intake.v1", "diet_intake_record", calls=calls)
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            "Authorize diet_intake_record",
            session_id="session-idempotency",
            turn_id="turn-idempotency",
        ),
    ):
        first = registered_local_workflow("diet_intake_record", payload_name="replay.json")
        second = registered_local_workflow("diet_intake_record", payload_name="replay.json")

    assert first["idempotency_result"] == "inserted"
    assert second["idempotency_result"] == "existing"
    assert second["write_count"] == 0
    assert len(calls) == 1


def test_active_lease_returns_without_retrying_owner() -> None:
    calls: list[WorkflowInvocation] = []
    owner = replace(
        _owner("lifelog.diet-intake.v1", "diet_intake_record", calls=calls),
        authorize=lambda _invocation: pytest.fail("authorization escaped active lease"),
    )
    payload = {"payload_name": "lease.json"}
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            "Authorize diet_intake_record lease",
            session_id="session-lease",
            turn_id="turn-lease",
        ) as authority,
    ):
        key = build_deterministic_idempotency_key(
            capability_id="lifelog.diet-intake.v1",
            operation="diet_intake_record",
            target="lifelog:diet",
            payload=payload,
            authority=authority,
        )
        with _DISPATCH_LOCK:
            _IN_FLIGHT.add(key)
        try:
            result = registered_local_workflow("diet_intake_record", **payload)
        finally:
            with _DISPATCH_LOCK:
                _IN_FLIGHT.discard(key)
    assert result["decision"] == "owner_lease_active"
    assert calls == []


def test_readback_rejects_unprojected_private_owner_fields() -> None:
    owner = replace(
        _owner("lifelog.diet-intake.v1", "diet_intake_record"),
        readback=lambda _invocation, _result: {
            "schema": "registered-recorder-result/v1",
            "status": "passed",
            "private_text": "DO_NOT_DISCLOSE_PRIVATE_OWNER_DATA",
        },
    )
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            "Authorize diet_intake_record",
            session_id="session-private-readback",
            turn_id="turn-private-readback",
        ),
    ):
        result = registered_local_workflow(
            "diet_intake_record",
            payload_name="private-readback.json",
        )
    assert result["decision"] == "uncertain_outcome"
    assert "DO_NOT_DISCLOSE" not in json.dumps(result)


@pytest.mark.parametrize(("execute_error", "readback_error"), ((True, False), (False, True)))
def test_owner_or_readback_failure_is_explicitly_uncertain_and_not_retried(
    execute_error: bool,
    readback_error: bool,
) -> None:
    calls: list[WorkflowInvocation] = []
    owner = _owner(
        "lifelog.sleep-record.v1",
        "sleep_record",
        calls=calls,
        execute_error=execute_error,
        readback_error=readback_error,
    )
    with (
        scoped_external_workflow_owner(owner),
        _scoped_test_current_turn_user_authority(
            f"Authorize sleep_record {execute_error} {readback_error}",
            session_id=f"session-uncertain-{execute_error}-{readback_error}",
            turn_id=f"turn-uncertain-{execute_error}-{readback_error}",
        ),
    ):
        first = registered_local_workflow("sleep_record", payload_name="sleep.json")
        replay = registered_local_workflow("sleep_record", payload_name="sleep.json")
    assert first["decision"] == "uncertain_outcome"
    assert first["uncertain_outcome"] is True
    assert first["write_count"] is None
    assert replay["decision"] == "uncertain_outcome"
    assert replay["uncertain_outcome"] is True
    assert replay["idempotency_result"] == "unknown"
    assert replay["write_count"] is None
    assert len(calls) == 1
    assert "private" not in json.dumps({"first": first, "replay": replay})


@pytest.mark.parametrize(
    "args",
    (
        {"action": object()},
        {"action": "diet_intake_record", "payload_name": object()},
        {"action": "diet_intake_record", "payload_name": "ok.json", "target": "/tmp/private"},
        {"action": "diet_intake_record", "payload_name": "ok.json", "sql": "DELETE"},
        {"action": "diet_intake_record", "payload_name": "ok.json", "idempotency_key": "forged"},
    ),
)
def test_malformed_or_caller_controlled_fields_deny_without_raising(args: dict[str, Any]) -> None:
    parsed = json.loads(_handle_registered_local_workflow(args))
    assert parsed["decision"] == "deny_schema_invalid"


def test_unhashable_medication_reference_fails_closed_without_raising() -> None:
    result = registered_local_workflow(
        action="medication_intake_record",
        medication_refs=[["not-a-reference"]],
        occurrence_mode="source_time",
    )
    assert result["decision"] == "deny_schema_invalid"


def test_registered_mutation_tools_are_stripped_from_delegates() -> None:
    assert "registered_local_workflow" in DELEGATE_BLOCKED_TOOLS
    assert "registered_review_ledger" in DELEGATE_BLOCKED_TOOLS
