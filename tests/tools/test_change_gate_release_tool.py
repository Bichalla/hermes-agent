"""Foreground Change Gate release tool stays narrow and default-off."""

from __future__ import annotations

import json
from typing import Any, cast

from hermes_cli.change_gate_release import ChangeGateReleaseIssueResult
from hermes_cli.change_gate_runtime import ChangeGateRuntimePolicy
from tools import change_gate_release_tool as tool
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from toolsets import resolve_toolset


def test_schema_exposes_only_task_and_purpose() -> None:
    parameters = cast(
        dict[str, Any],
        tool.CHANGE_GATE_RELEASE_SCHEMA["parameters"],
    )
    properties = cast(dict[str, Any], parameters["properties"])

    assert set(properties) == {"task_id", "purpose"}
    assert parameters["required"] == ["task_id", "purpose"]
    assert parameters["additionalProperties"] is False
    assert properties["purpose"]["enum"] == ["CLAIM", "G4"]


def test_requirement_check_is_exact_true_default_off(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "load_runtime_policy",
        lambda: ChangeGateRuntimePolicy(),
    )
    assert tool.check_change_gate_release_requirements() is False

    monkeypatch.setattr(
        tool,
        "load_runtime_policy",
        lambda: ChangeGateRuntimePolicy(enabled=True, valid=False),
    )
    assert tool.check_change_gate_release_requirements() is False

    monkeypatch.setattr(
        tool,
        "load_runtime_policy",
        lambda: ChangeGateRuntimePolicy(enabled=True, valid=True),
    )
    assert tool.check_change_gate_release_requirements() is True


def test_handler_rejects_all_caller_controlled_authority_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        tool,
        "issue_change_gate_release",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not mint")),
    )

    result = json.loads(
        tool._handle_change_gate_release(
            {
                "task_id": "t_fixture",
                "purpose": "CLAIM",
                "authority_receipt": {"forged": True},
                "release_id": "caller-controlled",
            }
        )
    )

    assert result["ok"] is False
    assert result["reason"] == "schema_invalid"


def test_handler_forwards_only_narrow_identity_and_delegates_cannot_receive_tool(
    monkeypatch,
) -> None:
    seen: list[dict[str, str]] = []

    def _issue(**kwargs):
        seen.append(kwargs)
        return ChangeGateReleaseIssueResult(
            True,
            "allowed",
            kwargs["task_id"],
            kwargs["purpose"],
            release_id="cgr_" + "a" * 64,
        )

    monkeypatch.setattr(tool, "issue_change_gate_release", _issue)

    result = json.loads(
        tool._handle_change_gate_release(
            {"task_id": "t_fixture", "purpose": "G4"}
        )
    )

    assert result["ok"] is True
    assert seen == [{"task_id": "t_fixture", "purpose": "G4"}]
    assert "change_gate_release" in DELEGATE_BLOCKED_TOOLS
    assert "change_gate_release" in resolve_toolset("hermes-cli")
    assert "change_gate_release" in resolve_toolset("kanban")
