"""Read-only lane/role contract helpers for Hermes Kanban cards.

Phase 1 lane/role mapping treats ``lane`` as card metadata only. It never
promotes a task, never dispatches work, and never treats virtual lanes or
subagent task roles as executable Hermes profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import types
from typing import Any, Iterable, Optional

from hermes_constants import get_default_hermes_root
from toolsets import get_toolset_names

VIRTUAL_LANES = {
    "intake_triage",
    "planning",
    "scout_research",
    "implementation",
    "review_safety",
    "docs_research",
    "follow_up_monitoring",
}

# Mirrors the existing context-preserving orchestration packet vocabulary. This
# is validation metadata, not a new delegate_task.role authority.
SUBAGENT_TASK_ROLES = {
    "scout",
    "plan_writer",
    "reviewer",
    "patch_worker",
    "focused_reviewer",
    "implementer",
    "spec_reviewer",
    "quality_reviewer",
}

CHANGE_GATE_REASON_CODES = frozenset({
    "CHANGE_GATE_METADATA_MISSING",
    "CHANGE_GATE_STAGE_INVALID",
    "CHANGE_GATE_ROLE_INVALID",
    "CHANGE_GATE_ARTIFACT_MISSING",
    "CHANGE_GATE_ARTIFACT_NOT_ATTACHED",
    "CHANGE_GATE_ARTIFACT_UNSAFE",
    "CHANGE_GATE_DIGEST_MISMATCH",
    "CHANGE_GATE_SCHEMA_INVALID",
    "CHANGE_GATE_POLICY_VERSION_MISMATCH",
    "CHANGE_GATE_EVIDENCE_INVALID",
    "CHANGE_GATE_BLOCKING_UNKNOWN",
    "CHANGE_GATE_TASK_MISMATCH",
    "CHANGE_GATE_PROFILE_MISMATCH",
    "CHANGE_GATE_MODEL_MISMATCH",
    "CHANGE_GATE_EFFORT_MISMATCH",
    "CHANGE_GATE_BOOTSTRAP_NOT_ALLOWED",
    "CHANGE_GATE_VALIDATOR_UNAVAILABLE",
})
_CANONICAL_CHANGE_GATE_ROOT = Path("/Users/honbul/.hermes")
_CHANGE_GATE_PACKET_LIMIT = 1024 * 1024
_CHANGE_GATE_POLICY_LIMIT = 512 * 1024
_MISSING = object()
_validator_module: Any = None
_validator_module_digest: Optional[str] = None


@dataclass(frozen=True)
class ChangeGateMetadata:
    """Closed operational pointer carried by an implementation contract."""

    stage: str
    artifact_ref: str
    artifact_sha256: str
    role: str
    review_outcome: Optional[str] = None


@dataclass(frozen=True)
class ChangeGateReadiness:
    ok: bool
    reason_codes: list[str] = field(default_factory=list)
    bootstrap_active: bool = False


@dataclass(frozen=True)
class _StableFileSnapshot:
    path: Path
    data: bytes
    sha256: str
    identity: tuple[int, int, int, int, int, int]


class _TrustedReadError(OSError):
    """A rooted no-follow read could not prove the authorized file identity."""


class ChangeGateBlocked(RuntimeError):
    """Bounded claim refusal carrying only stable Change Gate reason codes."""

    def __init__(self, reason_codes: str | Iterable[str]):
        if isinstance(reason_codes, str):
            codes = [reason_codes]
        else:
            codes = list(reason_codes)
        safe = [code for code in codes if code in CHANGE_GATE_REASON_CODES]
        if not safe:
            safe = ["CHANGE_GATE_SCHEMA_INVALID"]
        self.reason_codes = list(dict.fromkeys(safe))
        super().__init__(", ".join(self.reason_codes))


@dataclass(frozen=True)
class LaneRoleContract:
    lane: Optional[str] = None
    card_type: Optional[str] = None
    risk_class: Optional[str] = None
    human_required: Optional[bool] = None
    approval_boundary: list[str] = field(default_factory=list)
    repository_or_root: Optional[str] = None
    acceptance_criteria: list[str] = field(default_factory=list)
    verification: Any = None
    stop_conditions: list[str] = field(default_factory=list)
    recommended_assignee: Optional[str] = None
    recommended_skills: list[str] = field(default_factory=list)
    subagent_task_role: Optional[str] = None
    review_source_pointer: Optional[str] = None
    change_gate: Optional[ChangeGateMetadata] = None
    parseable: bool = True


@dataclass(frozen=True)
class ReadyCheckResult:
    pickup_ready: bool
    missing_fields: list[str]
    errors: list[str]
    warnings: list[str]
    recommended_next_action: str
    assignee_valid: bool = False
    contract: LaneRoleContract = field(default_factory=lambda: LaneRoleContract(parseable=False))


def _task_get(task: Any, name: str, default: Any = None) -> Any:
    try:
        if hasattr(task, "keys") and name in task.keys():
            return task[name]
    except Exception:
        pass
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _json_object_from_text(value: str) -> Optional[dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _body_to_dict(body: Any) -> Optional[dict[str, Any]]:
    if isinstance(body, dict):
        return body
    if isinstance(body, str):
        return _json_object_from_text(body)
    return None


def _contract_payload(body: dict[str, Any]) -> dict[str, Any]:
    nested = body.get("contract")
    if isinstance(nested, dict):
        return nested
    return body


def contract_body_has_signal(body: Any) -> bool:
    """Return True when a body contains actual lane/role contract metadata."""
    parsed = _body_to_dict(body)
    if parsed is None:
        return False
    if isinstance(parsed.get("contract"), dict):
        return True
    contract_keys = {
        "lane",
        "type",
        "card_type",
        "risk_class",
        "human_required",
        "approval_boundary",
        "repository_or_root",
        "acceptance_criteria",
        "verification",
        "stop_conditions",
        "recommended_assignee",
        "recommended_skills",
        "subagent_task_role",
        "change_gate",
    }
    return any(key in parsed for key in contract_keys)


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, (list, tuple, set)):
            joined = ", ".join(str(item).strip() for item in value if str(item).strip())
            if joined:
                return joined
            continue
        text = str(value or "").strip()
        if text:
            return text
    return None


def _is_clean_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def parse_contract_body(body: Any) -> LaneRoleContract:
    """Parse top-level or conversational-intake-envelope contract metadata."""
    parsed = _body_to_dict(body)
    if parsed is None:
        return LaneRoleContract(parseable=False)
    payload = _contract_payload(parsed)
    change_gate = None
    change_gate_parseable = True
    raw_change_gate = payload.get("change_gate", _MISSING)
    if raw_change_gate is not _MISSING:
        if not isinstance(raw_change_gate, dict):
            change_gate_parseable = False
        else:
            allowed_change_gate = {
                "stage", "artifact_ref", "artifact_sha256", "role", "review_outcome",
            }
            required_change_gate = {"stage", "artifact_ref", "artifact_sha256", "role"}
            invalid_shape = (
                bool(set(raw_change_gate) - allowed_change_gate)
                or not required_change_gate.issubset(raw_change_gate)
                or any(not _is_clean_text(raw_change_gate.get(key)) for key in required_change_gate)
                or (
                    "review_outcome" in raw_change_gate
                    and not _is_clean_text(raw_change_gate["review_outcome"])
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    raw_change_gate.get("artifact_sha256", ""),
                )
            )
            if invalid_shape:
                change_gate_parseable = False
            else:
                change_gate = ChangeGateMetadata(
                    stage=raw_change_gate["stage"],
                    artifact_ref=raw_change_gate["artifact_ref"],
                    artifact_sha256=raw_change_gate["artifact_sha256"],
                    role=raw_change_gate["role"],
                    review_outcome=(
                        raw_change_gate["review_outcome"]
                        if "review_outcome" in raw_change_gate else None
                    ),
                )
    return LaneRoleContract(
        lane=str(payload["lane"]).strip() if payload.get("lane") else None,
        card_type=str(payload.get("type") or payload.get("card_type") or "").strip() or None,
        risk_class=str(payload["risk_class"]).strip() if payload.get("risk_class") else None,
        human_required=payload.get("human_required") if isinstance(payload.get("human_required"), bool) else None,
        approval_boundary=_as_list(payload.get("approval_boundary")),
        repository_or_root=str(payload.get("repository_or_root") or "").strip() or None,
        acceptance_criteria=_as_list(payload.get("acceptance_criteria")),
        verification=payload.get("verification"),
        stop_conditions=_as_list(payload.get("stop_conditions")),
        recommended_assignee=str(payload.get("recommended_assignee") or "").strip() or None,
        recommended_skills=_as_list(payload.get("recommended_skills")),
        subagent_task_role=str(payload.get("subagent_task_role") or "").strip() or None,
        review_source_pointer=_first_text(
            payload.get("reviewed_artifact"),
            payload.get("reviewed_artifacts"),
            payload.get("source_plan_path"),
            payload.get("source_plan"),
            payload.get("artifact_path"),
            payload.get("artifact"),
            parsed.get("source_ref") if isinstance(parsed, dict) else None,
        ),
        change_gate=change_gate,
        parseable=change_gate_parseable,
    )


def _attachment_value(attachment: Any, name: str) -> Any:
    if isinstance(attachment, dict):
        return attachment.get(name)
    return getattr(attachment, name, None)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _rooted_components(root: Path, target: Path) -> tuple[Path, list[str]]:
    """Return an absolute target and safe relative components without resolving links."""
    raw_parts = Path(os.fspath(target)).parts
    if any(component in {".", ".."} for component in raw_parts):
        raise _TrustedReadError("non-normalized rooted path")
    root_abs = Path(os.path.abspath(os.fspath(root)))
    target_abs = Path(os.path.abspath(os.fspath(target)))
    if os.path.normpath(os.fspath(target_abs)) != os.fspath(target_abs):
        raise _TrustedReadError("non-normalized rooted path")
    try:
        relative = target_abs.relative_to(root_abs)
    except ValueError as exc:
        raise _TrustedReadError("path escapes trusted root") from exc
    components = list(relative.parts)
    if not components or any(component in {"", ".", ".."} for component in components):
        raise _TrustedReadError("invalid rooted path components")
    return target_abs, components


def _read_trusted_file(root: Path, target: Path, *, limit: int) -> _StableFileSnapshot:
    """Read a regular file through descriptor-rooted, no-follow traversal."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise _TrustedReadError("rooted no-follow traversal unavailable")
    target_abs, components = _rooted_components(root, target)
    try:
        root_fd = os.open(
            os.fspath(Path(os.path.abspath(os.fspath(root)))),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise _TrustedReadError("trusted root could not be opened") from exc
    parent_fd = root_fd
    opened_dirs: list[int] = []
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_fd)
                raise _TrustedReadError("intermediate component is not a directory")
            opened_dirs.append(next_fd)
            parent_fd = next_fd
        final_fd = os.open(
            components[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        try:
            before = os.fstat(final_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise _TrustedReadError("final component is not a bounded regular file")
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(final_fd, min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise _TrustedReadError("file exceeds bounded read limit")
            after = os.fstat(final_fd)
            before_identity = _stat_identity(before)
            after_identity = _stat_identity(after)
            if before_identity != after_identity:
                raise _TrustedReadError("file identity changed during read")
            data = b"".join(chunks)
            return _StableFileSnapshot(
                path=target_abs,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
                identity=after_identity,
            )
        finally:
            os.close(final_fd)
    except FileNotFoundError as exc:
        raise _TrustedReadError("trusted file missing") from exc
    except OSError as exc:
        if isinstance(exc, _TrustedReadError):
            raise
        raise _TrustedReadError("trusted file could not be opened") from exc
    finally:
        for fd in reversed(opened_dirs):
            os.close(fd)
        os.close(root_fd)


def _snapshot_same(left: _StableFileSnapshot, right: _StableFileSnapshot) -> bool:
    return (
        left.path == right.path
        and left.identity == right.identity
        and left.sha256 == right.sha256
    )


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _load_change_gate_validator(source: Optional[_StableFileSnapshot] = None) -> Any:
    """Load the canonical validator from exact trusted-read bytes."""
    global _validator_module, _validator_module_digest
    if source is None:
        source = _read_trusted_file(
            _CANONICAL_CHANGE_GATE_ROOT,
            _CANONICAL_CHANGE_GATE_ROOT / "scripts/change_gate_validate.py",
            limit=_CHANGE_GATE_POLICY_LIMIT,
        )
    if _validator_module is not None and _validator_module_digest == source.sha256:
        return _validator_module
    module = types.ModuleType("hermes_change_gate_validator")
    module.__file__ = os.fspath(source.path)
    module.__package__ = ""
    code = compile(source.data, os.fspath(source.path), "exec")
    exec(code, module.__dict__)
    _validator_module = module
    _validator_module_digest = source.sha256
    return module


def _map_validator_failure(
    code: str,
    handoff: Optional[dict[str, Any]],
    task: Any,
    error_path: Optional[str] = None,
) -> str:
    evidence_ref = str(handoff.get("evidence_ref", "")) if isinstance(handoff, dict) else ""
    if code in {"EVIDENCE_DIGEST_MISMATCH", "EVIDENCE_TASK_ID_MISMATCH"} or (
        evidence_ref and error_path and evidence_ref in error_path
    ):
        return "CHANGE_GATE_EVIDENCE_INVALID"
    if code == "POLICY_VERSION_MISMATCH":
        return "CHANGE_GATE_POLICY_VERSION_MISMATCH"
    if code == "BLOCKING_UNKNOWN":
        return "CHANGE_GATE_BLOCKING_UNKNOWN"
    if code == "TASK_ID_MISMATCH":
        return "CHANGE_GATE_TASK_MISMATCH"
    if code == "ARTIFACT_NOT_FOUND":
        return "CHANGE_GATE_ARTIFACT_MISSING"
    if code == "INVALID_STATE":
        return "CHANGE_GATE_STAGE_INVALID"
    if code == "ROUTE_MISMATCH" and isinstance(handoff, dict):
        route = handoff.get("execution_route")
        if not isinstance(route, dict):
            return "CHANGE_GATE_SCHEMA_INVALID"
        if route.get("profile") != _task_get(task, "assignee", None):
            return "CHANGE_GATE_PROFILE_MISMATCH"
        if route.get("model") != _task_get(task, "model_override", None):
            return "CHANGE_GATE_MODEL_MISMATCH"
        if route.get("reasoning_effort") != "xhigh":
            return "CHANGE_GATE_EFFORT_MISMATCH"
    if code in {
        "SCHEMA_VALIDATION_FAILED", "MALFORMED_JSON", "ROOT_NOT_OBJECT", "INVALID_BOOTSTRAP",
    }:
        return "CHANGE_GATE_SCHEMA_INVALID"
    return "CHANGE_GATE_EVIDENCE_INVALID"


def check_change_gate_readiness(
    task: Any,
    attachments: Iterable[Any],
    *,
    attachment_root: Path,
) -> ChangeGateReadiness:
    """Pure/read-only readiness adapter for implementation claim authority."""
    contract = parse_contract_body(_task_get(task, "body", ""))
    if contract.lane != "implementation":
        return ChangeGateReadiness(ok=True)
    if contract.change_gate is None:
        parsed_body = _body_to_dict(_task_get(task, "body", "")) or {}
        payload = _contract_payload(parsed_body)
        if "change_gate" in payload:
            return ChangeGateReadiness(False, ["CHANGE_GATE_SCHEMA_INVALID"])
        return ChangeGateReadiness(False, ["CHANGE_GATE_METADATA_MISSING"])
    metadata = contract.change_gate
    reasons: list[str] = []
    if metadata.stage != "PLAN_APPROVED":
        reasons.append("CHANGE_GATE_STAGE_INVALID")
    if metadata.role != "EXECUTOR":
        reasons.append("CHANGE_GATE_ROLE_INVALID")
    if reasons:
        return ChangeGateReadiness(False, reasons)

    stored_paths = {
        _attachment_value(attachment, "stored_path")
        for attachment in attachments
    }
    if not metadata.artifact_ref:
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_MISSING"])
    if metadata.artifact_ref not in stored_paths:
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_NOT_ATTACHED"])

    artifact = Path(metadata.artifact_ref)
    if not artifact.is_absolute():
        artifact = Path(attachment_root) / artifact
    try:
        handoff_snapshot = _read_trusted_file(
            Path(attachment_root), artifact, limit=_CHANGE_GATE_PACKET_LIMIT
        )
    except _TrustedReadError as exc:
        if "missing" in str(exc):
            return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_MISSING"])
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_UNSAFE"])
    if handoff_snapshot.sha256 != metadata.artifact_sha256:
        return ChangeGateReadiness(False, ["CHANGE_GATE_DIGEST_MISMATCH"])
    try:
        handoff = _strict_json_object(handoff_snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ChangeGateReadiness(False, ["CHANGE_GATE_SCHEMA_INVALID"])
    evidence_ref = handoff.get("evidence_ref")
    evidence_sha256 = handoff.get("evidence_sha256")
    if not _is_clean_text(evidence_ref) or not re.fullmatch(r"[0-9a-f]{64}", str(evidence_sha256 or "")):
        return ChangeGateReadiness(False, ["CHANGE_GATE_EVIDENCE_INVALID"])
    evidence_path = Path(str(evidence_ref))
    if not evidence_path.is_absolute():
        evidence_path = _CANONICAL_CHANGE_GATE_ROOT / evidence_path
    try:
        evidence_snapshot = _read_trusted_file(
            _CANONICAL_CHANGE_GATE_ROOT, evidence_path, limit=_CHANGE_GATE_PACKET_LIMIT
        )
        validator_source = _read_trusted_file(
            _CANONICAL_CHANGE_GATE_ROOT,
            _CANONICAL_CHANGE_GATE_ROOT / "scripts/change_gate_validate.py",
            limit=_CHANGE_GATE_POLICY_LIMIT,
        )
        policy_snapshot = _read_trusted_file(
            _CANONICAL_CHANGE_GATE_ROOT,
            _CANONICAL_CHANGE_GATE_ROOT / "policies/change-gate/policy.yaml",
            limit=_CHANGE_GATE_POLICY_LIMIT,
        )
        schema_paths = [
            _CANONICAL_CHANGE_GATE_ROOT / "policies/change-gate" / name
            for name in (
                "evidence-packet.schema.json",
                "frozen-handoff.schema.json",
                "scope-deviation.schema.json",
                "review-result.schema.json",
            )
        ]
        schema_snapshots = [
            _read_trusted_file(_CANONICAL_CHANGE_GATE_ROOT, path, limit=_CHANGE_GATE_POLICY_LIMIT)
            for path in schema_paths
        ]
    except _TrustedReadError:
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_UNSAFE"])
    if evidence_snapshot.sha256 != evidence_sha256:
        return ChangeGateReadiness(False, ["CHANGE_GATE_EVIDENCE_INVALID"])

    validator = None
    try:
        validator = _load_change_gate_validator(validator_source)
        validator.validate_artifact(
            "handoff",
            handoff_snapshot.path,
            canonical_root=_CANONICAL_CHANGE_GATE_ROOT,
            schema_dir=_CANONICAL_CHANGE_GATE_ROOT / "policies/change-gate",
            policy_path=_CANONICAL_CHANGE_GATE_ROOT / "policies/change-gate/policy.yaml",
        )
    except Exception as exc:
        validation_error = getattr(validator, "ValidationError", None)
        if validation_error is not None and isinstance(exc, validation_error):
            return ChangeGateReadiness(
                False,
                [_map_validator_failure(
                    getattr(exc, "code", ""),
                    handoff,
                    task,
                    getattr(exc, "path", None),
                )],
            )
        return ChangeGateReadiness(False, ["CHANGE_GATE_VALIDATOR_UNAVAILABLE"])

    try:
        post_snapshots = [
            _read_trusted_file(_CANONICAL_CHANGE_GATE_ROOT, path, limit=_CHANGE_GATE_POLICY_LIMIT)
            for path in schema_paths
        ]
        post_snapshots.append(
            _read_trusted_file(
                _CANONICAL_CHANGE_GATE_ROOT,
                _CANONICAL_CHANGE_GATE_ROOT / "policies/change-gate/policy.yaml",
                limit=_CHANGE_GATE_POLICY_LIMIT,
            )
        )
        post_snapshots.append(
            _read_trusted_file(
                _CANONICAL_CHANGE_GATE_ROOT,
                _CANONICAL_CHANGE_GATE_ROOT / "scripts/change_gate_validate.py",
                limit=_CHANGE_GATE_POLICY_LIMIT,
            )
        )
        post_snapshots.append(
            _read_trusted_file(
                _CANONICAL_CHANGE_GATE_ROOT,
                evidence_snapshot.path,
                limit=_CHANGE_GATE_PACKET_LIMIT,
            )
        )
        post_snapshots.append(
            _read_trusted_file(
                Path(attachment_root), handoff_snapshot.path, limit=_CHANGE_GATE_PACKET_LIMIT
            )
        )
    except _TrustedReadError:
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_UNSAFE"])
    pre_snapshots = schema_snapshots + [
        policy_snapshot,
        validator_source,
        evidence_snapshot,
        handoff_snapshot,
    ]
    if any(not _snapshot_same(before, after) for before, after in zip(pre_snapshots, post_snapshots)):
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_UNSAFE"])
    if _validator_module_digest != validator_source.sha256:
        return ChangeGateReadiness(False, ["CHANGE_GATE_ARTIFACT_UNSAFE"])

    if handoff.get("task_id") != _task_get(task, "id", None):
        return ChangeGateReadiness(False, ["CHANGE_GATE_TASK_MISMATCH"])
    route = handoff.get("execution_route")
    if not isinstance(route, dict):
        return ChangeGateReadiness(False, ["CHANGE_GATE_SCHEMA_INVALID"])
    if route.get("profile") != _task_get(task, "assignee", None):
        return ChangeGateReadiness(False, ["CHANGE_GATE_PROFILE_MISMATCH"])
    if route.get("model") != _task_get(task, "model_override", None):
        return ChangeGateReadiness(False, ["CHANGE_GATE_MODEL_MISMATCH"])
    if route.get("reasoning_effort") != "xhigh":
        return ChangeGateReadiness(False, ["CHANGE_GATE_EFFORT_MISMATCH"])
    bootstrap = handoff.get("bootstrap")
    if isinstance(bootstrap, dict) and bootstrap.get("active") is True:
        return ChangeGateReadiness(False, ["CHANGE_GATE_BOOTSTRAP_NOT_ALLOWED"], True)
    return ChangeGateReadiness(ok=True)


def _task_skills(task: Any) -> list[str]:
    raw = _task_get(task, "skills", None)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return _as_list(parsed)
    return _as_list(raw)


def _discover_profile_names() -> set[str]:
    names = {"default"}
    root = get_default_hermes_root()
    profiles_root = root / "profiles"
    if profiles_root.exists():
        for path in profiles_root.iterdir():
            if path.is_dir() and path.name.strip():
                names.add(path.name)
    active_profile = root / "profile.json"
    if active_profile.exists():
        try:
            data = json.loads(active_profile.read_text())
            name = str(data.get("name") or "").strip()
            if name:
                names.add(name)
        except Exception:
            pass
    return names


def ready_check_task(
    task: Any,
    *,
    existing_profiles: Optional[set[str]] = None,
    toolset_names: Optional[set[str]] = None,
    target_status: Optional[str] = None,
) -> ReadyCheckResult:
    """Return pickup-readiness diagnostics without mutating the task or DB."""
    contract = parse_contract_body(_task_get(task, "body", ""))
    profiles = set(existing_profiles) if existing_profiles is not None else _discover_profile_names()
    toolsets = {name.casefold() for name in (toolset_names if toolset_names is not None else set(get_toolset_names()))}

    status = str(target_status or _task_get(task, "status", "") or "").strip().lower()
    # ``recommended_assignee`` is advisory contract metadata only. Pickup
    # readiness must reflect the executable Kanban task assignment so a ready
    # card without ``task.assignee`` cannot become dispatchable by recommendation.
    assignee = str(_task_get(task, "assignee", "") or "").strip()
    assignee_valid = bool(assignee and assignee in profiles)

    missing: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    if not contract.parseable:
        missing.append("contract")
    if contract.lane is None:
        missing.append("lane")
    elif contract.lane not in VIRTUAL_LANES:
        errors.append("invalid_lane")
    if contract.lane == "implementation":
        if not contract.parseable or contract.change_gate is None:
            errors.append("CHANGE_GATE_METADATA_MISSING")
        else:
            if contract.change_gate.stage != "PLAN_APPROVED":
                errors.append("CHANGE_GATE_STAGE_INVALID")
            if contract.change_gate.role != "EXECUTOR":
                errors.append("CHANGE_GATE_ROLE_INVALID")
    if not contract.card_type:
        missing.append("type")
    if not contract.risk_class:
        missing.append("risk_class")
    if not contract.acceptance_criteria:
        missing.append("acceptance_criteria")
    if not contract.verification:
        missing.append("verification")
    if not contract.stop_conditions:
        missing.append("stop_conditions")
    if contract.human_required is not False and not contract.approval_boundary:
        missing.append("approval_boundary_resolved")
    if not assignee_valid:
        missing.append("assignee_profile")
    if contract.subagent_task_role and contract.subagent_task_role not in SUBAGENT_TASK_ROLES:
        errors.append("invalid_subagent_task_role")
    all_skills = contract.recommended_skills + _task_skills(task)
    if any(skill.casefold() in toolsets for skill in all_skills):
        errors.append("skill_is_toolset")
    if contract.lane == "review_safety" and not contract.review_source_pointer:
        errors.append("review_source_pointer")
    if contract.lane and contract.lane == assignee and contract.lane in VIRTUAL_LANES:
        errors.append("lane_used_as_assignee")

    if status != "ready":
        if status == "blocked":
            warnings.append("blocked cards are not dispatchable")
            action = "keep_blocked"
        else:
            warnings.append("task is not ready")
            action = "keep_not_ready"
    elif not assignee_valid:
        action = "assign_real_profile"
    elif missing or errors:
        action = "complete_contract"
    else:
        action = "pickup_ready"

    pickup_ready = status == "ready" and assignee_valid and not missing and not errors
    return ReadyCheckResult(
        pickup_ready=pickup_ready,
        missing_fields=missing,
        errors=errors,
        warnings=warnings,
        recommended_next_action=action,
        assignee_valid=assignee_valid,
        contract=contract,
    )
