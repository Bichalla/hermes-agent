"""Default-off fixed owner route for the canonical review ledger.

The model selects only one closed protocol action. Paths, commands, SQL,
authority claims, environment, raw reviewer text, and free-form summaries are
never caller-controlled.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.workflow_action_policy import (
    AuthorityMode,
    CapabilityDecision,
    WorkflowEffect,
    evaluate_registered_capability,
)
from gateway.session_context import (
    get_session_controller_role,
    get_session_env,
    get_trusted_current_user_text,
)
from tools.registry import registry

_OWNER_ROOT = Path.home() / ".hermes" / "ops"
_OWNER_WRAPPER = _OWNER_ROOT / "scripts" / "review_ledger.py"
_CANONICAL_DB = _OWNER_ROOT / "state" / "review-ledger" / "review-ledger.sqlite3"
_PYTHON = str(Path(__file__).resolve().parents[1] / "venv" / "bin" / "python")
_BLOCKED_PLATFORMS = frozenset(
    {"background", "cron", "delegate", "review", "subagent", "webhook"}
)
_ACTIONS = frozenset({"freeze", "start_attempt", "record_result", "status", "finalize"})
_MUTATIONS = frozenset({"freeze", "start_attempt", "record_result"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
_OUTCOMES = frozenset(
    {"PASS", "REQUEST_CHANGES", "TIMED_OUT", "EXECUTION_FAILED", "CANCELLED"}
)
_REVIEW_OUTCOMES = frozenset({"PASS", "REQUEST_CHANGES"})
_FINDING_CLASSES = frozenset(
    {
        "no_findings",
        "correctness",
        "security",
        "operations",
        "privacy",
        "scope",
        "test_coverage",
        "maintainability",
    }
)
_EXPECTED_FIELDS = {
    "freeze": frozenset({"action", "bundle_sha256", "required_roles", "created_at"}),
    "start_attempt": frozenset({"action", "bundle_sha256", "role", "started_at"}),
    "record_result": frozenset(
        {"action", "attempt_id", "outcome", "finding_classes", "completed_at"}
    ),
    "status": frozenset({"action", "bundle_sha256"}),
    "finalize": frozenset({"action", "bundle_sha256", "current_bundle_sha256"}),
}
_COMMANDS = {
    "freeze": "freeze",
    "start_attempt": "start-attempt",
    "record_result": "record-result",
    "status": "status",
    "finalize": "finalize",
}
_EFFECTS = {
    "freeze": WorkflowEffect.CREATE,
    "start_attempt": WorkflowEffect.CREATE,
    "record_result": WorkflowEffect.UPDATE,
    "status": WorkflowEffect.READ,
    "finalize": WorkflowEffect.READ,
}

REGISTERED_REVIEW_LEDGER_SCHEMA = {
    "name": "registered_review_ledger",
    "description": (
        "Execute the fixed five-action review-ledger owner protocol. Main controller "
        "only; paths, commands, SQL, environment, raw review text, and summaries are fixed."
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


class OwnerResponseError(RuntimeError):
    """The fixed owner route did not return one valid bounded response."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OwnerResponseError("owner returned duplicate JSON keys")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> Any:
    raise OwnerResponseError("owner returned non-finite JSON")


def _feature_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        block = (load_config() or {}).get("review_ledger_controller") or {}
        return isinstance(block, dict) and block.get("enabled") is True
    except Exception:
        return False


def _dependencies_ready() -> bool:
    try:
        root = _OWNER_ROOT.resolve(strict=True)
        wrapper = _OWNER_WRAPPER.resolve(strict=True)
        parent = _CANONICAL_DB.parent.resolve(strict=True)
        return bool(
            root.is_dir()
            and wrapper.is_file()
            and not _OWNER_WRAPPER.is_symlink()
            and not _CANONICAL_DB.is_symlink()
            and wrapper.is_relative_to(root)
            and parent.is_relative_to(root)
        )
    except (OSError, RuntimeError):
        return False


def check_registered_review_ledger_requirements() -> bool:
    return _feature_enabled() and _dependencies_ready()


def _result(decision: CapabilityDecision | str, **extra: Any) -> dict[str, Any]:
    value = decision.value if isinstance(decision, CapabilityDecision) else str(decision)
    result: dict[str, Any] = {
        "schema": "review-ledger-controller-result/v1",
        "decision": value,
        "prompt_count": 0,
        "write_count": 0,
        "action_id": f"rla_{secrets.token_hex(16)}",
    }
    result.update(extra)
    return result


def _valid_timestamp(value: Any) -> bool:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 128:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_digest(value: Any) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _valid_role(value: Any) -> bool:
    return type(value) is str and _ROLE_RE.fullmatch(value) is not None


def _validate_request(request: dict[str, Any]) -> bool:
    action = request.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return False
    if frozenset(request) != _EXPECTED_FIELDS[action]:
        return False
    if action in {"freeze", "start_attempt", "status", "finalize"} and not _valid_digest(
        request.get("bundle_sha256")
    ):
        return False
    if action == "freeze":
        roles = request.get("required_roles")
        return bool(
            type(roles) is list
            and 1 <= len(roles) <= 16
            and all(_valid_role(role) for role in roles)
            and roles == sorted(roles)
            and len(set(roles)) == len(roles)
            and _valid_timestamp(request.get("created_at"))
        )
    if action == "start_attempt":
        return _valid_role(request.get("role")) and _valid_timestamp(
            request.get("started_at")
        )
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
            or any(type(item) is not str or item not in _FINDING_CLASSES for item in findings)
            or findings != sorted(findings)
            or len(findings) != len(set(findings))
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


def _controller_authorized(session_id: Any) -> bool:
    if type(session_id) is not str or not session_id or len(session_id) > 256:
        return False
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    trusted_session = get_session_env("HERMES_SESSION_ID", "").strip()
    controller_role = get_session_controller_role()
    trusted_user_text = get_trusted_current_user_text()
    if (
        controller_role != "main_controller"
        or not isinstance(trusted_user_text, str)
        or not trusted_user_text
        or not platform
        or platform in _BLOCKED_PLATFORMS
        or not trusted_session
        or os.environ.get("HERMES_KANBAN_TASK")
    ):
        return False
    return hmac.compare_digest(session_id, trusted_session)


def _safe_summary(outcome: str, findings: list[str]) -> str | None:
    if outcome == "PASS":
        return "PASS: no material findings."
    if outcome == "REQUEST_CHANGES":
        return "REQUEST_CHANGES: material findings in " + ", ".join(findings) + "."
    return None


def _owner_payload(request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    payload = {key: value for key, value in request.items() if key != "action"}
    if action == "record_result":
        payload.pop("finding_classes")
        payload["safe_summary"] = _safe_summary(
            request["outcome"], request["finding_classes"]
        )
    return payload


def _parse_owner_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if completed.returncode != 0 or completed.stderr:
        raise OwnerResponseError("owner command failed")
    raw = completed.stdout
    if type(raw) is not str or len(raw.encode("utf-8")) > 65536:
        raise OwnerResponseError("owner response exceeded bounds")
    lines = raw.splitlines()
    if len(lines) != 1:
        raise OwnerResponseError("owner returned multiple or empty responses")
    try:
        value = json.loads(
            lines[0],
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeError, OwnerResponseError) as exc:
        raise OwnerResponseError("owner returned malformed JSON") from exc
    if type(value) is not dict or "error" in value:
        raise OwnerResponseError("owner rejected the request")
    return value


def _invoke_owner(request: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            _PYTHON,
            "-I",
            "-B",
            str(_OWNER_WRAPPER),
            "--db",
            str(_CANONICAL_DB),
            _COMMANDS[request["action"]],
        ],
        cwd=_OWNER_ROOT,
        input=json.dumps(
            _owner_payload(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        text=True,
        capture_output=True,
        shell=False,
        timeout=30,
        check=False,
        env={
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TZ": "UTC",
        },
    )
    return _parse_owner_result(completed)


def _readback_mutation(action: str, request: dict[str, Any]) -> dict[str, Any] | None:
    if action not in _MUTATIONS or not _CANONICAL_DB.is_file():
        return None
    connection = sqlite3.connect(
        _CANONICAL_DB.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        if action == "freeze":
            row = connection.execute(
                "SELECT required_roles_json, created_at FROM revisions WHERE bundle_sha256=?",
                (request["bundle_sha256"],),
            ).fetchone()
            if row is None:
                return None
            roles = json.loads(row["required_roles_json"])
            if (
                roles != request["required_roles"]
                or row["created_at"] != request["created_at"]
            ):
                return None
            return {
                "bundle_sha256": request["bundle_sha256"],
                "required_roles": roles,
                "created_at": row["created_at"],
            }
        if action == "start_attempt":
            rows = connection.execute(
                """
                SELECT id, attempt_no, status FROM attempts
                 WHERE bundle_sha256=? AND role=? AND started_at=?
                """,
                (
                    request["bundle_sha256"],
                    request["role"],
                    request["started_at"],
                ),
            ).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
            return {
                "attempt_id": row["id"],
                "bundle_sha256": request["bundle_sha256"],
                "role": request["role"],
                "attempt_no": row["attempt_no"],
                "status": row["status"],
            }
        row = connection.execute(
            """
            SELECT a.status, a.completed_at, r.verdict, r.safe_summary
              FROM attempts a LEFT JOIN results r ON r.attempt_id=a.id
             WHERE a.id=?
            """,
            (request["attempt_id"],),
        ).fetchone()
        expected_status = (
            "COMPLETED" if request["outcome"] in _REVIEW_OUTCOMES else request["outcome"]
        )
        expected_verdict = (
            request["outcome"] if request["outcome"] in _REVIEW_OUTCOMES else None
        )
        if (
            row is None
            or row["status"] != expected_status
            or row["completed_at"] != request["completed_at"]
            or row["verdict"] != expected_verdict
            or row["safe_summary"]
            != _safe_summary(request["outcome"], request["finding_classes"])
        ):
            return None
        return {
            "attempt_id": request["attempt_id"],
            "status": expected_status,
            "verdict": expected_verdict,
        }
    except (json.JSONDecodeError, sqlite3.Error, TypeError):
        return None
    finally:
        connection.close()


def _valid_optional_positive_int(value: Any) -> bool:
    return value is None or (type(value) is int and value > 0)


def _valid_optional_verdict(value: Any) -> bool:
    return value is None or value in _REVIEW_OUTCOMES


def _valid_safe_summary(verdict: Any, value: Any) -> bool:
    if verdict is None:
        return value is None
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if verdict == "PASS":
        return value == "PASS: no material findings."
    prefix = "REQUEST_CHANGES: material findings in "
    if not value.startswith(prefix) or not value.endswith("."):
        return False
    findings = value[len(prefix) : -1].split(", ")
    return bool(
        findings
        and findings == sorted(findings)
        and len(findings) == len(set(findings))
        and all(
            finding in _FINDING_CLASSES and finding != "no_findings"
            for finding in findings
        )
    )


def _valid_slot(slot: Any) -> bool:
    if type(slot) is not dict or set(slot) != {
        "role",
        "latest_status",
        "latest_verdict",
        "latest_attempt_no",
        "latest_review_verdict",
        "latest_review_attempt_no",
    }:
        return False
    return bool(
        _valid_role(slot["role"])
        and slot["latest_status"]
        in {
            "MISSING",
            "RUNNING",
            "COMPLETED",
            "TIMED_OUT",
            "EXECUTION_FAILED",
            "CANCELLED",
        }
        and _valid_optional_verdict(slot["latest_verdict"])
        and _valid_optional_positive_int(slot["latest_attempt_no"])
        and _valid_optional_verdict(slot["latest_review_verdict"])
        and _valid_optional_positive_int(slot["latest_review_attempt_no"])
    )


def _validate_attempt(attempt: Any, roles: list[str]) -> bool:
    if type(attempt) is not dict or set(attempt) != {
        "attempt_id",
        "role",
        "attempt_no",
        "status",
        "started_at",
        "completed_at",
        "verdict",
        "safe_summary",
    }:
        return False
    status = attempt["status"]
    verdict = attempt["verdict"]
    completed_at = attempt["completed_at"]
    if (
        type(attempt["attempt_id"]) is not int
        or attempt["attempt_id"] <= 0
        or attempt["role"] not in roles
        or type(attempt["attempt_no"]) is not int
        or attempt["attempt_no"] <= 0
        or status
        not in {
            "RUNNING",
            "COMPLETED",
            "TIMED_OUT",
            "EXECUTION_FAILED",
            "CANCELLED",
        }
        or not _valid_timestamp(attempt["started_at"])
        or not _valid_optional_verdict(verdict)
        or not _valid_safe_summary(verdict, attempt["safe_summary"])
    ):
        return False
    if status == "RUNNING":
        return completed_at is None and verdict is None
    if not _valid_timestamp(completed_at):
        return False
    if status == "COMPLETED":
        return verdict in _REVIEW_OUTCOMES
    return verdict is None


def _expected_slots(roles: list[str], attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    latest_review: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        latest[attempt["role"]] = attempt
        if attempt["verdict"] is not None:
            latest_review[attempt["role"]] = attempt
    return [
        {
            "role": role,
            "latest_status": latest[role]["status"] if role in latest else "MISSING",
            "latest_verdict": latest[role]["verdict"] if role in latest else None,
            "latest_attempt_no": latest[role]["attempt_no"] if role in latest else None,
            "latest_review_verdict": (
                latest_review[role]["verdict"] if role in latest_review else None
            ),
            "latest_review_attempt_no": (
                latest_review[role]["attempt_no"] if role in latest_review else None
            ),
        }
        for role in roles
    ]


def _normalize_read_result(
    action: str, request: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    if action == "status":
        if set(result) != {
            "bundle_sha256",
            "required_roles",
            "created_at",
            "slots",
            "attempts",
        } or result.get("bundle_sha256") != request["bundle_sha256"]:
            return None
        roles = result.get("required_roles")
        slots = result.get("slots")
        attempts = result.get("attempts")
        if not (
            type(roles) is list
            and roles
            and roles == sorted(roles)
            and len(roles) == len(set(roles))
            and all(_valid_role(role) for role in roles)
            and _valid_timestamp(result.get("created_at"))
            and type(slots) is list
            and len(slots) == len(roles)
            and all(_valid_slot(slot) for slot in slots)
            and [slot["role"] for slot in slots] == roles
            and type(attempts) is list
            and all(_validate_attempt(attempt, roles) for attempt in attempts)
            and attempts
            == sorted(attempts, key=lambda item: (item["role"], item["attempt_no"]))
            and len({attempt["attempt_id"] for attempt in attempts}) == len(attempts)
            and slots == _expected_slots(roles, attempts)
        ):
            return None
        return result
    if action == "finalize":
        if set(result) != {
            "bundle_sha256",
            "current_bundle_sha256",
            "decision",
            "slots",
        }:
            return None
        slots = result.get("slots")
        if not (
            result.get("bundle_sha256") == request["bundle_sha256"]
            and result.get("current_bundle_sha256")
            == request["current_bundle_sha256"]
            and result.get("decision")
            in {"STALE", "INCOMPLETE", "REQUEST_CHANGES", "CONVERGED"}
            and type(slots) is list
            and slots
            and all(_valid_slot(slot) for slot in slots)
            and [slot["role"] for slot in slots]
            == sorted({slot["role"] for slot in slots})
        ):
            return None
        if request["current_bundle_sha256"] != request["bundle_sha256"]:
            expected_decision = "STALE"
        elif any(slot["latest_review_verdict"] is None for slot in slots):
            expected_decision = "INCOMPLETE"
        elif any(
            slot["latest_review_verdict"] == "REQUEST_CHANGES" for slot in slots
        ):
            expected_decision = "REQUEST_CHANGES"
        else:
            expected_decision = "CONVERGED"
        if result["decision"] != expected_decision:
            return None
        return result
    return None


def registered_review_ledger(*, session_id: str | None = None, **request: Any) -> dict[str, Any]:
    action = request.get("action")
    if type(action) is not str or action not in _ACTIONS:
        return _result(CapabilityDecision.DENY_UNREGISTERED_ACTION)
    if not _validate_request(request):
        return _result(CapabilityDecision.DENY_SCHEMA_INVALID)

    owner_ready = _feature_enabled() and _dependencies_ready()
    authority_mode = (
        AuthorityMode.MAIN_CONTROLLER if _controller_authorized(session_id) else None
    )
    decision = evaluate_registered_capability(
        "review-ledger.history.v1",
        action,
        _EFFECTS[action],
        schema_valid=True,
        authority_mode=authority_mode,
        owner_ready=owner_ready,
        target_valid=_dependencies_ready(),
    )
    if decision is not CapabilityDecision.ALLOW:
        return _result(decision)

    preexisting = _readback_mutation(action, request)
    try:
        owner_result = _invoke_owner(request)
    except (OwnerResponseError, OSError, subprocess.SubprocessError, sqlite3.Error):
        reconciled = _readback_mutation(action, request)
        if reconciled is None:
            return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
        return _result(
            CapabilityDecision.ALLOW,
            action=action,
            write_count=0 if preexisting is not None else 1,
            idempotency_result="reconciled",
            readback="passed",
            result=reconciled,
        )

    if action in _MUTATIONS:
        readback = _readback_mutation(action, request)
        if readback is None or owner_result != readback:
            return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
        return _result(
            CapabilityDecision.ALLOW,
            action=action,
            write_count=0 if preexisting is not None else 1,
            idempotency_result="existing" if preexisting is not None else "inserted",
            readback="passed",
            result=readback,
        )
    normalized = _normalize_read_result(action, request, owner_result)
    if normalized is None:
        return _result(CapabilityDecision.DENY_OWNER_UNAVAILABLE)
    return _result(
        CapabilityDecision.ALLOW,
        action=action,
        write_count=0,
        idempotency_result="read_only",
        readback="passed",
        result=normalized,
    )


def _handle_registered_review_ledger(args: Any, **context: Any) -> str:
    if type(args) is not dict or "session_id" in args:
        result = _result(CapabilityDecision.DENY_SCHEMA_INVALID)
    else:
        try:
            result = registered_review_ledger(
                session_id=context.get("session_id"), **args
            )
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
