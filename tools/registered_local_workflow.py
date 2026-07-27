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
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
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
_SOCIAL_TARGET = "person_park_sanghyun:social-conversation"
_ACTIONS = frozenset(
    {
        "diet_intake_record",
        "social_conversation_record",
        "pending_read",
        "pending_soft_delete",
        "pending_restore",
    }
)
_REASON_CODES = frozenset({"user_dismissed", "superseded", "cleanup_confirmed"})
_DEPENDENCY_DIGESTS: dict[Path, str] = {}
_SOCIAL_MAX_BYTES = 16_384
_SOCIAL_SCHEMA = "social_conversation/v1"
_SOCIAL_INTENT = "confirmed_social_conversation"
_SOCIAL_PERSON_ID = "person_park_sanghyun"
_SOCIAL_REQUIRED_TAGS = frozenset({"social", "conversation", "confirmed_event"})
_SOCIAL_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "intent",
        "occurred_at",
        "ended_at",
        "timezone",
        "person_id",
        "partner_label",
        "confirmed_points",
        "tags",
        "sources",
    }
)
_SOCIAL_SOURCE_FIELDS = frozenset(
    {"platform", "guild_id", "channel_id", "thread_id", "message_id"}
)
_SOCIAL_SNOWFLAKE_RE = re.compile(r"^[0-9]{17,20}$")
_SOCIAL_TAG_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_SOCIAL_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?[+-]\d{2}:\d{2}$"
)
_UTC_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SOCIAL_EVENT_REFERENCE_COLUMNS = {
    "childcare_events": ("event_id",),
    "commute_events": ("event_id",),
    "condition_observations": ("event_id",),
    "event_relations": ("source_event_id", "target_event_id"),
    "life_event_people": ("event_id",),
    "life_event_tags": ("event_id",),
    "life_metrics": ("event_id",),
    "life_milestones": ("event_id",),
    "life_sources": ("event_id",),
    "profile_facts": ("event_id",),
    "reading_action_experiments": ("event_id", "reflection_event_id"),
    "reading_item_events": ("event_id",),
    "reading_note_events": ("event_id",),
    "reading_reflections": ("event_id",),
    "reading_sessions": ("event_id",),
    "sleep_blocks": ("event_id",),
    "training_parts": ("event_id",),
    "training_sessions": ("event_id",),
    "travel_artifacts": ("event_id",),
    "travel_event_places": ("event_id",),
    "travel_route_segments": ("event_id",),
    "travel_trip_events": ("event_id",),
    "value_grill_sessions": ("event_id",),
    "work_blocks": ("event_id",),
}

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


def _read_stable_social_script(root: Path, name: str) -> tuple[Path, bytes]:
    root_fd = scripts_fd = script_fd = None
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("no-follow unavailable")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
            file_flags |= os.O_CLOEXEC
        root_fd = os.open(root, directory_flags)
        scripts_fd = os.open("scripts", directory_flags, dir_fd=root_fd)
        script_fd = os.open(name, file_flags, dir_fd=scripts_fd)
        before = os.fstat(script_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or not 1 <= before.st_size <= 256_000
        ):
            raise OSError("invalid social script")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(script_fd, min(remaining, 16_384))
            if not chunk:
                raise OSError("short social script read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(script_fd, 1):
            raise OSError("oversized social script")
        after = os.fstat(script_fd)
        if _social_fd_signature(after) != _social_fd_signature(before):
            raise OSError("social script changed while reading")
        path = root / "scripts" / name
        path_metadata = os.stat(name, dir_fd=scripts_fd, follow_symlinks=False)
        if _social_fd_signature(path_metadata) != _social_fd_signature(before):
            raise OSError("social script path changed")
        return path, b"".join(chunks)
    finally:
        for descriptor in (script_fd, scripts_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _pin_social_dependencies(root: Path) -> bool:
    try:
        root = Path(root).resolve(strict=True)
        scripts = {}
        for name in (
            "run_registered_recorder.py",
            "record_social_conversation.py",
            "validate_lifelog.py",
        ):
            path, source_bytes = _read_stable_social_script(root, name)
            scripts[path] = hashlib.sha256(source_bytes).hexdigest()
        for path, digest in scripts.items():
            pinned = _DEPENDENCY_DIGESTS.get(path)
            if pinned is not None and not hmac.compare_digest(pinned, digest):
                return False
        for path, digest in scripts.items():
            _DEPENDENCY_DIGESTS.setdefault(path, digest)
        return True
    except BaseException:
        return False


def _social_dependencies_ready() -> bool:
    try:
        root = _LIFELOG_ROOT.resolve(strict=True)
        required = (
            root / "scripts" / "run_registered_recorder.py",
            root / "scripts" / "record_social_conversation.py",
            root / "scripts" / "validate_lifelog.py",
            root / "config" / "recorder-registry.json",
            root / "lifelog.db",
            root / ".runtime-inputs" / "social-conversation",
        )
        return all(
            not path.is_symlink() and (path.is_file() or path.is_dir())
            for path in required
        ) and _pin_social_dependencies(root)
    except Exception:
        return False


def _dependencies_ready(action: str) -> bool:
    if action == "diet_intake_record":
        return _diet_dependencies_ready()
    if action == "social_conversation_record":
        return _social_dependencies_ready()
    if action in {"pending_read", "pending_soft_delete", "pending_restore"}:
        return _pending_dependencies_ready()
    return (
        _pending_dependencies_ready()
        or _diet_dependencies_ready()
        or _social_dependencies_ready()
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


def _social_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("invalid social document")
        document[key] = value
    return document


def _decode_social_document_bytes(document_bytes: bytes) -> dict[str, Any]:
    if type(document_bytes) is not bytes or not 1 <= len(document_bytes) <= _SOCIAL_MAX_BYTES:
        raise ValueError("invalid social document")
    try:
        document = json.loads(
            document_bytes.decode("utf-8"), object_pairs_hook=_social_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid social document") from None
    if type(document) is not dict:
        raise ValueError("invalid social document")
    return document


def _social_string(value: Any, *, max_chars: int) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("invalid social document")
    normalized = value.strip()
    if len(normalized) > max_chars:
        raise ValueError("invalid social document")
    return normalized


def _social_timestamp(value: Any) -> str:
    normalized = _social_string(value, max_chars=40)
    if _SOCIAL_TIMESTAMP_RE.fullmatch(normalized) is None:
        raise ValueError("invalid social document")
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("invalid social document") from None
    return normalized


def _normalize_social_document(document: Any) -> dict[str, Any]:
    if type(document) is not dict:
        raise ValueError("invalid social document")
    required = _SOCIAL_TOP_LEVEL_FIELDS - {"ended_at"}
    if set(document) - _SOCIAL_TOP_LEVEL_FIELDS or required - set(document):
        raise ValueError("invalid social document")
    if document.get("schema_version") != _SOCIAL_SCHEMA:
        raise ValueError("invalid social document")
    if document.get("intent") != _SOCIAL_INTENT:
        raise ValueError("invalid social document")
    if document.get("person_id") != _SOCIAL_PERSON_ID:
        raise ValueError("invalid social document")
    if document.get("timezone") != "Asia/Seoul":
        raise ValueError("invalid social document")

    raw_points = document.get("confirmed_points")
    if type(raw_points) is not list or not 1 <= len(raw_points) <= 8:
        raise ValueError("invalid social document")
    points = [_social_string(point, max_chars=180) for point in raw_points]
    if sum(len(point.encode("utf-8")) for point in points) > 1_200:
        raise ValueError("invalid social document")

    raw_tags = document.get("tags")
    if type(raw_tags) is not list or not 3 <= len(raw_tags) <= 8:
        raise ValueError("invalid social document")
    tags: list[str] = []
    for tag in raw_tags:
        if type(tag) is not str or _SOCIAL_TAG_RE.fullmatch(tag) is None or tag in tags:
            raise ValueError("invalid social document")
        tags.append(tag)
    if not _SOCIAL_REQUIRED_TAGS.issubset(tags):
        raise ValueError("invalid social document")

    raw_sources = document.get("sources")
    if type(raw_sources) is not list or len(raw_sources) != 1:
        raise ValueError("invalid social document")
    raw_source = raw_sources[0]
    if type(raw_source) is not dict or set(raw_source) != _SOCIAL_SOURCE_FIELDS:
        raise ValueError("invalid social document")
    if raw_source.get("platform") != "discord":
        raise ValueError("invalid social document")
    source = {"platform": "discord"}
    for field in ("guild_id", "channel_id", "thread_id", "message_id"):
        value = raw_source.get(field)
        if type(value) is not str or _SOCIAL_SNOWFLAKE_RE.fullmatch(value) is None:
            raise ValueError("invalid social document")
        source[field] = value

    normalized: dict[str, Any] = {
        "schema_version": _SOCIAL_SCHEMA,
        "intent": _SOCIAL_INTENT,
        "occurred_at": _social_timestamp(document.get("occurred_at")),
        "timezone": "Asia/Seoul",
        "person_id": _SOCIAL_PERSON_ID,
        "partner_label": _social_string(document.get("partner_label"), max_chars=40),
        "confirmed_points": points,
        "tags": tags,
        "sources": [source],
    }
    if "ended_at" in document:
        normalized["ended_at"] = _social_timestamp(document.get("ended_at"))
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _derive_expected_social(document: Any) -> dict[str, Any]:
    payload = _normalize_social_document(document)
    source = payload["sources"][0]
    identity = {
        "schema": "social-conversation-identity/v1",
        "person_id": payload["person_id"],
        "sources": payload["sources"],
    }
    event_id = f"social_v1_{_canonical_sha256(identity)[:16]}"
    payload_hash = _canonical_sha256(payload)
    source_digest = hashlib.sha256(
        f"source|{event_id}|{_canonical_json(source)}".encode("utf-8")
    ).hexdigest()
    summary = (
        f"대화 상대: {payload['partner_label']}. 확인된 본인 발언·결정·소회: "
        + " / ".join(payload["confirmed_points"])
    )
    if len(summary) > 1_000:
        raise ValueError("invalid social document")
    return {
        "document": payload,
        "payload_hash": payload_hash,
        "event_id": event_id,
        "event": {
            "id": event_id,
            "occurred_at": payload["occurred_at"],
            "ended_at": payload.get("ended_at"),
            "timezone": "Asia/Seoul",
            "event_type": "note",
            "title": "중요한 대화 기록",
            "summary": summary,
            "source_type": "discord",
            "source_ref": (
                f"discord:{source['channel_id']}:{source['thread_id']}:{source['message_id']}"
            ),
            "confidence": 1.0,
            "raw_text_hash": None,
            "payload_hash": payload_hash,
        },
        "source": {
            "id": f"lifelog-{source_digest[:24]}",
            "event_id": event_id,
            "platform": "discord",
            "guild_id": source["guild_id"],
            "channel_id": source["channel_id"],
            "thread_id": source["thread_id"],
            "message_id": source["message_id"],
            "path": None,
            "redacted_excerpt": None,
        },
        "tags": tuple(sorted(payload["tags"])),
    }


def _current_social_source() -> dict[str, str] | None:
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    guild_id = get_session_env("HERMES_SESSION_SCOPE_ID", "").strip()
    channel_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "").strip() or channel_id
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip()
    source = {
        "platform": platform,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "message_id": message_id,
    }
    if platform != "discord" or any(
        _SOCIAL_SNOWFLAKE_RE.fullmatch(source[field]) is None
        for field in ("guild_id", "channel_id", "thread_id", "message_id")
    ):
        return None
    return source


def _social_fd_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _close_social_payload_binding(binding: dict[str, Any] | None) -> None:
    if binding is None:
        return
    for key in ("payload_fd", "parent_fd"):
        descriptor = binding.get(key)
        if type(descriptor) is int and descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            binding[key] = -1


def _read_bound_social_payload(payload_name: str) -> dict[str, Any] | None:
    if type(payload_name) is not str or _PAYLOAD_NAME_RE.fullmatch(payload_name) is None:
        return None
    root_fd = runtime_fd = social_fd = payload_fd = None
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("no-follow unavailable")
        root = _LIFELOG_ROOT.resolve(strict=True)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        root_fd = os.open(root, directory_flags)
        runtime_fd = os.open(".runtime-inputs", directory_flags, dir_fd=root_fd)
        social_fd = os.open("social-conversation", directory_flags, dir_fd=runtime_fd)
        social_metadata = os.fstat(social_fd)
        if (
            not stat.S_ISDIR(social_metadata.st_mode)
            or social_metadata.st_uid != os.getuid()
            or stat.S_IMODE(social_metadata.st_mode) != 0o700
        ):
            raise OSError("invalid social payload root")

        file_flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            file_flags |= os.O_CLOEXEC
        payload_fd = os.open(payload_name, file_flags, dir_fd=social_fd)
        before = os.fstat(payload_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= _SOCIAL_MAX_BYTES
        ):
            raise OSError("invalid social payload metadata")

        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(payload_fd, min(remaining, 4096))
            if not chunk:
                raise OSError("short social payload read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(payload_fd, 1):
            raise OSError("oversized social payload")
        document_bytes = b"".join(chunks)
        after = os.fstat(payload_fd)
        if _social_fd_signature(after) != _social_fd_signature(before):
            raise OSError("social payload changed while reading")

        expected = _derive_expected_social(_decode_social_document_bytes(document_bytes))
        trusted_source = _current_social_source()
        if trusted_source is None or expected["document"]["sources"] != [trusted_source]:
            raise ValueError("social source mismatch")
        binding = {
            "root": root,
            "name": payload_name,
            "parent_fd": social_fd,
            "payload_fd": payload_fd,
            "signature": _social_fd_signature(before),
            "document_bytes": document_bytes,
            "expected": expected,
            "source": trusted_source,
        }
        social_fd = payload_fd = None
        return binding
    except BaseException:
        return None
    finally:
        for descriptor in (payload_fd, social_fd, runtime_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _unlink_bound_social_payload(binding: dict[str, Any]) -> None:
    payload_fd = binding["payload_fd"]
    parent_fd = binding["parent_fd"]
    current_fd = os.fstat(payload_fd)
    current_path = os.stat(binding["name"], dir_fd=parent_fd, follow_symlinks=False)
    signature = binding["signature"]
    if (
        _social_fd_signature(current_fd) != signature
        or _social_fd_signature(current_path) != signature
    ):
        raise RuntimeError("social payload changed before cleanup")
    os.unlink(binding["name"], dir_fd=parent_fd)
    after_unlink = os.fstat(payload_fd)
    if (
        after_unlink.st_nlink != 0
        or (
            after_unlink.st_dev,
            after_unlink.st_ino,
            after_unlink.st_mode,
            after_unlink.st_uid,
            after_unlink.st_gid,
            after_unlink.st_size,
            after_unlink.st_mtime_ns,
        )
        != (
            signature[0],
            signature[1],
            signature[2],
            signature[3],
            signature[4],
            signature[6],
            signature[7],
        )
    ):
        raise RuntimeError("social payload cleanup failed")
    try:
        os.stat(binding["name"], dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RuntimeError("social payload cleanup failed")


def _execute_pinned_module(name: str, path: Path, source_bytes: bytes) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    try:
        code = compile(source_bytes, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        raise RuntimeError("registered social owner is unavailable") from None
    return module


def _load_social_dispatcher(root: Path) -> ModuleType:
    root = Path(root).resolve(strict=True)
    loaded: dict[str, tuple[Path, bytes]] = {}
    for name in (
        "run_registered_recorder.py",
        "record_social_conversation.py",
        "validate_lifelog.py",
    ):
        path, source_bytes = _read_stable_social_script(root, name)
        pinned = _DEPENDENCY_DIGESTS.get(path)
        digest = hashlib.sha256(source_bytes).hexdigest()
        if pinned is None or not hmac.compare_digest(pinned, digest):
            raise RuntimeError("registered social owner is unavailable")
        loaded[name] = (path, source_bytes)

    recorder_path, recorder_bytes = loaded["record_social_conversation.py"]
    recorder = _execute_pinned_module(
        "_hermes_registered_social_recorder", recorder_path, recorder_bytes
    )
    dispatcher_path, dispatcher_bytes = loaded["run_registered_recorder.py"]
    dispatcher = _execute_pinned_module(
        "_hermes_registered_social_dispatcher", dispatcher_path, dispatcher_bytes
    )

    def pinned_recorder(owner_root_or_script: Path) -> ModuleType:
        requested = Path(owner_root_or_script).resolve(strict=True)
        if requested not in {root, recorder_path.resolve(strict=True)}:
            raise RuntimeError("registered social owner is unavailable")
        return recorder

    setattr(dispatcher, "_load_social_recorder", pinned_recorder)
    validator_path, validator_bytes = loaded["validate_lifelog.py"]
    setattr(
        dispatcher,
        "_SOCIAL_PINNED_VALIDATOR",
        (validator_path, validator_bytes),
    )
    if not callable(getattr(dispatcher, "run_registered_social_document", None)):
        raise RuntimeError("registered social owner is unavailable")
    return dispatcher


def _social_session_binding() -> dict[str, str] | None:
    source = _current_social_source()
    session_id = get_session_env("HERMES_SESSION_ID", "").strip()
    if source is None or not session_id:
        return None
    return {**source, "session_id": session_id}


def _social_pre_live_check(
    *,
    authority: Any,
    binding: dict[str, str],
    document_bytes: bytes,
    expected: dict[str, Any],
) -> None:
    current_authority = get_current_turn_user_authority()
    current_binding = _social_session_binding()
    independently_derived = _derive_expected_social(
        _decode_social_document_bytes(document_bytes)
    )
    if (
        current_authority is not authority
        or not authority.source_event_fingerprint
        or not _authority_matches_session(authority)
        or "trusted_local_record" not in authority.allowed_action_classes
        or not authority.allows_operation_target(
            "social_conversation_record", _SOCIAL_TARGET
        )
        or current_binding != binding
        or independently_derived != expected
        or independently_derived["document"]["sources"] != [
            {key: binding[key] for key in _SOCIAL_SOURCE_FIELDS}
        ]
    ):
        raise PermissionError("social pre-live check denied")


def _parse_utc_second(value: Any) -> datetime:
    if type(value) is not str or _UTC_SECOND_RE.fullmatch(value) is None:
        raise RuntimeError("social exact readback validation failed")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("social exact readback validation failed") from None


def _discover_event_reference_columns(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    discovered: dict[str, tuple[str, ...]] = {}
    for (table_name,) in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
    ).fetchall():
        columns = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM pragma_table_info(?) "
                "WHERE instr(name, 'event_id') > 0 ORDER BY cid",
                (table_name,),
            ).fetchall()
        )
        if columns:
            discovered[table_name] = columns
    return discovered


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _verify_social_written_event(
    db: Path,
    *,
    expected: dict[str, Any],
    inserted_events: int,
    invocation_started_at: datetime,
    invocation_ended_at: datetime,
) -> None:
    if type(inserted_events) is not int or inserted_events not in {0, 1}:
        raise RuntimeError("social exact readback validation failed")
    try:
        if db.is_symlink() or not db.is_file():
            raise RuntimeError("social exact readback validation failed")
        connection = sqlite3.connect(db.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
        try:
            event_rows = connection.execute(
                "SELECT id,occurred_at,ended_at,timezone,event_type,title,summary,"
                "source_type,source_ref,confidence,typeof(confidence),raw_text_hash,"
                "payload_hash,created_at,updated_at FROM life_events WHERE id=? LIMIT 2",
                (expected["event_id"],),
            ).fetchall()
            if len(event_rows) != 1:
                raise RuntimeError("social exact readback validation failed")
            event_row = event_rows[0]
            expected_event = expected["event"]
            if event_row[:10] != (
                expected_event["id"],
                expected_event["occurred_at"],
                expected_event["ended_at"],
                expected_event["timezone"],
                expected_event["event_type"],
                expected_event["title"],
                expected_event["summary"],
                expected_event["source_type"],
                expected_event["source_ref"],
                expected_event["confidence"],
            ):
                raise RuntimeError("social exact readback validation failed")
            if event_row[10] != "real" or event_row[11:13] != (
                expected_event["raw_text_hash"],
                expected_event["payload_hash"],
            ):
                raise RuntimeError("social exact readback validation failed")
            created_at, updated_at = event_row[13], event_row[14]
            created_datetime = _parse_utc_second(created_at)
            if created_at != updated_at or created_datetime > invocation_ended_at:
                raise RuntimeError("social exact readback validation failed")
            if inserted_events == 1 and created_datetime < invocation_started_at:
                raise RuntimeError("social exact readback validation failed")

            people = connection.execute(
                "SELECT event_id,person_id,role,confidence,typeof(confidence) "
                "FROM life_event_people WHERE event_id=? LIMIT 2",
                (expected["event_id"],),
            ).fetchall()
            if people != [
                (expected["event_id"], _SOCIAL_PERSON_ID, "subject", 1.0, "real")
            ]:
                raise RuntimeError("social exact readback validation failed")

            tags = connection.execute(
                "SELECT tag FROM life_event_tags WHERE event_id=? ORDER BY tag LIMIT 9",
                (expected["event_id"],),
            ).fetchall()
            if tuple(row[0] for row in tags) != expected["tags"]:
                raise RuntimeError("social exact readback validation failed")

            sources = connection.execute(
                "SELECT id,event_id,platform,guild_id,channel_id,thread_id,message_id,"
                "path,redacted_excerpt,captured_at FROM life_sources "
                "WHERE event_id=? LIMIT 2",
                (expected["event_id"],),
            ).fetchall()
            expected_source = expected["source"]
            if sources != [
                (
                    expected_source["id"],
                    expected_source["event_id"],
                    expected_source["platform"],
                    expected_source["guild_id"],
                    expected_source["channel_id"],
                    expected_source["thread_id"],
                    expected_source["message_id"],
                    expected_source["path"],
                    expected_source["redacted_excerpt"],
                    created_at,
                )
            ]:
                raise RuntimeError("social exact readback validation failed")

            discovered = _discover_event_reference_columns(connection)
            if discovered != _SOCIAL_EVENT_REFERENCE_COLUMNS:
                raise RuntimeError("social exact readback validation failed")
            for table_name, columns in discovered.items():
                if table_name in {
                    "life_event_people",
                    "life_event_tags",
                    "life_sources",
                }:
                    continue
                where = " OR ".join(
                    f"{_quote_identifier(column)}=?" for column in columns
                )
                count = connection.execute(
                    f"SELECT count(*) FROM {_quote_identifier(table_name)} WHERE {where}",
                    (expected["event_id"],) * len(columns),
                ).fetchone()[0]
                if count != 0:
                    raise RuntimeError("social exact readback validation failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("social exact readback validation failed")
        finally:
            connection.close()
    except RuntimeError:
        raise
    except BaseException:
        raise RuntimeError("social exact readback validation failed") from None


def _validate_social_owner_result(
    result: Any, *, expected: dict[str, Any]
) -> int:
    expected_keys = {
        "schema",
        "recorder_id",
        "exit_status",
        "validation_status",
        "idempotency_result",
        "event_ids",
        "payload_hash",
        "dry_run",
        "backup_status",
        "replay_status",
        "readback",
    }
    if type(result) is not dict or set(result) != expected_keys:
        raise RuntimeError("registered social owner returned invalid evidence")
    string_fields = (
        "schema",
        "recorder_id",
        "validation_status",
        "idempotency_result",
        "payload_hash",
        "backup_status",
        "replay_status",
        "readback",
    )
    if (
        any(type(result.get(field)) is not str for field in string_fields)
        or result.get("schema") != "registered-recorder-result/v1"
        or result.get("recorder_id") != "social_conversation.v1"
        or type(result.get("exit_status")) is not int
        or result["exit_status"] != 0
        or result.get("validation_status")
        != "validator_and_exact_readback_passed"
        or result.get("idempotency_result") not in {"inserted", "existing"}
        or result.get("event_ids") != [expected["event_id"]]
        or result.get("payload_hash") != expected["payload_hash"]
        or result.get("dry_run") is not False
        or result.get("backup_status") != "verified"
        or result.get("replay_status") != "verified"
        or result.get("readback") != "passed"
    ):
        raise RuntimeError("registered social owner returned invalid evidence")
    if (
        type(result["event_ids"]) is not list
        or type(result["event_ids"][0]) is not str
        or re.fullmatch(r"social_v1_[a-f0-9]{16}", result["event_ids"][0]) is None
        or type(result["payload_hash"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", result["payload_hash"]) is None
    ):
        raise RuntimeError("registered social owner returned invalid evidence")
    return 1 if result["idempotency_result"] == "inserted" else 0


def _social_db_identity(path: Path) -> tuple[int, ...]:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("registered social owner is unavailable")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    )


def _social_owner_action(
    *,
    root: Path,
    document_bytes: bytes,
    expected: dict[str, Any],
    authority: Any,
    session_binding: dict[str, str],
) -> dict[str, Any]:
    _social_pre_live_check(
        authority=authority,
        binding=session_binding,
        document_bytes=document_bytes,
        expected=expected,
    )
    dispatcher = _load_social_dispatcher(root)
    db = root / "lifelog.db"
    db_identity = _social_db_identity(db)

    def pre_live_check() -> None:
        _social_pre_live_check(
            authority=authority,
            binding=session_binding,
            document_bytes=document_bytes,
            expected=expected,
        )

    invocation_started_at = datetime.now(timezone.utc).replace(microsecond=0)
    result = dispatcher.run_registered_social_document(
        document_bytes, pre_live_check, root
    )
    invocation_ended_at = datetime.now(timezone.utc)
    if _social_db_identity(db) != db_identity:
        raise RuntimeError("registered social owner is unavailable")
    inserted_events = _validate_social_owner_result(result, expected=expected)
    _verify_social_written_event(
        db,
        expected=expected,
        inserted_events=inserted_events,
        invocation_started_at=invocation_started_at,
        invocation_ended_at=invocation_ended_at,
    )
    if _social_db_identity(db) != db_identity:
        raise RuntimeError("registered social owner is unavailable")
    return result


def registered_local_workflow(action: str, **kwargs: Any) -> dict[str, Any]:
    if action not in _ACTIONS:
        return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
    supplied = {"action", *kwargs}
    expected_arguments = {
        "diet_intake_record": {"action", "payload_name"},
        "social_conversation_record": {"action", "payload_name"},
        "pending_read": {"action", "pending_id"},
        "pending_soft_delete": {"action", "pending_id", "reason_code"},
        "pending_restore": {"action", "pending_id"},
    }[action]
    if supplied != expected_arguments:
        return _result(CapabilityDecision.DENY_SCHEMA_INVALID)

    social_payload: dict[str, Any] | None = None
    social_session: dict[str, str] | None = None
    if action in {"diet_intake_record", "social_conversation_record"}:
        payload_name = kwargs.get("payload_name")
        if type(payload_name) is not str or _PAYLOAD_NAME_RE.fullmatch(payload_name) is None:
            return _result(CapabilityDecision.DENY_SCHEMA_INVALID)
        target = _DIET_TARGET if action == "diet_intake_record" else _SOCIAL_TARGET
        capability_id = (
            "lifelog.diet-intake.v1"
            if action == "diet_intake_record"
            else "lifelog.social-conversation.v1"
        )
        effect = WorkflowEffect.CREATE
        if action == "social_conversation_record":
            social_payload = _read_bound_social_payload(payload_name)
            social_session = _social_session_binding()
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
        if action in {"diet_intake_record", "social_conversation_record"}
        else "registered_soft_delete"
    )
    authority_valid = bool(
        authority_active
        and authority is not None
        and required_class in authority.allowed_action_classes
        and authority.allows_operation_target(action, target)
    )
    if action == "diet_intake_record":
        target_valid = _pending_binding() is not None and _diet_payload_matches_session(
            kwargs["payload_name"]
        )
    elif action == "social_conversation_record":
        target_valid = bool(
            social_payload is not None
            and social_session is not None
            and social_payload["source"]
            == {key: social_session[key] for key in _SOCIAL_SOURCE_FIELDS}
        )
    else:
        target_valid = _pending_binding() is not None

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
        if social_payload is not None:
            try:
                _unlink_bound_social_payload(social_payload)
            except Exception:
                _close_social_payload_binding(social_payload)
                return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
        _close_social_payload_binding(social_payload)
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

    if action == "social_conversation_record":
        if social_payload is None or social_session is None:
            _close_social_payload_binding(social_payload)
            return _result(CapabilityDecision.DENY_TARGET_MISMATCH)
        try:
            _unlink_bound_social_payload(social_payload)
            document_bytes = social_payload["document_bytes"]
            expected_social = social_payload["expected"]
        except Exception:
            return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
        finally:
            _close_social_payload_binding(social_payload)
        try:
            owner = _social_owner_action(
                root=social_payload["root"],
                document_bytes=document_bytes,
                expected=expected_social,
                authority=authority,
                session_binding=social_session,
            )
            idempotency_result = owner["idempotency_result"]
            return _result(
                CapabilityDecision.ALLOW,
                write_count=1 if idempotency_result == "inserted" else 0,
                idempotency_result=idempotency_result,
                validation_status=owner["validation_status"],
                event_ids=owner["event_ids"],
                payload_hash=owner["payload_hash"],
                backup_status=owner["backup_status"],
                replay_status=owner["replay_status"],
                payload_cleanup="deleted",
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
