"""Tool surface for foreground Change Gate release issuance."""

from __future__ import annotations

import json
from typing import Any

from hermes_cli.change_gate_release import issue_change_gate_release
from hermes_cli.change_gate_runtime import load_runtime_policy
from tools.registry import registry


CHANGE_GATE_RELEASE_SCHEMA = {
    "name": "change_gate_release",
    "description": (
        "Issue a one-use Change Gate release for the current foreground turn. "
        "Only task_id and purpose are accepted; authority and release metadata "
        "are derived by the host."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Kanban task id whose frozen Change Gate artifacts are attached.",
            },
            "purpose": {
                "type": "string",
                "enum": ["CLAIM", "G4"],
                "description": "Boundary the release authorizes.",
            },
        },
        "required": ["task_id", "purpose"],
        "additionalProperties": False,
    },
}

_ALLOWED_KEYS = frozenset({"task_id", "purpose"})


def check_change_gate_release_requirements() -> bool:
    policy = load_runtime_policy()
    return policy.enabled is True and policy.valid is True


def _handle_change_gate_release(args: Any, **_context: Any) -> str:
    if type(args) is not dict or frozenset(args) != _ALLOWED_KEYS:
        result = {
            "schema": "change-gate-release-issue-result/v1",
            "ok": False,
            "reason": "schema_invalid",
            "task_id": "",
            "purpose": "",
            "review_count": 0,
        }
    else:
        result = issue_change_gate_release(
            task_id=args["task_id"],
            purpose=args["purpose"],
        ).as_dict()
    return json.dumps(result, ensure_ascii=True, sort_keys=True)


registry.register(
    name="change_gate_release",
    toolset="kanban",
    schema=CHANGE_GATE_RELEASE_SCHEMA,
    handler=_handle_change_gate_release,
    check_fn=check_change_gate_release_requirements,
    description=CHANGE_GATE_RELEASE_SCHEMA["description"],
    emoji="🔐",
)


__all__ = [
    "CHANGE_GATE_RELEASE_SCHEMA",
    "check_change_gate_release_requirements",
    "issue_change_gate_release",
]
