"""Default-off fixed-action bridge to externally owned local workflows.

Core owns only validation, current-turn authorization, a one-shot process
lease, deterministic idempotency, and readback enforcement. Domain code,
private paths, databases, commands, and live configuration remain external.
No owner is registered by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    IdempotencyMode,
    WorkflowEffect,
    evaluate_registered_capability,
    registered_capability_catalog,
)
from gateway.session_context import (
    get_session_controller_role,
    get_trusted_current_user_text,
)
from tools.registry import registry
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    get_current_turn_user_authority,
    is_host_issued_current_turn_authority,
    matches_current_workflow_session,
    opaque_workflow_action_id,
)

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,96}$")
_OWNER_RE = re.compile(r"^[a-z][a-z0-9-]{0,96}$")
_PAYLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.json$")
_PENDING_ID_RE = re.compile(r"^kp_[a-f0-9]{16}$")
_RUN_ID_RE = re.compile(r"^semantic-debug-[a-z0-9][a-z0-9-]{4,100}$")
_MEDICATION_REF_RE = re.compile(r"^med-ref-[a-f0-9]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_READBACK_STATUS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REASON_CODES = frozenset({"cleanup_confirmed", "superseded", "user_dismissed"})
_READBACK_FIELDS = frozenset(
    {"schema", "status", "action", "receipt_sha256", "item_count"}
)


@dataclass(frozen=True, slots=True)
class _ActionSpec:
    capability_id: str
    effect: WorkflowEffect
    fields: frozenset[str]
    target: str


_ACTION_SPECS: Mapping[str, _ActionSpec] = MappingProxyType(
    {
        "childcare_event_record": _ActionSpec(
            "lifelog.childcare-event.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "lifelog:childcare"
        ),
        "company_work_os_initial_seed_preview": _ActionSpec(
            "company-work-os.initial-seed-preview.v1", WorkflowEffect.READ, frozenset(), "company-work-os:initial-seed"
        ),
        "company_work_os_initial_seed_record": _ActionSpec(
            "company-work-os.initial-seed-record.v1", WorkflowEffect.CREATE, frozenset(), "company-work-os:initial-seed"
        ),
        "company_work_os_operating_record": _ActionSpec(
            "company-work-os.operating-record.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "company-work-os:operating-record"
        ),
        "company_work_os_team_roster_record": _ActionSpec(
            "company-work-os.team-roster-seed.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "company-work-os:team-roster"
        ),
        "diet_intake_record": _ActionSpec(
            "lifelog.diet-intake.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "lifelog:diet"
        ),
        "family_event_correct": _ActionSpec(
            "lifelog.family-event.v1", WorkflowEffect.UPDATE, frozenset({"payload_name"}), "lifelog:family"
        ),
        "family_event_record": _ActionSpec(
            "lifelog.family-event.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "lifelog:family"
        ),
        "medication_intake_record": _ActionSpec(
            "lifelog.medication-intake.v1", WorkflowEffect.CREATE, frozenset({"medication_refs", "occurrence_mode"}), "lifelog:medication"
        ),
        "pending_read": _ActionSpec(
            "kanban-intake.pending-soft-delete.v1", WorkflowEffect.READ, frozenset({"pending_id"}), ""
        ),
        "pending_restore": _ActionSpec(
            "kanban-intake.pending-soft-delete.v1", WorkflowEffect.RESTORE, frozenset({"pending_id"}), ""
        ),
        "pending_soft_delete": _ActionSpec(
            "kanban-intake.pending-soft-delete.v1", WorkflowEffect.SOFT_DELETE, frozenset({"pending_id", "reason_code"}), ""
        ),
        "semantic_debug_issue": _ActionSpec(
            "lifelog.semantic-debug-issue.v1", WorkflowEffect.CREATE, frozenset({"run_id"}), "lifelog:semantic-debug"
        ),
        "sleep_record": _ActionSpec(
            "lifelog.sleep-record.v1", WorkflowEffect.CREATE, frozenset({"payload_name"}), "lifelog:sleep"
        ),
    }
)

_REVIEW_ACTIONS = frozenset({"finalize", "freeze", "record_result", "start_attempt", "status"})
_KNOWN_ACTIONS_BY_CAPABILITY: dict[str, frozenset[str]] = {}
for _action_name, _action_spec in _ACTION_SPECS.items():
    _KNOWN_ACTIONS_BY_CAPABILITY[_action_spec.capability_id] = (
        _KNOWN_ACTIONS_BY_CAPABILITY.get(_action_spec.capability_id, frozenset())
        | frozenset({_action_name})
    )
_KNOWN_ACTIONS_BY_CAPABILITY["review-ledger.history.v1"] = _REVIEW_ACTIONS


REGISTERED_LOCAL_WORKFLOW_SCHEMA = {
    "name": "registered_local_workflow",
    "description": (
        "Execute one enabled, externally owned fixed workflow. Paths, SQL, commands, "
        "authority, owner selection, and idempotency are never caller controlled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTION_SPECS)},
            "payload_name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\\.json$",
            },
            "pending_id": {"type": "string", "pattern": "^kp_[a-f0-9]{16}$"},
            "reason_code": {"type": "string", "enum": sorted(_REASON_CODES)},
            "run_id": {
                "type": "string",
                "pattern": "^semantic-debug-[a-z0-9][a-z0-9-]{4,100}$",
            },
            "medication_refs": {
                "type": "array",
                "items": {"type": "string", "pattern": "^med-ref-[a-f0-9]{64}$"},
                "minItems": 1,
                "maxItems": 64,
                "uniqueItems": True,
            },
            "occurrence_mode": {"type": "string", "enum": ["local_date", "source_time"]},
            "occurred_on": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True, slots=True, repr=False)
class WorkflowInvocation:
    action: str
    capability_id: str
    effect: WorkflowEffect
    target: str
    payload: Mapping[str, Any]
    authority: CurrentTurnUserAuthority
    trusted_user_text: str
    idempotency_key: str

    def __repr__(self) -> str:
        return "WorkflowInvocation(<host-bound>)"


@dataclass(frozen=True, slots=True, repr=False)
class ExternalWorkflowOwner:
    """Callable interface implemented and registered by an external owner."""

    owner_id: str
    adapter_id: str
    capability_id: str
    actions: frozenset[str]
    input_schema_id: str
    result_schema_id: str
    ready: Callable[[], bool]
    authorize: Callable[[WorkflowInvocation], bool]
    execute: Callable[[WorkflowInvocation], object]
    readback: Callable[[WorkflowInvocation, object], object]
    idempotency_supported: bool

    def __repr__(self) -> str:
        return f"ExternalWorkflowOwner(owner_id={self.owner_id!r}, capability_id={self.capability_id!r})"


@dataclass(frozen=True, slots=True)
class ExternalPluginContract:
    plugin_id: str
    tool_name: str
    input_schema_id: str
    result_schema_id: str
    privacy_projection: str
    no_live: bool = True


_OWNER_LOCK = threading.RLock()
_OWNERS: dict[tuple[str, str], ExternalWorkflowOwner] = {}
_DISPATCH_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()
_COMPLETED: OrderedDict[str, dict[str, Any]] = OrderedDict()
_COMPLETED_LIMIT = 1024


def _feature_enabled(config_key: str = "registered_workflow") -> bool:
    try:
        from hermes_cli.config import load_config

        block = (load_config() or {}).get(config_key) or {}
        return type(block) is dict and block.get("enabled") is True
    except Exception:
        return False


def _validate_owner(owner: ExternalWorkflowOwner) -> None:
    if type(owner) is not ExternalWorkflowOwner:
        raise TypeError("owner_invalid")
    capability = registered_capability_catalog().get(owner.capability_id)
    known_actions = _KNOWN_ACTIONS_BY_CAPABILITY.get(owner.capability_id)
    if (
        type(owner.owner_id) is not str
        or _OWNER_RE.fullmatch(owner.owner_id) is None
        or capability is None
        or owner.adapter_id != capability.adapter_id
        or type(owner.actions) is not frozenset
        or not owner.actions
        or known_actions is None
        or not owner.actions.issubset(known_actions)
        or any(type(action) is not str or _ACTION_RE.fullmatch(action) is None for action in owner.actions)
        or owner.input_schema_id != capability.input_schema_id
        or owner.result_schema_id != capability.result_schema_id
        or any(not callable(callback) for callback in (owner.ready, owner.authorize, owner.execute, owner.readback))
        or type(owner.idempotency_supported) is not bool
        or (
            capability.idempotency is not IdempotencyMode.READ_ONLY
            and owner.idempotency_supported is not True
        )
    ):
        raise ValueError("owner_contract_invalid")


def register_external_workflow_owner(owner: ExternalWorkflowOwner) -> None:
    _validate_owner(owner)
    key = (owner.adapter_id, owner.capability_id)
    with _OWNER_LOCK:
        existing = _OWNERS.get(key)
        if existing is not None and existing is not owner:
            raise ValueError("owner_already_registered")
        _OWNERS[key] = owner


def unregister_external_workflow_owner(owner: ExternalWorkflowOwner) -> None:
    key = (owner.adapter_id, owner.capability_id)
    with _OWNER_LOCK:
        if _OWNERS.get(key) is owner:
            _OWNERS.pop(key, None)


@contextmanager
def scoped_external_workflow_owner(owner: ExternalWorkflowOwner) -> Iterator[None]:
    register_external_workflow_owner(owner)
    try:
        yield
    finally:
        unregister_external_workflow_owner(owner)


def _owner_for(capability_id: str) -> ExternalWorkflowOwner | None:
    capability = registered_capability_catalog().get(capability_id)
    if capability is None:
        return None
    with _OWNER_LOCK:
        return _OWNERS.get((capability.adapter_id, capability_id))


def check_registered_workflow_requirements() -> bool:
    if not _feature_enabled():
        return False
    for capability_id in {spec.capability_id for spec in _ACTION_SPECS.values()}:
        owner = _owner_for(capability_id)
        if owner is not None:
            try:
                if owner.ready() is True:
                    return True
            except Exception:
                continue
    return False


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def build_deterministic_idempotency_key(
    *,
    capability_id: str,
    operation: str,
    target: str,
    payload: Mapping[str, Any],
    authority: CurrentTurnUserAuthority,
) -> str:
    if not is_host_issued_current_turn_authority(authority):
        raise ValueError("authority_invalid")
    encoded = _canonical_payload(
        {
            "capability_id": capability_id,
            "operation": operation,
            "payload_sha256": hashlib.sha256(_canonical_payload(payload)).hexdigest(),
            "session": authority.session_scope,
            "target": target,
            "turn": authority.turn_id,
            "user_action": authority.user_action_fingerprint,
        }
    )
    return "registered-workflow:v1:" + hashlib.sha256(encoded).hexdigest()


def constant_readback_fingerprint(result: object) -> str:
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(b"registered-local-workflow-readback/v1", encoded, hashlib.sha256).hexdigest()


def validate_external_plugin_contract(contract: ExternalPluginContract) -> bool:
    return bool(
        type(contract) is ExternalPluginContract
        and contract.no_live is True
        and type(contract.plugin_id) is str
        and _OWNER_RE.fullmatch(contract.plugin_id)
        and type(contract.tool_name) is str
        and _ACTION_RE.fullmatch(contract.tool_name)
        and type(contract.input_schema_id) is str
        and "/v" in contract.input_schema_id
        and type(contract.result_schema_id) is str
        and "/v" in contract.result_schema_id
        and contract.privacy_projection in {"routing-hints-only", "typed-redacted-context"}
    )


def _result(decision: CapabilityDecision | str, **extra: Any) -> dict[str, Any]:
    value = decision.value if type(decision) is CapabilityDecision else str(decision)
    result: dict[str, Any] = {
        "schema": "registered-local-workflow-result/v1",
        "decision": value,
        "prompt_count": 0,
        "write_count": 0,
        "action_id": opaque_workflow_action_id(),
        "uncertain_outcome": False,
    }
    result.update(extra)
    return result


def _normalize_readback(
    value: object,
    expected_schema: str,
    expected_action: str,
) -> dict[str, Any] | None:
    if type(value) is not dict or value.get("schema") != expected_schema:
        return None
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(raw.encode("ascii")) > 65536:
            return None
        parsed = json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        return None
    if (
        type(parsed) is not dict
        or not {"schema", "status"}.issubset(parsed)
        or not set(parsed).issubset(_READBACK_FIELDS)
        or type(parsed.get("status")) is not str
        or _READBACK_STATUS_RE.fullmatch(parsed["status"]) is None
    ):
        return None
    action = parsed.get("action")
    if action is not None and (type(action) is not str or action != expected_action):
        return None
    receipt_sha256 = parsed.get("receipt_sha256")
    if receipt_sha256 is not None and (
        type(receipt_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", receipt_sha256) is None
    ):
        return None
    item_count = parsed.get("item_count")
    if item_count is not None and (
        type(item_count) is not int or not 0 <= item_count <= 1_000_000
    ):
        return None
    return parsed


def _claim_dispatch(idempotency_key: str) -> tuple[str, dict[str, Any] | None]:
    with _DISPATCH_LOCK:
        completed = _COMPLETED.get(idempotency_key)
        if completed is not None:
            return "replay", json.loads(json.dumps(completed))
        if idempotency_key in _IN_FLIGHT:
            return "busy", None
        _IN_FLIGHT.add(idempotency_key)
        return "claimed", None


def _release_dispatch(idempotency_key: str) -> None:
    with _DISPATCH_LOCK:
        _IN_FLIGHT.discard(idempotency_key)


def _remember_dispatch(idempotency_key: str, result: dict[str, Any]) -> None:
    with _DISPATCH_LOCK:
        _COMPLETED[idempotency_key] = json.loads(json.dumps(result))
        _COMPLETED.move_to_end(idempotency_key)
        while len(_COMPLETED) > _COMPLETED_LIMIT:
            _COMPLETED.popitem(last=False)


def _dispatch_registered_action(
    *,
    capability_id: str,
    action: str,
    effect: WorkflowEffect,
    target: str,
    payload: Mapping[str, Any],
    authority_mode: AuthorityMode,
    config_key: str,
) -> dict[str, Any]:
    if not _feature_enabled(config_key):
        return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
    authority = get_current_turn_user_authority()
    trusted_user_text = get_trusted_current_user_text()
    authority_valid = bool(
        is_host_issued_current_turn_authority(authority)
        and matches_current_workflow_session(authority)
        and type(trusted_user_text) is str
        and trusted_user_text
        and (
            authority_mode is not AuthorityMode.MAIN_CONTROLLER
            or get_session_controller_role() == "main_controller"
        )
    )
    if not authority_valid:
        return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
    assert type(authority) is CurrentTurnUserAuthority
    assert type(trusted_user_text) is str

    capability = registered_capability_catalog().get(capability_id)
    owner = _owner_for(capability_id)
    owner_ready = False
    if capability is not None and owner is not None and action in owner.actions:
        try:
            owner_ready = owner.ready() is True
        except Exception:
            owner_ready = False
    decision = evaluate_registered_capability(
        capability_id,
        action,
        effect,
        schema_valid=capability is not None,
        authority_mode=authority_mode,
        owner_ready=owner_ready,
        target_valid=type(target) is str and bool(target),
        restore_contract_valid=True,
        represented_live_or_external_boundary=False,
    )
    if decision is not CapabilityDecision.ALLOW or owner is None or capability is None:
        return _result(decision)

    idempotency_key = build_deterministic_idempotency_key(
        capability_id=capability_id,
        operation=action,
        target=target,
        payload=payload,
        authority=authority,
    )
    invocation = WorkflowInvocation(
        action=action,
        capability_id=capability_id,
        effect=effect,
        target=target,
        payload=MappingProxyType(dict(payload)),
        authority=authority,
        trusted_user_text=trusted_user_text,
        idempotency_key=idempotency_key,
    )
    if effect is WorkflowEffect.READ:
        try:
            if owner.authorize(invocation) is not True:
                return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
        except Exception:
            return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
        try:
            owner_result = owner.execute(invocation)
        except Exception:
            return _result(
                "uncertain_outcome",
                write_count=0,
                uncertain_outcome=True,
                idempotency_result="read_only",
            )
        try:
            readback = _normalize_readback(
                owner.readback(invocation, owner_result),
                capability.result_schema_id,
                action,
            )
        except Exception:
            readback = None
        if readback is None:
            return _result(
                "uncertain_outcome",
                write_count=0,
                uncertain_outcome=True,
                idempotency_result="read_only",
            )
        return _result(
            CapabilityDecision.ALLOW,
            action=action,
            result=readback,
            readback="passed",
            write_count=0,
            idempotency_result="read_only",
        )

    claim, replay = _claim_dispatch(idempotency_key)
    if claim == "replay":
        assert replay is not None
        replay["action_id"] = opaque_workflow_action_id()
        if replay.get("uncertain_outcome") is True:
            replay["idempotency_result"] = "unknown"
            replay["write_count"] = None
        else:
            replay["idempotency_result"] = "existing"
            replay["write_count"] = 0
        return replay
    if claim == "busy":
        return _result("owner_lease_active", idempotency_result="in_flight")

    try:
        try:
            if owner.authorize(invocation) is not True:
                return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
        except Exception:
            return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
        try:
            owner_result = owner.execute(invocation)
        except Exception:
            outcome = _result(
                "uncertain_outcome",
                write_count=None,
                uncertain_outcome=True,
                idempotency_result="unknown",
            )
            if capability.idempotency is not IdempotencyMode.READ_ONLY:
                _remember_dispatch(idempotency_key, outcome)
            return outcome
        try:
            readback = _normalize_readback(
                owner.readback(invocation, owner_result),
                capability.result_schema_id,
                action,
            )
        except Exception:
            readback = None
        if readback is None:
            outcome = _result(
                "uncertain_outcome",
                write_count=None,
                uncertain_outcome=True,
                idempotency_result="unknown",
            )
            if capability.idempotency is not IdempotencyMode.READ_ONLY:
                _remember_dispatch(idempotency_key, outcome)
            return outcome
        read_only = capability.idempotency is IdempotencyMode.READ_ONLY
        outcome = _result(
            CapabilityDecision.ALLOW,
            action=action,
            result=readback,
            readback="passed",
            write_count=0 if read_only else 1,
            idempotency_result="read_only" if read_only else "inserted",
        )
        if not read_only:
            _remember_dispatch(idempotency_key, outcome)
        return outcome
    finally:
        _release_dispatch(idempotency_key)


def _validated_local_request(action: object, kwargs: Mapping[str, Any]) -> tuple[_ActionSpec, dict[str, Any], str] | None:
    if type(action) is not str:
        return None
    spec = _ACTION_SPECS.get(action)
    if spec is None or type(kwargs) is not dict:
        return None
    payload = dict(kwargs)
    expected = spec.fields
    if action == "medication_intake_record":
        mode = payload.get("occurrence_mode")
        expected = (
            frozenset({"medication_refs", "occurrence_mode"})
            if mode == "source_time"
            else frozenset({"medication_refs", "occurrence_mode", "occurred_on"})
            if mode == "local_date"
            else frozenset({"__invalid__"})
        )
    if frozenset(payload) != expected:
        return None
    if "payload_name" in payload:
        name = payload["payload_name"]
        if type(name) is not str or _PAYLOAD_NAME_RE.fullmatch(name) is None or ".." in name:
            return None
    if "pending_id" in payload:
        pending_id = payload["pending_id"]
        if type(pending_id) is not str or _PENDING_ID_RE.fullmatch(pending_id) is None:
            return None
    if "reason_code" in payload and payload["reason_code"] not in _REASON_CODES:
        return None
    if "run_id" in payload:
        run_id = payload["run_id"]
        if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
            return None
    if action == "medication_intake_record":
        refs = payload.get("medication_refs")
        if (
            type(refs) is not list
            or not 1 <= len(refs) <= 64
            or any(type(ref) is not str or _MEDICATION_REF_RE.fullmatch(ref) is None for ref in refs)
            or len(refs) != len(set(refs))
        ):
            return None
        occurred_on = payload.get("occurred_on")
        if payload["occurrence_mode"] == "local_date":
            if type(occurred_on) is not str or _DATE_RE.fullmatch(occurred_on) is None:
                return None
            try:
                date.fromisoformat(occurred_on)
            except ValueError:
                return None
    target = payload.get("pending_id") if action.startswith("pending_") else spec.target
    if type(target) is not str or not target:
        return None
    return spec, payload, target


def registered_local_workflow(action: object, **kwargs: Any) -> dict[str, Any]:
    validated = _validated_local_request(action, kwargs)
    if validated is None:
        if type(action) is str and action not in _ACTION_SPECS:
            return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
        return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    spec, payload, target = validated
    return _dispatch_registered_action(
        capability_id=spec.capability_id,
        action=str(action),
        effect=spec.effect,
        target=target,
        payload=payload,
        authority_mode=AuthorityMode.FOREGROUND_CURRENT_TURN,
        config_key="registered_workflow",
    )


def _handle_registered_local_workflow(args: Any, **_context: Any) -> str:
    if type(args) is not dict or "idempotency_key" in args or "authority" in args:
        result = _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    else:
        try:
            result = registered_local_workflow(**args)
        except TypeError:
            result = _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    return json.dumps(result, ensure_ascii=True, sort_keys=True)


registry.register(
    name="registered_local_workflow",
    toolset="registered-workflow",
    schema=REGISTERED_LOCAL_WORKFLOW_SCHEMA,
    handler=_handle_registered_local_workflow,
    check_fn=check_registered_workflow_requirements,
    description=REGISTERED_LOCAL_WORKFLOW_SCHEMA["description"],
    emoji="🔒",
)


__all__ = [
    "ExternalPluginContract",
    "ExternalWorkflowOwner",
    "REGISTERED_LOCAL_WORKFLOW_SCHEMA",
    "WorkflowInvocation",
    "build_deterministic_idempotency_key",
    "check_registered_workflow_requirements",
    "constant_readback_fingerprint",
    "register_external_workflow_owner",
    "registered_local_workflow",
    "scoped_external_workflow_owner",
    "unregister_external_workflow_owner",
    "validate_external_plugin_contract",
]
