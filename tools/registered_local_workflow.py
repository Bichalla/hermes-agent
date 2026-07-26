"""Service-gated typed dispatcher for registered local workflows.

This module never opens approval UI and never accepts paths, commands, SQL,
authority claims, or provenance from model input.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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
    is_host_issued_current_turn_authority,
    matches_active_workflow_turn,
)

_PENDING_ID_RE = re.compile(r"^kp_[a-f0-9]{16}$")
_PAYLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.json$")
_SEMANTIC_DEBUG_RUN_ID_RE = re.compile(
    r"^semantic-debug-[a-z0-9][a-z0-9-]{4,100}$"
)
_LIFELOG_ROOT = Path.home() / ".hermes" / "ops" / "state" / "lifelog"
_CHILDCARE_TARGET_PREFIX = "person_park_haesoo:childcare"
_DIET_TARGET = "person_park_sanghyun:diet"
_ACTIONS = frozenset(
    {
        "childcare_event_record",
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
        "diet_intake_record",
        "pending_read",
        "pending_soft_delete",
        "pending_restore",
        "semantic_debug_issue",
    }
)
_REASON_CODES = frozenset({"user_dismissed", "superseded", "cleanup_confirmed"})
_DEPENDENCY_DIGESTS: dict[Path, str] = {}


@dataclass(frozen=True)
class ChildcarePayloadBinding:
    path: Path
    digest: str
    target: str
    receipt_key: str
    payload_bytes: bytes


@dataclass(frozen=True, slots=True)
class _CompanyWorkOSOwnerConfig:
    project_root: Path
    owner_script: Path
    manifest: Path
    interpreter: Path
    interpreter_link_target: str
    interpreter_link_identity: tuple[int, int]
    interpreter_resolved: Path
    interpreter_target_identity: tuple[int, int]
    interpreter_sha256: str


_PRODUCTION_COMPANY_WORK_OS_OWNER = _CompanyWorkOSOwnerConfig(
    project_root=Path("/Users/honbul/.hermes/ops/state/company_work_os"),
    owner_script=Path(
        "/Users/honbul/.hermes/ops/state/company_work_os/"
        "scripts/run_registered_initial_seed.py"
    ),
    manifest=Path(
        "/Users/honbul/.hermes/ops/state/company_work_os/registered-owner-manifest.json"
    ),
    interpreter=Path("/Users/honbul/.hermes/hermes-agent/venv/bin/python"),
    interpreter_link_target=(
        "/Users/honbul/.local/share/uv/python/"
        "cpython-3.11-macos-aarch64-none/bin/python3.11"
    ),
    interpreter_link_identity=(16777229, 273357),
    interpreter_resolved=Path(
        "/Users/honbul/.local/share/uv/python/"
        "cpython-3.11.15-macos-aarch64-none/bin/python3.11"
    ),
    interpreter_target_identity=(16777229, 261735),
    interpreter_sha256=(
        "4c78423e7d5986362ac04df40edb18cdd1174f9818d653402e3abbd2a5bbf793"
    ),
)
_COMPANY_WORK_OS_TARGET = "company-work-os:canonical-initial-seed"
_COMPANY_OWNER_MANIFEST_SCHEMA = "company-work-os-registered-owner-manifest/v1"
_COMPANY_OWNER_PREVIEW_SCHEMA = "company-work-os/initial-seed-preview-result/v1"
_COMPANY_OWNER_RECORD_SCHEMA = "company-work-os/initial-seed-record-result/v1"
_COMPANY_OWNER_MAX_STDOUT_BYTES = 16_384
_COMPANY_OWNER_UNAVAILABLE = "registered company owner unavailable"
_COMPANY_COUNT_KEYS = (
    "org_units",
    "persons",
    "positions",
    "assignments",
    "command_receipts",
    "change_events",
)
_COMPANY_ZERO_COUNTS = (0, 0, 0, 0, 0, 0)
_COMPANY_INSERT_COUNTS = (2, 1, 2, 2, 1, 1)
_COMPANY_BLOCKED_PLATFORMS = frozenset(
    {"background", "cron", "delegate", "review", "subagent", "webhook"}
)


def _workflow_branch(
    action: str, properties: tuple[tuple[str, dict[str, Any]], ...]
) -> dict[str, Any]:
    branch_properties: dict[str, Any] = {"action": {"type": "string", "const": action}}
    branch_properties.update(properties)
    return {
        "type": "object",
        "properties": branch_properties,
        "required": list(branch_properties),
        "additionalProperties": False,
    }

REGISTERED_LOCAL_WORKFLOW_SCHEMA = {
    "name": "registered_local_workflow",
    "description": "Execute one registered local low-risk workflow with closed validation.",
    "parameters": {
        "type": "object",
        "oneOf": [
            _workflow_branch(
                "childcare_event_record",
                (("payload_name", {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\\.json$",
                }),),
            ),
            _workflow_branch(
                "diet_intake_record",
                (("payload_name", {
                    "type": "string",
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\\.json$",
                }),),
            ),
            _workflow_branch(
                "pending_read",
                (("pending_id", {"type": "string", "pattern": "^kp_[a-f0-9]{16}$"}),),
            ),
            _workflow_branch(
                "pending_soft_delete",
                (
                    ("pending_id", {"type": "string", "pattern": "^kp_[a-f0-9]{16}$"}),
                    ("reason_code", {"type": "string", "enum": sorted(_REASON_CODES)}),
                ),
            ),
            _workflow_branch(
                "pending_restore",
                (("pending_id", {"type": "string", "pattern": "^kp_[a-f0-9]{16}$"}),),
            ),
            _workflow_branch(
                "semantic_debug_issue",
                (("run_id", {
                    "type": "string",
                    "pattern": "^semantic-debug-[a-z0-9][a-z0-9-]{4,100}$",
                }),),
            ),
            _workflow_branch("company_work_os_initial_seed_preview", ()),
            _workflow_branch("company_work_os_initial_seed_record", ()),
        ],
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


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _same_file_identity(metadata: os.stat_result, expected: tuple[int, int]) -> bool:
    return (metadata.st_dev, metadata.st_ino) == expected


def _owned_regular_file(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    if not (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
    ):
        raise ValueError
    return metadata


def _verify_company_interpreter(config: _CompanyWorkOSOwnerConfig) -> None:
    if (
        type(config.interpreter) is not type(Path())
        or type(config.interpreter_resolved) is not type(Path())
        or not config.interpreter.is_absolute()
        or not config.interpreter_resolved.is_absolute()
        or type(config.interpreter_link_target) is not str
        or type(config.interpreter_link_identity) is not tuple
        or type(config.interpreter_target_identity) is not tuple
        or type(config.interpreter_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", config.interpreter_sha256) is None
    ):
        raise ValueError
    link_metadata = os.lstat(config.interpreter)
    if not (
        stat.S_ISLNK(link_metadata.st_mode)
        and link_metadata.st_uid == os.getuid()
        and _same_file_identity(link_metadata, config.interpreter_link_identity)
        and os.readlink(config.interpreter) == config.interpreter_link_target
    ):
        raise ValueError
    resolved = config.interpreter.resolve(strict=True)
    target_metadata = os.stat(resolved)
    if not (
        resolved == config.interpreter_resolved
        and stat.S_ISREG(target_metadata.st_mode)
        and target_metadata.st_uid == os.getuid()
        and _same_file_identity(target_metadata, config.interpreter_target_identity)
        and hmac.compare_digest(_sha256(resolved), config.interpreter_sha256)
    ):
        raise ValueError


def _read_company_owner_manifest(config: _CompanyWorkOSOwnerConfig) -> dict[str, Any]:
    if type(config) is not _CompanyWorkOSOwnerConfig:
        raise ValueError
    concrete_path_type = type(Path())
    if any(
        type(path) is not concrete_path_type or not path.is_absolute()
        for path in (config.project_root, config.owner_script, config.manifest)
    ):
        raise ValueError
    root_metadata = os.lstat(config.project_root)
    if not (
        stat.S_ISDIR(root_metadata.st_mode)
        and not stat.S_ISLNK(root_metadata.st_mode)
        and root_metadata.st_uid == os.getuid()
        and config.owner_script
        == config.project_root / "scripts/run_registered_initial_seed.py"
        and config.manifest
        == config.project_root / "registered-owner-manifest.json"
    ):
        raise ValueError
    manifest_metadata = _owned_regular_file(config.manifest)
    if not 0 < manifest_metadata.st_size <= 65_536:
        raise ValueError
    raw = config.manifest.read_bytes()
    if len(raw) != manifest_metadata.st_size:
        raise ValueError
    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_closed_json_object,
        parse_constant=_reject_json_constant,
    )
    if (
        type(document) is not dict
        or tuple(document)
        != ("schema", "version", "project_relative_root", "python_files")
        or document.get("schema") != _COMPANY_OWNER_MANIFEST_SCHEMA
        or type(document.get("version")) is not int
        or document.get("version") != 1
        or document.get("project_relative_root") != "."
        or type(document.get("python_files")) is not list
    ):
        raise ValueError
    return document


def _verify_company_work_os_owner(config: _CompanyWorkOSOwnerConfig) -> None:
    try:
        document = _read_company_owner_manifest(config)
        entries = document["python_files"]
        paths: list[str] = []
        for entry in entries:
            if (
                type(entry) is not dict
                or tuple(entry) != ("path", "sha256")
                or type(entry.get("path")) is not str
                or type(entry.get("sha256")) is not str
            ):
                raise ValueError
            relative = entry["path"]
            digest = entry["sha256"]
            relative_path = Path(relative)
            if (
                relative_path.is_absolute()
                or relative_path.as_posix() != relative
                or ".." in relative_path.parts
                or relative_path.suffix != ".py"
                or not (
                    relative.startswith("company_work_os/")
                    or relative == "scripts/run_registered_initial_seed.py"
                )
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise ValueError
            paths.append(relative)
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError
        inventory = sorted(
            path.relative_to(config.project_root).as_posix()
            for path in (config.project_root / "company_work_os").rglob("*.py")
        )
        inventory.append("scripts/run_registered_initial_seed.py")
        inventory.sort()
        if paths != inventory:
            raise ValueError
        for entry in entries:
            source = config.project_root / entry["path"]
            _owned_regular_file(source)
            if not hmac.compare_digest(_sha256(source), entry["sha256"]):
                raise ValueError
        _verify_company_interpreter(config)
    except Exception:
        raise RuntimeError(_COMPANY_OWNER_UNAVAILABLE) from None


def _company_work_os_dependencies_ready() -> bool:
    try:
        _verify_company_work_os_owner(_PRODUCTION_COMPANY_WORK_OS_OWNER)
        return True
    except Exception:
        return False


def _exact_company_count_map(value: Any) -> tuple[int, ...]:
    if type(value) is not dict or tuple(value) != _COMPANY_COUNT_KEYS:
        raise ValueError
    counts = tuple(value[key] for key in _COMPANY_COUNT_KEYS)
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError
    return counts


def _exact_sha256(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _validate_company_preview_result(result: Any) -> dict[str, Any]:
    expected_keys = (
        "schema",
        "outcome",
        "target_token",
        "payload_sha256",
        "semantic_sha256",
        "migration_versions",
        "current_counts",
        "expected_delta",
    )
    if (
        type(result) is not dict
        or tuple(result) != expected_keys
        or result.get("schema") != _COMPANY_OWNER_PREVIEW_SCHEMA
        or result.get("target_token") != _COMPANY_WORK_OS_TARGET
        or not _exact_sha256(result.get("payload_sha256"))
        or not _exact_sha256(result.get("semantic_sha256"))
        or type(result.get("migration_versions")) is not list
        or result.get("migration_versions") != [1, 2, 3, 4, 5, 6, 7]
        or any(type(version) is not int for version in result["migration_versions"])
    ):
        raise ValueError
    current = _exact_company_count_map(result.get("current_counts"))
    expected = _exact_company_count_map(result.get("expected_delta"))
    outcome = result.get("outcome")
    valid = (
        outcome == "ready_insert"
        and current == _COMPANY_ZERO_COUNTS
        and expected == _COMPANY_INSERT_COUNTS
    ) or (
        outcome == "exact_replay"
        and current == _COMPANY_INSERT_COUNTS
        and expected == _COMPANY_ZERO_COUNTS
    ) or (outcome == "conflict" and expected == _COMPANY_ZERO_COUNTS)
    if not valid:
        raise ValueError
    return result


def _validate_company_record_result(result: Any) -> dict[str, Any]:
    expected_keys = (
        "schema",
        "outcome",
        "target_token",
        "semantic_sha256",
        "committed_delta",
        "receipt_count",
        "event_count",
    )
    if (
        type(result) is not dict
        or tuple(result) != expected_keys
        or result.get("schema") != _COMPANY_OWNER_RECORD_SCHEMA
        or result.get("target_token") != _COMPANY_WORK_OS_TARGET
        or not _exact_sha256(result.get("semantic_sha256"))
        or type(result.get("outcome")) is not str
    ):
        raise ValueError
    outcome = result["outcome"]
    if outcome == "manual_recovery_required":
        if not (
            result.get("committed_delta") is None
            and result.get("receipt_count") is None
            and result.get("event_count") is None
        ):
            raise ValueError
        return result
    delta = _exact_company_count_map(result.get("committed_delta"))
    if outcome == "inserted":
        valid = (
            delta == _COMPANY_INSERT_COUNTS
            and type(result.get("receipt_count")) is int
            and result.get("receipt_count") == 1
            and type(result.get("event_count")) is int
            and result.get("event_count") == 1
        )
    elif outcome == "existing":
        valid = (
            delta == _COMPANY_ZERO_COUNTS
            and type(result.get("receipt_count")) is int
            and result.get("receipt_count") == 1
            and type(result.get("event_count")) is int
            and result.get("event_count") == 1
        )
    elif outcome == "conflict":
        valid = (
            delta == _COMPANY_ZERO_COUNTS
            and result.get("receipt_count") is None
            and result.get("event_count") is None
        )
    else:
        valid = False
    if not valid:
        raise ValueError
    return result


def _validate_company_work_os_owner_result(
    action: str, result: Any
) -> dict[str, Any]:
    try:
        if action == "company_work_os_initial_seed_preview":
            return _validate_company_preview_result(result)
        if action == "company_work_os_initial_seed_record":
            return _validate_company_record_result(result)
        raise ValueError
    except Exception:
        raise RuntimeError(_COMPANY_OWNER_UNAVAILABLE) from None


def _invoke_company_work_os_owner_at(
    *,
    action: str,
    config: _CompanyWorkOSOwnerConfig,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    try:
        child_action = {
            "company_work_os_initial_seed_preview": "preview",
            "company_work_os_initial_seed_record": "record",
        }[action]
        _verify_company_work_os_owner(config)
        argv = [
            str(config.interpreter),
            "-I",
            "-B",
            str(config.owner_script),
            child_action,
        ]
        completed = runner(
            argv,
            cwd=config.project_root,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "HOME": "/Users/honbul",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            shell=False,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        if (
            type(completed.returncode) is not int
            or completed.returncode != 0
            or type(stdout) is not bytes
            or not stdout
            or len(stdout) > _COMPANY_OWNER_MAX_STDOUT_BYTES
            or type(stderr) is not bytes
            or stderr != b""
        ):
            raise ValueError
        result = json.loads(
            stdout.decode("utf-8"),
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_json_constant,
        )
        return _validate_company_work_os_owner_result(action, result)
    except Exception:
        raise RuntimeError(_COMPANY_OWNER_UNAVAILABLE) from None


def _company_work_os_owner_action(action: str) -> dict[str, Any]:
    return _invoke_company_work_os_owner_at(
        action=action,
        config=_PRODUCTION_COMPANY_WORK_OS_OWNER,
        runner=subprocess.run,
    )



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


def _childcare_dependencies_ready() -> bool:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        required = (
            root / "scripts" / "run_registered_recorder.py",
            root / "scripts" / "record_childcare_event.py",
            root / "scripts" / "validate_lifelog.py",
            root / "config" / "recorder-registry.json",
            root / "lifelog.db",
            root / ".runtime-inputs" / "childcare-event",
        )
        return all(
            not path.is_symlink() and (path.is_file() or path.is_dir())
            for path in required
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


def _semantic_debug_dependencies_ready() -> bool:
    try:
        home = Path.home() / ".hermes"
        required = (
            home / "scripts" / "issue_lifelog_context_broker_provider_transport_probe.py",
            home / "scripts" / "probe_lifelog_context_broker_provider_transport.py",
            home / "reviews" / "lifelog-context-broker-v3" / "semantic-debug-genesis-v1.json",
        )
        return all(path.is_file() and not path.is_symlink() for path in required)
    except Exception:
        return False


def _dependencies_ready(action: str) -> bool:
    if action in {
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
    }:
        return _company_work_os_dependencies_ready()
    if action == "childcare_event_record":
        return _childcare_dependencies_ready()
    if action == "diet_intake_record":
        return _diet_dependencies_ready()
    if action == "semantic_debug_issue":
        return _semantic_debug_dependencies_ready()
    if action in {"pending_read", "pending_soft_delete", "pending_restore"}:
        return _pending_dependencies_ready()
    return (
        _pending_dependencies_ready()
        or _childcare_dependencies_ready()
        or _diet_dependencies_ready()
    )


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


def _parse_childcare_recorder_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if completed.returncode != 0 or completed.stderr or len(completed.stdout.encode("utf-8")) > 32768:
        raise RuntimeError("registered childcare owner rejected the request")
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("registered childcare owner returned invalid JSON") from exc
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
        or result.get("recorder_id") != "childcare_event.v1"
        or result.get("exit_status") != 0
        or type(result.get("dry_run")) is not bool
    ):
        raise RuntimeError("registered childcare owner returned an unexpected result")
    event_ids = result.get("event_ids")
    if (
        type(event_ids) is not list
        or len(event_ids) != 1
        or type(event_ids[0]) is not str
        or re.fullmatch(r"evt_childcare_v1_[a-f0-9]{16}", event_ids[0]) is None
    ):
        raise RuntimeError("registered childcare owner returned invalid event IDs")
    return result


def _invoke_childcare_dispatcher(
    *, root: Path, dispatcher: Path, payload: Path, dry_run: bool
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(dispatcher),
            "childcare_event.v1",
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
    return _parse_childcare_recorder_result(completed)


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


def _lifelog_payload_matches_session(payload_dir: str, payload_name: str) -> bool:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        payload = root / ".runtime-inputs" / payload_dir / payload_name
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


def _childcare_fact_kind(document: dict[str, Any]) -> str | None:
    category = str(document.get("category", "")).strip().casefold()
    subcategory = str(document.get("subcategory", "")).strip().casefold()
    metrics = document.get("metrics")
    metrics = metrics if type(metrics) is dict else {}
    kinds: set[str] = set()
    if "temperature_c" in metrics or any(
        token in subcategory for token in ("fever", "temperature")
    ):
        kinds.add("fever")
    if category == "medication" or any(
        token in subcategory for token in ("medication", "dose", "antipyretic")
    ):
        kinds.add("medication")
    if any(
        token in subcategory
        for token in (
            "clinical",
            "visit",
            "diagnosis",
            "treatment_plan",
            "medical_advice",
            "prescription",
        )
    ):
        kinds.add("clinical")
    if len(kinds) != 1:
        return None
    return next(iter(kinds))


def _childcare_payload_binding(payload_name: str) -> ChildcarePayloadBinding | None:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        path = root / ".runtime-inputs" / "childcare-event" / payload_name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return None
        payload_bytes = path.read_bytes()
        document = json.loads(payload_bytes)
        if (
            type(document) is not dict
            or "parent_impact" in document
            or document.get("child_person_id") != "person_park_haesoo"
        ):
            return None
        source = document.get("source")
        if type(source) is not dict:
            return None
        expected = {
            "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
            "channel_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
            "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
            "message_id": get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
        }
        if not (
            expected["platform"]
            and expected["channel_id"]
            and expected["message_id"]
            and str(source.get("platform", "")).strip().lower() == expected["platform"]
            and str(source.get("channel_id", "")).strip() == expected["channel_id"]
            and str(source.get("thread_id", "") or "").strip() == expected["thread_id"]
            and str(source.get("message_id", "")).strip() == expected["message_id"]
        ):
            return None
        kind = _childcare_fact_kind(document)
        if kind is None:
            return None
        target = f"{_CHILDCARE_TARGET_PREFIX}:{kind}"
        digest = hashlib.sha256(payload_bytes).hexdigest()
        identity = "\x1f".join(
            (
                expected["platform"],
                expected["channel_id"],
                expected["thread_id"],
                expected["message_id"],
                target,
            )
        )
        return ChildcarePayloadBinding(
            path=path,
            digest=digest,
            target=target,
            receipt_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            payload_bytes=payload_bytes,
        )
    except Exception:
        return None


def _diet_payload_matches_session(payload_name: str) -> bool:
    return _lifelog_payload_matches_session("diet-intake", payload_name)


def _write_private_file(path: Path, data: bytes, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _childcare_owner_action(*, binding: ChildcarePayloadBinding) -> dict[str, Any]:
    root = _LIFELOG_ROOT.resolve(strict=True)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    if dispatcher.is_symlink() or not dispatcher.is_file():
        raise RuntimeError("registered childcare owner is unavailable")
    current_bytes = binding.path.read_bytes()
    if (
        current_bytes != binding.payload_bytes
        or hashlib.sha256(current_bytes).hexdigest() != binding.digest
    ):
        raise RuntimeError("registered childcare payload changed before pinning")

    control_root = root / ".runtime-inputs" / "childcare-event" / ".owner"
    pinned_root = control_root / "pinned"
    receipt_root = control_root / "receipts"
    for directory in (control_root, pinned_root, receipt_root):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    pinned = pinned_root / f"{binding.digest}.json"
    if pinned.exists():
        if pinned.is_symlink() or pinned.read_bytes() != binding.payload_bytes:
            raise RuntimeError("registered childcare pinned payload conflict")
    else:
        _write_private_file(pinned, binding.payload_bytes, exclusive=True)

    receipt = receipt_root / f"{binding.receipt_key}.json"
    if receipt.exists():
        if receipt.is_symlink():
            raise RuntimeError("registered childcare receipt is invalid")
        prior = json.loads(receipt.read_text(encoding="utf-8"))
        if (
            type(prior) is not dict
            or prior.get("state") != "complete"
            or prior.get("digest") != binding.digest
            or prior.get("target") != binding.target
        ):
            raise RuntimeError("registered childcare source-event receipt conflict")
    else:
        pending = json.dumps(
            {"state": "pending", "digest": binding.digest, "target": binding.target},
            sort_keys=True,
        ).encode("utf-8")
        _write_private_file(receipt, pending, exclusive=True)

    if hashlib.sha256(pinned.read_bytes()).hexdigest() != binding.digest:
        raise RuntimeError("registered childcare pinned payload digest drift")
    dry_result = _invoke_childcare_dispatcher(
        root=root, dispatcher=dispatcher, payload=pinned, dry_run=True
    )
    if (
        dry_result.get("dry_run") is not True
        or dry_result.get("validation_status") != "payload_validated"
        or dry_result.get("idempotency_result") != "not_applicable_dry_run"
    ):
        raise RuntimeError("registered childcare dry-run evidence is invalid")
    if hashlib.sha256(pinned.read_bytes()).hexdigest() != binding.digest:
        raise RuntimeError("registered childcare pinned payload changed before live write")
    live_result = _invoke_childcare_dispatcher(
        root=root, dispatcher=dispatcher, payload=pinned, dry_run=False
    )
    if (
        live_result.get("dry_run") is not False
        or live_result.get("validation_status") != "validator_and_readback_passed"
        or live_result.get("idempotency_result") not in {"inserted", "existing"}
        or live_result.get("event_ids") != dry_result.get("event_ids")
    ):
        raise RuntimeError("registered childcare live evidence is invalid")
    complete = json.dumps(
        {
            "state": "complete",
            "digest": binding.digest,
            "target": binding.target,
            "event_ids": live_result["event_ids"],
        },
        sort_keys=True,
    ).encode("utf-8")
    _write_private_file(receipt, complete, exclusive=False)
    return live_result


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


def _semantic_debug_issue_owner_action(
    *, run_id: str, authority: Any,
) -> dict[str, Any]:
    binding = _pending_binding()
    if binding is None or binding['platform'] != 'discord':
        raise PermissionError('target unavailable')
    home = Path.home() / '.hermes'
    scripts = home / 'scripts'
    module_path = scripts / 'issue_lifelog_context_broker_provider_transport_probe.py'
    if module_path.is_symlink() or not module_path.is_file():
        raise RuntimeError('semantic debug issuer unavailable')
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        '_registered_semantic_debug_issuer', module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError('semantic debug issuer unavailable')
    issuer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = issuer
    spec.loader.exec_module(issuer)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    request = {
        'schema': issuer.REQUEST_SCHEMA,
        'approval': {
            'approval_id': f"foreground-{authority.source_event_fingerprint[:24]}",
            'approval_message_id': f"event:{authority.source_event_fingerprint}",
            'approval_session_id': authority.session_scope,
            'approved_run_id': run_id,
            'approved_at': now.isoformat().replace('+00:00', 'Z'),
            'approval_expires_at': (now + timedelta(minutes=15)).isoformat().replace('+00:00', 'Z'),
            'allowed_profile': 'default',
            'allowed_sender': binding['user_id'],
            'allowed_platform': 'discord',
            'allowed_chat': binding['thread_id'] or binding['chat_id'],
        },
        'probe_contract': {
            'provider': issuer.PROVIDER,
            'model': issuer.MODEL,
            'expected_wire_model': issuer.EXPECTED_WIRE_MODEL,
            'billing_mode': 'subscription_oauth',
            'max_total_calls': 1,
            'max_input_tokens': 1000,
            'max_output_tokens': 512,
            'request_timeout_seconds': 60,
            'synthetic_data_only': True,
            'family_context_included': False,
            'health_domain_included': True,
            'real_personal_data_included': False,
            'allowed_subject_scope': ['self'],
            'no_external_delivery': True,
            'probe_kind': issuer.probe.SEMANTIC_DEBUG_PROBE_KIND,
            'response_json_schema': issuer.probe.SEMANTIC_DEBUG_RESPONSE_SCHEMA,
        },
        'predecessor_cleanup': {
            'run_id': issuer.probe.SEMANTIC_DEBUG_PREDECESSOR_RUN_ID,
            'cleanup_evidence_path': str(issuer.probe.semantic_debug_predecessor_path()),
            'cleanup_evidence_canonical_sha256': (
                issuer.probe.SEMANTIC_DEBUG_PREDECESSOR_CANONICAL_SHA256
            ),
        },
    }
    payload = (
        json.dumps(
            request, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        ) + '\n'
    ).encode('utf-8')
    request_sha = hashlib.sha256(payload).hexdigest()
    issuer.hardened._secure_directory(issuer.PRIVATE_ROOT, private=True)
    issuer.hardened._secure_directory(issuer.REQUEST_ROOT, private=True)
    request_path = issuer.REQUEST_ROOT / f'{run_id}.json'
    try:
        issuer.hardened._exclusive_write(request_path, payload, 0o600)
    except ValueError as exc:
        if str(exc) != 'issuance_target_exists':
            raise
        existing = issuer._read_request_once(request_path, request_sha)
        if not hmac.compare_digest(existing, payload):
            raise RuntimeError('semantic debug request collision')
    preflight = issuer.preflight(request_path, request_sha)
    issued = issuer._issue_from_registered_foreground(request_path, request_sha)
    inspected = issuer.inspect_committed(run_id)
    if (
        preflight.get('approved_run_id') != run_id
        or issued.get('approved_run_id') != run_id
        or inspected.get('approved_run_id') != run_id
        or inspected.get('bundle_state') != 'committed'
    ):
        raise RuntimeError('semantic debug issuance readback failed')
    return {
        'run_id': run_id,
        'request_sha256': request_sha,
        'manifest_sha256': issued['manifest_sha256'],
        'bundle_state': 'committed',
        'provider_calls': 0,
        'credential_resolutions': 0,
        'network_actions': 0,
    }


def _company_work_os_authority_mode(action: str, authority: Any) -> AuthorityMode | None:
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform in _COMPANY_BLOCKED_PLATFORMS:
        return None
    authority_active = bool(
        authority is not None
        and authority.source_event_fingerprint
        and _authority_matches_session(authority)
    )
    if action == "company_work_os_initial_seed_preview":
        return (
            AuthorityMode.FOREGROUND_CURRENT_TURN
            if authority_active
            else AuthorityMode.LOCAL_READ_BOUNDARY
        )
    authority_valid = bool(
        authority_active
        and authority is not None
        and "trusted_local_record" in authority.allowed_action_classes
        and authority.allows_operation_target(action, _COMPANY_WORK_OS_TARGET)
    )
    return AuthorityMode.FOREGROUND_CURRENT_TURN if authority_valid else None


def _dispatch_company_work_os(action: str) -> dict[str, Any]:
    authority = get_current_turn_user_authority()
    authority_mode = _company_work_os_authority_mode(action, authority)
    preview = action == "company_work_os_initial_seed_preview"
    capability_id = (
        "company-work-os.initial-seed-preview.v1"
        if preview
        else "company-work-os.initial-seed-record.v1"
    )
    effect = WorkflowEffect.READ if preview else WorkflowEffect.CREATE
    policy_decision = evaluate_registered_capability(
        capability_id,
        action,
        effect,
        schema_valid=True,
        authority_mode=authority_mode,
        owner_ready=_owner_ready(action),
        target_valid=True,
        restore_contract_valid=True,
    )
    if policy_decision is not CapabilityDecision.ALLOW:
        return _result(policy_decision, owner_result=None)
    try:
        owner = _validate_company_work_os_owner_result(
            action, _company_work_os_owner_action(action)
        )
        outcome = owner["outcome"]
        if preview:
            return _result(
                CapabilityDecision.ALLOW,
                write_count=0,
                owner_result=owner,
            )
        if outcome == "manual_recovery_required":
            return _result(
                CapabilityDecision.HARD_BLOCK,
                write_count=None,
                owner_result=owner,
            )
        return _result(
            CapabilityDecision.ALLOW,
            write_count=9 if outcome == "inserted" else 0,
            owner_result=owner,
        )
    except Exception:
        return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE, owner_result=None)


def registered_local_workflow(action: str, **kwargs: Any) -> dict[str, Any]:
    if action not in _ACTIONS:
        return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
    supplied = {"action", *kwargs}
    expected = {
        "childcare_event_record": {"action", "payload_name"},
        "company_work_os_initial_seed_preview": {"action"},
        "company_work_os_initial_seed_record": {"action"},
        "diet_intake_record": {"action", "payload_name"},
        "pending_read": {"action", "pending_id"},
        "pending_soft_delete": {"action", "pending_id", "reason_code"},
        "pending_restore": {"action", "pending_id"},
        "semantic_debug_issue": {"action", "run_id"},
    }[action]
    if supplied != expected:
        return _result(
            CapabilityDecision.DENY_SCHEMA_INVALID,
            **(
                {"owner_result": None}
                if action.startswith("company_work_os_initial_seed_")
                else {}
            ),
        )

    if action in {
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
    }:
        return _dispatch_company_work_os(action)

    childcare_binding: ChildcarePayloadBinding | None = None
    if action in {"childcare_event_record", "diet_intake_record"}:
        payload_name = kwargs.get("payload_name")
        if type(payload_name) is not str or _PAYLOAD_NAME_RE.fullmatch(payload_name) is None:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        if action == "childcare_event_record":
            childcare_binding = _childcare_payload_binding(payload_name)
            target = (
                childcare_binding.target
                if childcare_binding is not None
                else f"{_CHILDCARE_TARGET_PREFIX}:invalid"
            )
            capability_id = "lifelog.childcare-event.v1"
        else:
            target = _DIET_TARGET
            capability_id = "lifelog.diet-intake.v1"
        effect = WorkflowEffect.CREATE
    elif action == "semantic_debug_issue":
        run_id = kwargs.get("run_id")
        if type(run_id) is not str or _SEMANTIC_DEBUG_RUN_ID_RE.fullmatch(run_id) is None:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        target = run_id
        capability_id = "lifelog.semantic-debug-issue.v1"
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
        if action in {"childcare_event_record", "diet_intake_record"}
        else (
            "approval_required_live_mutation"
            if action == "semantic_debug_issue"
            else "registered_soft_delete"
        )
    )
    authority_valid = bool(
        authority_active
        and authority is not None
        and required_class in authority.allowed_action_classes
        and authority.allows_operation_target(action, target)
        and (
            action != "semantic_debug_issue"
            or is_host_issued_current_turn_authority(authority)
        )
    )
    if action == "childcare_event_record":
        payload_matches_session = childcare_binding is not None
    elif action == "diet_intake_record":
        payload_matches_session = _diet_payload_matches_session(kwargs["payload_name"])
    else:
        payload_matches_session = True
    target_valid = _pending_binding() is not None and payload_matches_session
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

    if action == "childcare_event_record":
        try:
            if childcare_binding is None:
                return _result(CapabilityDecision.DENY_TARGET_MISMATCH)
            owner = _childcare_owner_action(binding=childcare_binding)
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

    if action == "semantic_debug_issue":
        try:
            if authority is None:
                return _result(CapabilityDecision.DENY_AUTHORITY_MISSING)
            owner = _semantic_debug_issue_owner_action(
                run_id=kwargs["run_id"], authority=authority,
            )
            return _result(
                CapabilityDecision.ALLOW,
                write_count=4,
                idempotency_result="inserted",
                readback="passed",
                run_id=owner["run_id"],
                request_sha256=owner["request_sha256"],
                manifest_sha256=owner["manifest_sha256"],
                bundle_state=owner["bundle_state"],
                provider_calls=0,
                credential_resolutions=0,
                network_actions=0,
            )
        except PermissionError:
            return _result(CapabilityDecision.DENY_TARGET_MISMATCH)
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
