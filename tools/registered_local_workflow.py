"""Service-gated typed dispatcher for registered local workflows.

This module never opens approval UI and never accepts paths, commands, SQL,
authority claims, or provenance from model input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    WorkflowEffect,
    evaluate_registered_capability,
)
from gateway.session_context import get_session_env
from tools.registry import registry
from tools.workflow_authority import (
    get_current_turn_user_authority,
    matches_active_workflow_turn,
)

_PENDING_ID_RE = re.compile(r"^kp_[a-f0-9]{16}$")
_PAYLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.json$")
_LIFELOG_ROOT = Path.home() / ".hermes" / "ops" / "state" / "lifelog"
_DIET_TARGET = "person_park_sanghyun:diet"
_ACTIONS = frozenset(
    {
        "diet_intake_record",
        "pending_read",
        "pending_soft_delete",
        "pending_restore",
    }
)
_REASON_CODES = frozenset({"user_dismissed", "superseded", "cleanup_confirmed"})
_DEPENDENCY_DIGESTS: dict[Path, str] = {}

REGISTERED_LOCAL_WORKFLOW_SCHEMA = {
    "name": "registered_local_workflow",
    "description": "Execute one registered local low-risk workflow with closed validation.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(_ACTIONS)},
            "pending_id": {"type": "string", "pattern": "^kp_[a-f0-9]{16}$"},
            "reason_code": {"type": "string", "enum": sorted(_REASON_CODES)},
            "payload_name": {
                "type": "string",
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\\.json$",
            },

        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _feature_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        block = (load_config() or {}).get("registered_workflow") or {}
        return isinstance(block, dict) and block.get("enabled") is True
    except Exception:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _pending_dependencies_ready() -> bool:
    try:
        from gateway.kanban_intake import (
            PendingKanbanStore,
            parse_config,
            transition_audit_ready,
        )
        from hermes_cli.config import load_config

        store = PendingKanbanStore(parse_config(load_config()).store_path)
        wal_path = store.path.with_name(store.path.name + "-wal")
        if wal_path.exists() and wal_path.stat().st_size != 0:
            return False
        before_digest = _sha256(store.path)
        connection = sqlite3.connect(
            store.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            ready = transition_audit_ready(connection)
        finally:
            connection.close()
        wal_stable = not wal_path.exists() or wal_path.stat().st_size == 0
        return ready and wal_stable and _sha256(store.path) == before_digest and all(
            hasattr(store, name)
            for name in ("registered_projection", "registered_soft_delete", "registered_restore")
        )
    except Exception:
        return False


def _diet_dependencies_ready() -> bool:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        required = (
            root / "scripts" / "run_registered_recorder.py",
            root / "scripts" / "record_diet_intake.py",
            root / "scripts" / "validate_lifelog.py",
            root / "config" / "recorder-registry.json",
            root / "lifelog.db",
            root / ".runtime-inputs" / "diet-intake",
        )
        return all(
            not path.is_symlink() and (path.is_file() or path.is_dir())
            for path in required
        )
    except Exception:
        return False


def _dependencies_ready(action: str) -> bool:
    if action == "diet_intake_record":
        return _diet_dependencies_ready()
    if action in {"pending_read", "pending_soft_delete", "pending_restore"}:
        return _pending_dependencies_ready()
    return _pending_dependencies_ready() or _diet_dependencies_ready()


def _owner_ready(action: str) -> bool:
    return _feature_enabled() and _dependencies_ready(action)


def check_registered_workflow_requirements() -> bool:
    return _feature_enabled() and _dependencies_ready("*")


def _result(decision: CapabilityDecision | str, **extra: Any) -> dict[str, Any]:
    value = decision.value if isinstance(decision, CapabilityDecision) else str(decision)
    result: dict[str, Any] = {
        "schema": "registered-local-workflow-result/v1",
        "decision": value,
        "prompt_count": 0,
        "write_count": 0,
        "action_id": f"rwa_{secrets.token_hex(16)}",
    }
    result.update(extra)
    return result


def _authority_matches_session(authority: Any) -> bool:
    if not matches_active_workflow_turn(authority):
        return False
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if not platform:
        return authority.platform_scope.strip().lower() in {"manual", "cli", "tui"}
    if platform in {"background", "cron", "delegate", "review", "subagent", "webhook"}:
        return False
    session_id = get_session_env("HERMES_SESSION_ID", "").strip()
    if not session_id:
        return False
    return hmac.compare_digest(
        str(authority.platform_scope).strip().lower(), platform
    ) and hmac.compare_digest(str(authority.session_scope), session_id)



def _pending_binding() -> dict[str, str] | None:
    values = {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
        "session_key": get_session_env("HERMES_SESSION_KEY", "").strip(),
    }
    if values["platform"] in {"", "manual", "background", "cron", "delegate", "review", "subagent", "webhook"}:
        return None
    if not all(values[key] for key in ("chat_id", "user_id", "session_key")):
        return None
    return values


def _pending_owner_action(*, action: str, pending_id: str, reason_code: str | None, authority: Any) -> dict[str, Any]:
    from gateway.kanban_intake import PendingKanbanStore, SourceBinding, parse_config
    from hermes_cli.config import load_config

    binding_data = _pending_binding()
    if binding_data is None:
        raise PermissionError("target unavailable")
    binding = SourceBinding(
        platform=binding_data["platform"],
        chat_id=binding_data["chat_id"],
        thread_id=binding_data["thread_id"] or None,
        user_id=binding_data["user_id"],
        session_key=binding_data["session_key"],
    )
    store = PendingKanbanStore(parse_config(load_config()).store_path)
    if action == "pending_read":
        return store.registered_projection(pending_id, binding)
    invocation_key = hashlib.sha256(
        json.dumps(
            {
                "schema": "pending-external-transition/v1",
                "operation": action,
                "pending_target": hashlib.sha256(pending_id.encode()).hexdigest(),
                "source_event": authority.source_event_fingerprint,
                "reason": reason_code or "user_restored",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if action == "pending_soft_delete":
        return store.registered_soft_delete(
            pending_id, binding, reason_code=reason_code, invocation_key=invocation_key
        )
    return store.registered_restore(pending_id, binding, invocation_key=invocation_key)


def _parse_diet_recorder_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if completed.returncode != 0 or completed.stderr or len(completed.stdout.encode("utf-8")) > 32768:
        raise RuntimeError("registered diet owner rejected the request")
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("registered diet owner returned invalid JSON") from exc
    expected_keys = {
        "schema",
        "recorder_id",
        "exit_status",
        "validation_status",
        "idempotency_result",
        "event_ids",
        "dry_run",
    }
    if (
        type(result) is not dict
        or set(result) != expected_keys
        or result.get("schema") != "registered-recorder-result/v1"
        or result.get("recorder_id") != "diet_intake.v1"
        or result.get("exit_status") != 0
        or type(result.get("dry_run")) is not bool
    ):
        raise RuntimeError("registered diet owner returned an unexpected result")
    event_ids = result.get("event_ids")
    if (
        type(event_ids) is not list
        or not event_ids
        or len(event_ids) != len(set(event_ids))
        or any(
            type(event_id) is not str
            or re.fullmatch(r"diet_v1_[a-f0-9]{16}", event_id) is None
            for event_id in event_ids
        )
    ):
        raise RuntimeError("registered diet owner returned invalid event IDs")
    return result


def _invoke_diet_dispatcher(*, root: Path, dispatcher: Path, payload: Path, dry_run: bool) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(dispatcher),
            "diet_intake.v1",
            "--payload",
            str(payload),
            "--dry-run",
            "true" if dry_run else "false",
            "--json",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        shell=False,
        timeout=180,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )
    return _parse_diet_recorder_result(completed)


def _diet_payload_matches_session(payload_name: str) -> bool:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        payload = root / ".runtime-inputs" / "diet-intake" / payload_name
        if payload.is_symlink() or not payload.is_file() or payload.stat().st_size > 65536:
            return False
        document = json.loads(payload.read_text(encoding="utf-8"))
        source = document.get("source") if type(document) is dict else None
        if type(source) is not dict:
            return False
        expected = {
            "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
            "channel_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
            "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
        }
        return bool(
            expected["platform"]
            and expected["channel_id"]
            and expected["message_id"]
            and str(source.get("platform", "")).strip().lower() == expected["platform"]
            and str(source.get("channel_id", "")).strip() == expected["channel_id"]
            and str(source.get("thread_id", "") or "").strip() == expected["thread_id"]
            and str(source.get("message_id", "")).strip() == expected["message_id"]
        )
    except Exception:
        return False


def _diet_owner_action(*, payload_name: str) -> dict[str, Any]:
    root = _LIFELOG_ROOT.resolve(strict=True)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    payload = root / ".runtime-inputs" / "diet-intake" / payload_name
    if dispatcher.is_symlink() or not dispatcher.is_file():
        raise RuntimeError("registered diet owner is unavailable")
    dry_result = _invoke_diet_dispatcher(
        root=root, dispatcher=dispatcher, payload=payload, dry_run=True
    )
    if (
        dry_result.get("dry_run") is not True
        or dry_result.get("validation_status") != "payload_validated"
        or dry_result.get("idempotency_result") != "not_applicable_dry_run"
    ):
        raise RuntimeError("registered diet dry-run evidence is invalid")
    live_result = _invoke_diet_dispatcher(
        root=root, dispatcher=dispatcher, payload=payload, dry_run=False
    )
    if (
        live_result.get("dry_run") is not False
        or live_result.get("validation_status") != "validator_and_readback_passed"
        or live_result.get("idempotency_result") not in {"inserted", "existing"}
        or live_result.get("event_ids") != dry_result.get("event_ids")
    ):
        raise RuntimeError("registered diet live evidence is invalid")
    return live_result


def registered_local_workflow(action: str, **kwargs: Any) -> dict[str, Any]:
    if action not in _ACTIONS:
        return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
    supplied = {"action", *kwargs}
    expected = {
        "diet_intake_record": {"action", "payload_name"},
        "pending_read": {"action", "pending_id"},
        "pending_soft_delete": {"action", "pending_id", "reason_code"},
        "pending_restore": {"action", "pending_id"},
    }[action]
    if supplied != expected:
        return _result(CapabilityDecision.DENY_SCHEMA_INVALID)

    if action == "diet_intake_record":
        payload_name = kwargs.get("payload_name")
        if type(payload_name) is not str or _PAYLOAD_NAME_RE.fullmatch(payload_name) is None:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        target = _DIET_TARGET
        capability_id = "lifelog.diet-intake.v1"
        effect = WorkflowEffect.CREATE
    else:
        pending_id = kwargs.get("pending_id")
        if type(pending_id) is not str or _PENDING_ID_RE.fullmatch(pending_id) is None:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        if action == "pending_soft_delete" and kwargs.get("reason_code") not in _REASON_CODES:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        target = pending_id
        capability_id = "kanban-intake.pending-soft-delete.v1"
        effect = {
            "pending_read": WorkflowEffect.READ,
            "pending_soft_delete": WorkflowEffect.SOFT_DELETE,
            "pending_restore": WorkflowEffect.RESTORE,
        }[action]

    authority = get_current_turn_user_authority()
    authority_active = bool(
        authority is not None
        and authority.source_event_fingerprint
        and _authority_matches_session(authority)
    )
    required_class = (
        "trusted_local_record"
        if action == "diet_intake_record"
        else "registered_soft_delete"
    )
    authority_valid = bool(
        authority_active
        and authority is not None
        and required_class in authority.allowed_action_classes
        and authority.allows_operation_target(action, target)
    )
    target_valid = _pending_binding() is not None and (
        action != "diet_intake_record"
        or _diet_payload_matches_session(kwargs["payload_name"])
    )
    policy_decision = evaluate_registered_capability(
        capability_id,
        action,
        effect,
        schema_valid=True,
        authority_mode=(
            AuthorityMode.FOREGROUND_CURRENT_TURN if authority_valid else None
        ),
        owner_ready=_owner_ready(action),
        target_valid=target_valid,
        restore_contract_valid=True,
    )
    if policy_decision is not CapabilityDecision.ALLOW:
        return _result(policy_decision)

    if action == "diet_intake_record":
        try:
            owner = _diet_owner_action(payload_name=kwargs["payload_name"])
            idempotency_result = owner["idempotency_result"]
            return _result(
                CapabilityDecision.ALLOW,
                write_count=1 if idempotency_result == "inserted" else 0,
                idempotency_result=idempotency_result,
                validation_status=owner["validation_status"],
                event_ids=owner["event_ids"],
                readback="passed",
            )
        except Exception:
            return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)

    try:
        owner = _pending_owner_action(
            action=action,
            pending_id=kwargs["pending_id"],
            reason_code=kwargs.get("reason_code"),
            authority=authority,
        )
        return _result(
            CapabilityDecision.ALLOW,
            write_count=(
                0
                if action == "pending_read" or bool(owner.get("replayed"))
                else 1
            ),
            idempotency_result=(
                "existing" if bool(owner.get("replayed")) else "inserted"
            ),
            readback="passed",
            pending_status=owner.get("status"),
            replayed=bool(owner.get("replayed")),
            reason_code=owner.get("reason_code"),
        )
    except PermissionError:
        return _result(CapabilityDecision.DENY_TARGET_MISMATCH)
    except (sqlite3.IntegrityError, ValueError):
        return _result(CapabilityDecision.DENY_SOFT_DELETE_NOT_RESTORABLE)
    except Exception:
        return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)


def _handle_registered_local_workflow(args: Any, **_context: Any) -> str:
    if type(args) is not dict:
        return json.dumps(_result(CapabilityDecision.DENY_SCHEMA_INVALID), sort_keys=True)
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
