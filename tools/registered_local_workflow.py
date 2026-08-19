"""No-live fixed-action contract for externally owned local workflows.

The adapter validates capability, schema, authority, owner readiness and
readback/idempotency requirements. It intentionally does not dispatch to owner
scripts, open databases, read Profiles, accept SQL/paths, or perform network or
provider effects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    IdempotencyMode,
    WorkflowEffect,
    registered_capability_catalog,
    evaluate_registered_capability,
)
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    is_host_issued_current_turn_authority,
)

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,96}$")
_OWNER_RE = re.compile(r"^[a-z][a-z0-9-]{0,96}$")
_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9-]*(?:/[a-z0-9-]+)?/v[1-9][0-9]*$")
_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9:._/-]{0,180}$")
_PLUGIN_RE = re.compile(r"^[a-z][a-z0-9-]{0,96}$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,96}$")


@dataclass(frozen=True, slots=True)
class SyntheticOwnerInterface:
    owner_id: str
    capability_id: str
    actions: frozenset[str]
    input_schema_id: str
    result_schema_id: str
    readback_supported: bool
    idempotency_supported: bool
    no_live: bool = True


@dataclass(frozen=True, slots=True)
class ExternalPluginContract:
    plugin_id: str
    tool_name: str
    input_schema_id: str
    result_schema_id: str
    privacy_projection: str
    no_live: bool = True


@dataclass(frozen=True, slots=True)
class WorkflowDispatchRequest:
    capability_id: str
    operation: str
    effect: WorkflowEffect
    input_schema_id: str
    target: str
    authority: CurrentTurnUserAuthority | None
    owner: SyntheticOwnerInterface
    idempotency_key: str
    authority_mode: AuthorityMode = AuthorityMode.FOREGROUND_CURRENT_TURN


@dataclass(frozen=True, slots=True)
class WorkflowDispatchEvaluation:
    decision: CapabilityDecision
    owner_id: str
    capability_id: str
    operation: str
    readback_required: bool
    idempotency_required: bool
    uncertain_outcome: bool


def _valid_owner(owner: SyntheticOwnerInterface) -> bool:
    catalog = registered_capability_catalog()
    capability = catalog.get(owner.capability_id)
    return (
        type(owner) is SyntheticOwnerInterface
        and owner.no_live is True
        and _OWNER_RE.fullmatch(owner.owner_id) is not None
        and capability is not None
        and type(owner.actions) is frozenset
        and bool(owner.actions)
        and all(type(action) is str and _ACTION_RE.fullmatch(action) for action in owner.actions)
        and owner.input_schema_id == capability.input_schema_id
        and owner.result_schema_id == capability.result_schema_id
        and type(owner.readback_supported) is bool
        and type(owner.idempotency_supported) is bool
    )


def _schema_matches(operation_schema: str, owner_schema: str, request_schema: str) -> bool:
    return (
        type(request_schema) is str
        and _SCHEMA_RE.fullmatch(request_schema) is not None
        and request_schema in {operation_schema, owner_schema}
    )


def build_deterministic_idempotency_key(
    *,
    capability_id: str,
    operation: str,
    target: str,
    authority: CurrentTurnUserAuthority,
) -> str:
    if not is_host_issued_current_turn_authority(authority):
        raise ValueError("authority_invalid")
    payload = {
        "capability_id": capability_id,
        "operation": operation,
        "session": authority.session_fingerprint,
        "target": target,
        "user_action": authority.user_action_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return "registered-workflow:v1:" + hashlib.sha256(encoded).hexdigest()


def evaluate_no_live_workflow_dispatch(
    request: WorkflowDispatchRequest,
) -> WorkflowDispatchEvaluation:
    catalog = registered_capability_catalog()
    capability = catalog.get(request.capability_id)
    schema_valid = (
        type(request) is WorkflowDispatchRequest
        and capability is not None
        and _valid_owner(request.owner)
        and request.operation in request.owner.actions
        and _ACTION_RE.fullmatch(request.operation) is not None
        and _TARGET_RE.fullmatch(request.target) is not None
        and _schema_matches(capability.input_schema_id, request.owner.input_schema_id, request.input_schema_id)
    )
    authority_valid = (
        (
            request.authority_mode is AuthorityMode.FOREGROUND_CURRENT_TURN
            and is_host_issued_current_turn_authority(request.authority)
        )
        or request.authority_mode
        in {AuthorityMode.MAIN_CONTROLLER, AuthorityMode.LOCAL_READ_BOUNDARY}
    )
    idempotency_required = bool(
        capability is not None and capability.idempotency is not IdempotencyMode.READ_ONLY
    )
    idempotency_valid = (
        type(request.idempotency_key) is str
        and request.idempotency_key.startswith("registered-workflow:v1:")
        and (
            is_host_issued_current_turn_authority(request.authority)
            or request.authority_mode is AuthorityMode.MAIN_CONTROLLER
        )
    )
    if idempotency_required and not idempotency_valid:
        schema_valid = False
    owner_ready = bool(
        _valid_owner(request.owner)
        and request.owner.readback_supported
        and (request.owner.idempotency_supported or not idempotency_required)
    )
    decision = evaluate_registered_capability(
        request.capability_id,
        request.operation,
        request.effect,
        schema_valid=schema_valid,
        authority_mode=request.authority_mode if authority_valid else None,
        owner_ready=owner_ready,
        target_valid=schema_valid,
        restore_contract_valid=True,
        represented_live_or_external_boundary=False,
    )
    return WorkflowDispatchEvaluation(
        decision=decision,
        owner_id=request.owner.owner_id,
        capability_id=request.capability_id,
        operation=request.operation,
        readback_required=bool(capability is not None and capability.readback_required),
        idempotency_required=idempotency_required,
        uncertain_outcome=decision is not CapabilityDecision.ALLOW,
    )


def validate_external_plugin_contract(contract: ExternalPluginContract) -> bool:
    return (
        type(contract) is ExternalPluginContract
        and contract.no_live is True
        and _PLUGIN_RE.fullmatch(contract.plugin_id) is not None
        and _TOOL_RE.fullmatch(contract.tool_name) is not None
        and _SCHEMA_RE.fullmatch(contract.input_schema_id) is not None
        and _SCHEMA_RE.fullmatch(contract.result_schema_id) is not None
        and contract.privacy_projection in {"typed-redacted-context", "routing-hints-only"}
    )


def constant_readback_fingerprint(result: object) -> str:
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(b"registered-local-workflow-readback/v1", encoded, hashlib.sha256).hexdigest()


__all__ = [
    "ExternalPluginContract",
    "SyntheticOwnerInterface",
    "WorkflowDispatchEvaluation",
    "WorkflowDispatchRequest",
    "build_deterministic_idempotency_key",
    "constant_readback_fingerprint",
    "evaluate_no_live_workflow_dispatch",
    "validate_external_plugin_contract",
]
