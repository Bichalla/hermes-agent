"""Synthetic fixed-protocol tests for the external review-ledger seam."""

from __future__ import annotations

import json

import pytest

from agent.workflow_action_policy import registered_capability_catalog
from tools.registered_local_workflow import (
    _COMPLETED,
    _DISPATCH_LOCK,
    _IN_FLIGHT,
    ExternalWorkflowOwner,
    WorkflowInvocation,
    scoped_external_workflow_owner,
)
from tools.registered_review_ledger import (
    REGISTERED_REVIEW_LEDGER_SCHEMA,
    _handle_registered_review_ledger,
    check_registered_review_ledger_requirements,
    registered_review_ledger,
)
from tools.registry import registry
from tools.workflow_authority import _scoped_test_current_turn_user_authority

_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_TIMESTAMP = "2026-08-20T00:00:00+00:00"


def _owner(calls: list[WorkflowInvocation]) -> ExternalWorkflowOwner:
    capability = registered_capability_catalog()["review-ledger.history.v1"]

    def execute(invocation: WorkflowInvocation) -> object:
        calls.append(invocation)
        return {"synthetic": invocation.action}

    return ExternalWorkflowOwner(
        owner_id="synthetic-review-ledger-controller",
        adapter_id=capability.adapter_id,
        capability_id=capability.capability_id,
        actions=frozenset({"finalize", "freeze", "record_result", "start_attempt", "status"}),
        input_schema_id=capability.input_schema_id,
        result_schema_id=capability.result_schema_id,
        ready=lambda: True,
        authorize=lambda invocation: invocation.trusted_user_text.startswith("Authorize review ledger"),
        execute=execute,
        readback=lambda invocation, _result: {
            "schema": capability.result_schema_id,
            "status": "synthetic-readback-passed",
            "action": invocation.action,
        },
        idempotency_supported=True,
    )


@pytest.fixture(autouse=True)
def _enable_review_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": True,
    )
    monkeypatch.setattr(
        "tools.registered_review_ledger._feature_enabled",
        lambda _config_key="registered_workflow": True,
    )
    with _DISPATCH_LOCK:
        _COMPLETED.clear()
        _IN_FLIGHT.clear()


def test_review_ledger_tool_is_registered_and_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = registry.get_entry("registered_review_ledger")
    assert entry is not None
    assert entry.toolset == "review-ledger-controller"
    assert set(REGISTERED_REVIEW_LEDGER_SCHEMA["parameters"]["properties"]["action"]["enum"]) == {
        "finalize",
        "freeze",
        "record_result",
        "start_attempt",
        "status",
    }
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": False,
    )
    monkeypatch.setattr(
        "tools.registered_review_ledger._feature_enabled",
        lambda _config_key="registered_workflow": False,
    )
    assert not check_registered_review_ledger_requirements()


def test_disabled_review_ledger_denies_before_owner_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[WorkflowInvocation] = []
    monkeypatch.setattr(
        "tools.registered_local_workflow._feature_enabled",
        lambda _config_key="registered_workflow": False,
    )
    with (
        scoped_external_workflow_owner(_owner(calls)),
        _scoped_test_current_turn_user_authority(
            "Authorize review ledger status",
            session_id="session-review-disabled",
            turn_id="turn-review-disabled",
        ),
    ):
        result = registered_review_ledger(action="status", bundle_sha256=_DIGEST)
    assert result["decision"] == "deny_owner_unavailable"
    assert calls == []


@pytest.mark.parametrize(
    "workflow_request",
    (
        {
            "action": "freeze",
            "bundle_sha256": _DIGEST,
            "required_roles": ["code-reviewer", "security-reviewer"],
            "created_at": _TIMESTAMP,
        },
        {
            "action": "start_attempt",
            "bundle_sha256": _DIGEST,
            "role": "code-reviewer",
            "started_at": _TIMESTAMP,
        },
        {
            "action": "record_result",
            "attempt_id": 1,
            "outcome": "PASS",
            "finding_classes": ["no_findings"],
            "completed_at": _TIMESTAMP,
        },
        {"action": "status", "bundle_sha256": _DIGEST},
        {
            "action": "finalize",
            "bundle_sha256": _DIGEST,
            "current_bundle_sha256": _OTHER_DIGEST,
        },
    ),
)
def test_all_five_review_ledger_actions_use_main_controller_owner_readback(
    workflow_request: dict[str, object],
) -> None:
    calls: list[WorkflowInvocation] = []
    with (
        scoped_external_workflow_owner(_owner(calls)),
        _scoped_test_current_turn_user_authority(
            f"Authorize review ledger {workflow_request['action']}",
            session_id=f"session-review-{workflow_request['action']}",
            turn_id=f"turn-review-{workflow_request['action']}",
        ),
    ):
        result = registered_review_ledger(**workflow_request)
    assert result["schema"] == "review-ledger-controller-result/v1"
    assert result["decision"] == "allow"
    assert result["readback"] == "passed"
    assert len(calls) == 1
    assert calls[0].action == workflow_request["action"]


@pytest.mark.parametrize(
    "workflow_request",
    (
        {"action": "status", "bundle_sha256": _DIGEST},
        {
            "action": "finalize",
            "bundle_sha256": _DIGEST,
            "current_bundle_sha256": _OTHER_DIGEST,
        },
    ),
)
def test_review_ledger_read_actions_invoke_owner_on_each_read_without_mutation_replay_cache(
    workflow_request: dict[str, object],
) -> None:
    calls: list[WorkflowInvocation] = []
    with (
        scoped_external_workflow_owner(_owner(calls)),
        _scoped_test_current_turn_user_authority(
            f"Authorize review ledger {workflow_request['action']}",
            session_id=f"session-review-read-{workflow_request['action']}",
            turn_id=f"turn-review-read-{workflow_request['action']}",
        ),
    ):
        first = registered_review_ledger(**workflow_request)
        second = registered_review_ledger(**workflow_request)

    assert first["decision"] == "allow"
    assert first["idempotency_result"] == "read_only"
    assert first["write_count"] == 0
    assert second["decision"] == "allow"
    assert second["idempotency_result"] == "read_only"
    assert second["write_count"] == 0
    assert len(calls) == 2
    assert all(call.action == workflow_request["action"] for call in calls)


def test_review_ledger_requires_independent_main_controller_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[WorkflowInvocation] = []
    monkeypatch.setattr(
        "tools.registered_local_workflow.get_session_controller_role",
        lambda: "",
    )
    with (
        scoped_external_workflow_owner(_owner(calls)),
        _scoped_test_current_turn_user_authority(
            "Authorize review ledger status",
            session_id="session-review-denied",
            turn_id="turn-review-denied",
        ),
    ):
        result = registered_review_ledger(action="status", bundle_sha256=_DIGEST)
    assert result["decision"] == "deny_authority_missing"
    assert calls == []


@pytest.mark.parametrize(
    "workflow_request",
    (
        {"action": object()},
        {"action": "status", "bundle_sha256": object()},
        {"action": "freeze", "bundle_sha256": _DIGEST, "required_roles": [], "created_at": _TIMESTAMP},
        {
            "action": "record_result",
            "attempt_id": 1,
            "outcome": "PASS",
            "finding_classes": ["security"],
            "completed_at": _TIMESTAMP,
        },
    ),
)
def test_review_ledger_malformed_shapes_deny_without_raising(
    workflow_request: dict[str, object],
) -> None:
    result = registered_review_ledger(**workflow_request)
    assert result["decision"] == "deny_schema_invalid"


@pytest.mark.parametrize(
    "forbidden_field",
    ("authority", "idempotency_key", "owner", "session_id"),
)
def test_review_ledger_handler_rejects_caller_controlled_boundary_fields(
    forbidden_field: str,
) -> None:
    parsed = json.loads(
        _handle_registered_review_ledger(
            {
                "action": "status",
                "bundle_sha256": _DIGEST,
                forbidden_field: "forged",
            }
        )
    )
    assert parsed["decision"] == "deny_schema_invalid"
