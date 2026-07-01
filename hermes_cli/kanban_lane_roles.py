"""Read-only lane/role contract helpers for Hermes Kanban cards.

Phase 1 lane/role mapping treats ``lane`` as card metadata only. It never
promotes a task, never dispatches work, and never treats virtual lanes or
subagent task roles as executable Hermes profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
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


def parse_contract_body(body: Any) -> LaneRoleContract:
    """Parse top-level or conversational-intake-envelope contract metadata."""
    parsed = _body_to_dict(body)
    if parsed is None:
        return LaneRoleContract(parseable=False)
    payload = _contract_payload(parsed)
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
        parseable=True,
    )


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
    if any(skill.casefold() in toolsets for skill in contract.recommended_skills):
        errors.append("skill_is_toolset")
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
