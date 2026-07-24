"""Pure business-action taxonomy for trusted workflow approval decisions.

This module classifies typed workflow metadata.  It does not parse shell text,
read current-turn authority, perform I/O, or bypass terminal/Tirith guards.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class WorkflowActionClass(StrEnum):
    READ_ONLY = "read_only"
    STATUS_MEMORY = "status_memory"
    EXPLICIT_BLOCKED_CARD_CREATE = "explicit_blocked_card_create"
    TRUSTED_LOCAL_RECORD = "trusted_local_record"
    APPROVAL_REQUIRED_LIVE_MUTATION = "approval_required_live_mutation"
    DESTRUCTIVE_OR_PUBLIC = "destructive_or_public"


class WorkflowEffect(StrEnum):
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    SOFT_DELETE = "soft_delete"
    RESTORE = "restore"


class AuthorityMode(StrEnum):
    FOREGROUND_CURRENT_TURN = "foreground_current_turn"
    EXISTING_DISPATCHER_WORKER = "existing_dispatcher_worker"
    MAIN_CONTROLLER = "main_controller"
    LOCAL_READ_BOUNDARY = "local_read_boundary"


class IdempotencyMode(StrEnum):
    OWNER_ATOMIC = "owner_atomic"
    DETERMINISTIC_REPLAY = "deterministic_replay"
    READ_ONLY = "read_only"


class CapabilityDecision(StrEnum):
    ALLOW = "allow"
    DENY_UNREGISTERED_ACTION = "deny_unregistered_action"
    DENY_AUTHORITY_MISSING = "deny_authority_missing"
    DENY_TARGET_MISMATCH = "deny_target_mismatch"
    DENY_SCHEMA_INVALID = "deny_schema_invalid"
    DENY_OWNER_UNAVAILABLE = "deny_owner_unavailable"
    DENY_SOFT_DELETE_NOT_RESTORABLE = "deny_soft_delete_not_restorable"
    DENY_LIVE_OR_EXTERNAL_BOUNDARY = "deny_live_or_external_boundary"
    HARD_BLOCK = "hard_block"


@dataclass(frozen=True, slots=True)
class RegisteredCapability:
    capability_id: str
    effects: frozenset[WorkflowEffect]
    authority_modes: frozenset[AuthorityMode]
    adapter_id: str
    input_schema_id: str
    result_schema_id: str
    idempotency: IdempotencyMode
    readback_required: bool
    soft_delete_restore_required: bool


_CAPABILITY_ID_RE = re.compile(
    r"^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*\.v[1-9][0-9]*$"
)
_SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9-]*/v[1-9][0-9]*$")
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ADAPTER_IDS = frozenset(
    {
        "kanban-status-memory",
        "kanban-intake-pending",
        "lifelog-diet-recorder",
    }
)


def validate_registered_capability(capability: RegisteredCapability) -> None:
    if type(capability) is not RegisteredCapability:
        raise TypeError("capability must be an exact RegisteredCapability")
    if type(capability.capability_id) is not str or not _CAPABILITY_ID_RE.fullmatch(
        capability.capability_id
    ):
        raise ValueError("capability_id must be a versioned lowercase ID")
    if type(capability.effects) is not frozenset or not capability.effects:
        raise ValueError("effects must be a non-empty frozenset")
    if any(type(effect) is not WorkflowEffect for effect in capability.effects):
        raise TypeError("effects must contain exact WorkflowEffect values")
    if (
        type(capability.authority_modes) is not frozenset
        or not capability.authority_modes
    ):
        raise ValueError("authority_modes must be a non-empty frozenset")
    if any(
        type(mode) is not AuthorityMode for mode in capability.authority_modes
    ):
        raise TypeError("authority_modes must contain exact AuthorityMode values")
    if (
        type(capability.adapter_id) is not str
        or not _ADAPTER_ID_RE.fullmatch(capability.adapter_id)
        or capability.adapter_id not in _ADAPTER_IDS
    ):
        raise ValueError("adapter_id is not registered")
    for name, value in (
        ("input_schema_id", capability.input_schema_id),
        ("result_schema_id", capability.result_schema_id),
    ):
        if type(value) is not str or not _SCHEMA_ID_RE.fullmatch(value):
            raise ValueError(f"{name} must be a versioned lowercase schema ID")
    if type(capability.idempotency) is not IdempotencyMode:
        raise TypeError("idempotency must be an exact IdempotencyMode")
    if type(capability.readback_required) is not bool:
        raise TypeError("readback_required must be an exact bool")
    if type(capability.soft_delete_restore_required) is not bool:
        raise TypeError("soft_delete_restore_required must be an exact bool")
    if WorkflowEffect.SOFT_DELETE in capability.effects and (
        WorkflowEffect.RESTORE not in capability.effects
        or not capability.soft_delete_restore_required
    ):
        raise ValueError("soft delete requires an explicit restore contract")


_REGISTERED_CAPABILITIES: Mapping[str, RegisteredCapability] = MappingProxyType(
    {
        "kanban.status-memory.v1": RegisteredCapability(
            capability_id="kanban.status-memory.v1",
            effects=frozenset({WorkflowEffect.CREATE, WorkflowEffect.READ}),
            authority_modes=frozenset(
                {
                    AuthorityMode.FOREGROUND_CURRENT_TURN,
                    AuthorityMode.EXISTING_DISPATCHER_WORKER,
                    AuthorityMode.LOCAL_READ_BOUNDARY,
                }
            ),
            adapter_id="kanban-status-memory",
            input_schema_id="kanban-status-memory/v1",
            result_schema_id="kanban-status-memory-result/v1",
            idempotency=IdempotencyMode.OWNER_ATOMIC,
            readback_required=True,
            soft_delete_restore_required=False,
        ),

        "kanban-intake.pending-soft-delete.v1": RegisteredCapability(
            capability_id="kanban-intake.pending-soft-delete.v1",
            effects=frozenset(
                {
                    WorkflowEffect.READ,
                    WorkflowEffect.SOFT_DELETE,
                    WorkflowEffect.RESTORE,
                }
            ),
            authority_modes=frozenset(
                {
                    AuthorityMode.FOREGROUND_CURRENT_TURN,
                    AuthorityMode.LOCAL_READ_BOUNDARY,
                }
            ),
            adapter_id="kanban-intake-pending",
            input_schema_id="kanban-intake-pending/v1",
            result_schema_id="kanban-intake-pending-result/v1",
            idempotency=IdempotencyMode.OWNER_ATOMIC,
            readback_required=True,
            soft_delete_restore_required=True,
        ),
        "lifelog.diet-intake.v1": RegisteredCapability(
            capability_id="lifelog.diet-intake.v1",
            effects=frozenset({WorkflowEffect.CREATE}),
            authority_modes=frozenset({AuthorityMode.FOREGROUND_CURRENT_TURN}),
            adapter_id="lifelog-diet-recorder",
            input_schema_id="lifelog-diet-intake/v1",
            result_schema_id="registered-recorder-result/v1",
            idempotency=IdempotencyMode.DETERMINISTIC_REPLAY,
            readback_required=True,
            soft_delete_restore_required=False,
        ),
    }
)

_REGISTERED_OPERATIONS: Mapping[tuple[str, str], WorkflowEffect] = MappingProxyType(
    {
        ("kanban.status-memory.v1", "kanban_status_memory_comment"): WorkflowEffect.CREATE,
        ("kanban.status-memory.v1", "list_comments"): WorkflowEffect.READ,

        ("kanban-intake.pending-soft-delete.v1", "pending_read"): WorkflowEffect.READ,
        ("kanban-intake.pending-soft-delete.v1", "pending_soft_delete"): WorkflowEffect.SOFT_DELETE,
        ("kanban-intake.pending-soft-delete.v1", "pending_restore"): WorkflowEffect.RESTORE,
        ("lifelog.diet-intake.v1", "diet_intake_record"): WorkflowEffect.CREATE,
    }
)
_REGISTERED_OPERATION_AUTHORITIES: Mapping[
    tuple[str, str], frozenset[AuthorityMode]
] = MappingProxyType(
    {
        ("kanban.status-memory.v1", "kanban_status_memory_comment"): frozenset(
            {
                AuthorityMode.FOREGROUND_CURRENT_TURN,
                AuthorityMode.EXISTING_DISPATCHER_WORKER,
            }
        ),
        ("kanban.status-memory.v1", "list_comments"): frozenset(
            {
                AuthorityMode.FOREGROUND_CURRENT_TURN,
                AuthorityMode.EXISTING_DISPATCHER_WORKER,
                AuthorityMode.LOCAL_READ_BOUNDARY,
            }
        ),
        ("kanban-intake.pending-soft-delete.v1", "pending_read"): frozenset(
            {
                AuthorityMode.FOREGROUND_CURRENT_TURN,
                AuthorityMode.LOCAL_READ_BOUNDARY,
            }
        ),
        ("kanban-intake.pending-soft-delete.v1", "pending_soft_delete"): frozenset(
            {AuthorityMode.FOREGROUND_CURRENT_TURN}
        ),
        ("kanban-intake.pending-soft-delete.v1", "pending_restore"): frozenset(
            {AuthorityMode.FOREGROUND_CURRENT_TURN}
        ),
        ("lifelog.diet-intake.v1", "diet_intake_record"): frozenset(
            {AuthorityMode.FOREGROUND_CURRENT_TURN}
        ),
    }
)

for _capability in _REGISTERED_CAPABILITIES.values():
    validate_registered_capability(_capability)


def registered_capability_catalog() -> Mapping[str, RegisteredCapability]:
    return _REGISTERED_CAPABILITIES


def evaluate_registered_capability(
    capability_id: str,
    operation: str,
    effect: WorkflowEffect,
    *,
    schema_valid: bool,
    authority_mode: AuthorityMode | None,
    owner_ready: bool,
    target_valid: bool,
    restore_contract_valid: bool = True,
    represented_live_or_external_boundary: bool = False,
) -> CapabilityDecision:
    if represented_live_or_external_boundary is True:
        return CapabilityDecision.HARD_BLOCK
    expected_effect = _REGISTERED_OPERATIONS.get((capability_id, operation))
    if expected_effect is None or type(effect) is not WorkflowEffect or effect is not expected_effect:
        return CapabilityDecision.DENY_UNREGISTERED_ACTION
    flags = (
        schema_valid,
        owner_ready,
        target_valid,
        restore_contract_valid,
        represented_live_or_external_boundary,
    )
    if any(type(value) is not bool for value in flags):
        return CapabilityDecision.DENY_SCHEMA_INVALID
    if not schema_valid:
        return CapabilityDecision.DENY_SCHEMA_INVALID
    if authority_mode is None:
        return CapabilityDecision.DENY_AUTHORITY_MISSING
    if type(authority_mode) is not AuthorityMode:
        return CapabilityDecision.DENY_SCHEMA_INVALID
    allowed_authorities = _REGISTERED_OPERATION_AUTHORITIES[(capability_id, operation)]
    if authority_mode not in allowed_authorities:
        return CapabilityDecision.DENY_AUTHORITY_MISSING
    if not owner_ready:
        return CapabilityDecision.DENY_OWNER_UNAVAILABLE
    if not target_valid:
        return CapabilityDecision.DENY_TARGET_MISMATCH
    if effect in {WorkflowEffect.SOFT_DELETE, WorkflowEffect.RESTORE} and not restore_contract_valid:
        return CapabilityDecision.DENY_SOFT_DELETE_NOT_RESTORABLE
    return CapabilityDecision.ALLOW


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    """Typed, model-independent metadata about one proposed workflow action."""

    operation: str
    target_exists: bool = False
    changes_lifecycle: bool = False
    publishes_external: bool = False
    reads_credentials: bool = False
    destructive: bool = False
    unknown_db_action: bool = False
    read_only: bool = False
    explicit_blocked_card_create: bool = False
    deterministic_idempotency_key: bool = False
    registered_recorder: bool = False
    confirmed_fact: bool = False
    exact_target: bool = False


_STATUS_MEMORY_OPERATIONS = frozenset(
    {
        "kanban.comment",
        "kanban.progress_note",
        "kanban.artifact_link",
        "kanban.verification_summary",
        "kanban.handoff_note",
        "kanban.heartbeat",
    }
)


def _normalize_action_fingerprint_part(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def build_blocked_card_idempotency_key_from_fingerprint(
    *, board: str, user_action_fingerprint: str, target_scope: str
) -> str:
    """Derive a blocked-card key from hidden accepted-user authority."""
    if not re.fullmatch(r"[a-f0-9]{64}", user_action_fingerprint):
        raise ValueError("user_action_fingerprint must be a SHA-256 token")
    fingerprint = {
        "schema": "blocked-card-action-fingerprint/v1",
        "board": _normalize_action_fingerprint_part(board, "board"),
        "user_action_fingerprint": user_action_fingerprint,
        "target_scope": _normalize_action_fingerprint_part(
            target_scope, "target_scope"
        ),
    }
    encoded = json.dumps(
        fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"blocked-card:v1:{hashlib.sha256(encoded).hexdigest()[:32]}"


def build_blocked_card_idempotency_key(
    *, board: str, exact_user_action: str, target_scope: str
) -> str:
    """Derive a stable key from the exact blocked-card action fingerprint.

    The caller must pass the accepted current user action, never assistant or
    retrieved text. The digest is for deduplication only, not approval evidence.
    """
    normalized_action = _normalize_action_fingerprint_part(
        exact_user_action, "exact_user_action"
    )
    user_action_fingerprint = hashlib.sha256(
        normalized_action.encode("utf-8")
    ).hexdigest()
    return build_blocked_card_idempotency_key_from_fingerprint(
        board=board,
        user_action_fingerprint=user_action_fingerprint,
        target_scope=target_scope,
    )


def classify_workflow_action(action: WorkflowAction) -> WorkflowActionClass:
    """Classify *action* with fail-closed precedence.

    The result explains the business/action class only.  It is never an allow
    token for raw terminal commands, direct SQL, or arbitrary interpreters.
    """
    if action.publishes_external or action.reads_credentials or action.destructive:
        return WorkflowActionClass.DESTRUCTIVE_OR_PUBLIC

    if action.read_only:
        return WorkflowActionClass.READ_ONLY

    if action.changes_lifecycle or action.unknown_db_action:
        return WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION

    if action.explicit_blocked_card_create:
        if action.deterministic_idempotency_key:
            return WorkflowActionClass.EXPLICIT_BLOCKED_CARD_CREATE
        return WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION

    if action.registered_recorder:
        if action.confirmed_fact and action.exact_target:
            return WorkflowActionClass.TRUSTED_LOCAL_RECORD
        return WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION

    if action.operation in _STATUS_MEMORY_OPERATIONS and action.target_exists:
        return WorkflowActionClass.STATUS_MEMORY

    return WorkflowActionClass.APPROVAL_REQUIRED_LIVE_MUTATION
