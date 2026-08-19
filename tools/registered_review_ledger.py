"""Default-off fixed five-action bridge to the external review ledger owner."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from agent.workflow_action_policy import AuthorityMode, CapabilityDecision, WorkflowEffect
from tools.registered_local_workflow import (
    _dispatch_registered_action,
    _feature_enabled,
    _owner_for,
)
from tools.registry import registry
from tools.workflow_authority import opaque_workflow_action_id

_ACTIONS = frozenset({"finalize", "freeze", "record_result", "start_attempt", "status"})
_EFFECTS = {
    "freeze": WorkflowEffect.CREATE,
    "start_attempt": WorkflowEffect.CREATE,
    "record_result": WorkflowEffect.UPDATE,
    "status": WorkflowEffect.READ,
    "finalize": WorkflowEffect.READ,
}
_EXPECTED_FIELDS = {
    "freeze": frozenset({"action", "bundle_sha256", "required_roles", "created_at"}),
    "start_attempt": frozenset({"action", "bundle_sha256", "role", "started_at"}),
    "record_result": frozenset({"action", "attempt_id", "outcome", "finding_classes", "completed_at"}),
    "status": frozenset({"action", "bundle_sha256"}),
    "finalize": frozenset({"action", "bundle_sha256", "current_bundle_sha256"}),
}
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
_OUTCOMES = frozenset({"CANCELLED", "EXECUTION_FAILED", "PASS", "REQUEST_CHANGES", "TIMED_OUT"})
_FINDING_CLASSES = frozenset(
    {
        "correctness",
        "maintainability",
        "no_findings",
        "operations",
        "privacy",
        "scope",
        "security",
        "test_coverage",
    }
)

REGISTERED_REVIEW_LEDGER_SCHEMA = {
    "name": "registered_review_ledger",
    "description": (
        "Execute the enabled external review-ledger owner's fixed five-action protocol. "
        "Main-controller current-turn authority is required; no path, SQL, command, "
        "environment, raw review text, or owner is caller controlled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTIONS)},
            "bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "required_roles": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,62}[a-z0-9]$"},
                "minItems": 1,
                "maxItems": 16,
            },
            "created_at": {"type": "string", "maxLength": 128},
            "role": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,62}[a-z0-9]$"},
            "started_at": {"type": "string", "maxLength": 128},
            "attempt_id": {"type": "integer", "minimum": 1},
            "outcome": {"type": "string", "enum": sorted(_OUTCOMES)},
            "finding_classes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_FINDING_CLASSES)},
                "maxItems": 8,
            },
            "completed_at": {"type": "string", "maxLength": 128},
            "current_bundle_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _result(decision: CapabilityDecision | str) -> dict[str, Any]:
    value = decision.value if type(decision) is CapabilityDecision else str(decision)
    return {
        "schema": "review-ledger-controller-result/v1",
        "decision": value,
        "prompt_count": 0,
        "write_count": 0,
        "action_id": opaque_workflow_action_id(),
        "uncertain_outcome": False,
    }


def _valid_timestamp(value: object) -> bool:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 128:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _valid_role(value: object) -> bool:
    return type(value) is str and _ROLE_RE.fullmatch(value) is not None


def _validate_request(request: object) -> bool:
    if type(request) is not dict:
        return False
    action = request.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return False
    if frozenset(request) != _EXPECTED_FIELDS[action]:
        return False
    if action in {"finalize", "freeze", "start_attempt", "status"} and not _valid_digest(
        request.get("bundle_sha256")
    ):
        return False
    if action == "freeze":
        roles = request.get("required_roles")
        return bool(
            type(roles) is list
            and 1 <= len(roles) <= 16
            and roles == sorted(roles)
            and len(roles) == len(set(roles))
            and all(_valid_role(role) for role in roles)
            and _valid_timestamp(request.get("created_at"))
        )
    if action == "start_attempt":
        return _valid_role(request.get("role")) and _valid_timestamp(request.get("started_at"))
    if action == "record_result":
        attempt_id = request.get("attempt_id")
        outcome = request.get("outcome")
        findings = request.get("finding_classes")
        if (
            type(attempt_id) is not int
            or attempt_id <= 0
            or type(outcome) is not str
            or outcome not in _OUTCOMES
            or type(findings) is not list
            or len(findings) > 8
            or findings != sorted(findings)
            or len(findings) != len(set(findings))
            or any(type(item) is not str or item not in _FINDING_CLASSES for item in findings)
            or not _valid_timestamp(request.get("completed_at"))
        ):
            return False
        if outcome == "PASS":
            return findings == ["no_findings"]
        if outcome == "REQUEST_CHANGES":
            return bool(findings and "no_findings" not in findings)
        return findings == []
    if action == "finalize":
        return _valid_digest(request.get("current_bundle_sha256"))
    return True


def check_registered_review_ledger_requirements() -> bool:
    if not _feature_enabled("review_ledger_controller"):
        return False
    owner = _owner_for("review-ledger.history.v1")
    if owner is None:
        return False
    try:
        return owner.ready() is True
    except Exception:
        return False


def registered_review_ledger(**request: Any) -> dict[str, Any]:
    if not _validate_request(request):
        action = request.get("action")
        if type(action) is str and action not in _ACTIONS:
            return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
        return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    action = request["action"]
    target = (
        f"review-attempt:{request['attempt_id']}"
        if action == "record_result"
        else f"review-bundle:{request['bundle_sha256']}"
    )
    outcome = _dispatch_registered_action(
        capability_id="review-ledger.history.v1",
        action=action,
        effect=_EFFECTS[action],
        target=target,
        payload={key: value for key, value in request.items() if key != "action"},
        authority_mode=AuthorityMode.MAIN_CONTROLLER,
        config_key="review_ledger_controller",
    )
    outcome["schema"] = "review-ledger-controller-result/v1"
    return outcome


def _handle_registered_review_ledger(args: Any, **_context: Any) -> str:
    if type(args) is not dict or any(
        key in args for key in ("authority", "idempotency_key", "owner", "session_id")
    ):
        result = _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    else:
        try:
            result = registered_review_ledger(**args)
        except TypeError:
            result = _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    return json.dumps(result, ensure_ascii=True, sort_keys=True)


registry.register(
    name="registered_review_ledger",
    toolset="review-ledger-controller",
    schema=REGISTERED_REVIEW_LEDGER_SCHEMA,
    handler=_handle_registered_review_ledger,
    check_fn=check_registered_review_ledger_requirements,
    description=REGISTERED_REVIEW_LEDGER_SCHEMA["description"],
    emoji="🧾",
)


__all__ = [
    "REGISTERED_REVIEW_LEDGER_SCHEMA",
    "check_registered_review_ledger_requirements",
    "registered_review_ledger",
]
