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


class WorkflowActionClass(StrEnum):
    READ_ONLY = "read_only"
    STATUS_MEMORY = "status_memory"
    EXPLICIT_BLOCKED_CARD_CREATE = "explicit_blocked_card_create"
    TRUSTED_LOCAL_RECORD = "trusted_local_record"
    APPROVAL_REQUIRED_LIVE_MUTATION = "approval_required_live_mutation"
    DESTRUCTIVE_OR_PUBLIC = "destructive_or_public"


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
