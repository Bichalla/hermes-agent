from __future__ import annotations

import json

from hermes_cli import kanban_lane_roles as klr


def _contract_body(**overrides):
    body = {
        "lane": "planning",
        "type": "implementation_plan",
        "risk_class": "S3",
        "human_required": True,
        "approval_boundary": ["commit", "push"],
        "repository_or_root": "/tmp/repo",
        "acceptance_criteria": ["tests pass"],
        "verification": {"command": "pytest"},
        "stop_conditions": ["live mutation requires approval"],
        "recommended_assignee": "default",
        "recommended_skills": ["writing-plans"],
        "subagent_task_role": "reviewer",
    }
    body.update(overrides)
    return body


def test_parse_contract_accepts_virtual_lane_without_treating_it_as_profile():
    contract = klr.parse_contract_body(_contract_body(lane="planning", recommended_assignee="default"))

    assert contract.lane == "planning"
    assert contract.recommended_assignee == "default"
    assert contract.lane != contract.recommended_assignee


def test_parse_contract_accepts_top_level_contract_fields():
    contract = klr.parse_contract_body(_contract_body(lane="implementation", type="code_change"))

    assert contract.lane == "implementation"
    assert contract.card_type == "code_change"
    assert contract.risk_class == "S3"
    assert contract.acceptance_criteria == ["tests pass"]
    assert contract.verification == {"command": "pytest"}


def test_parse_contract_accepts_existing_intake_contract_envelope():
    envelope = {
        "source_ref": "kp_safe",
        "domain": "lifelog-core",
        "tenant": "lifelog",
        "status": "blocked",
        "why": "explicit card request",
        "contract": _contract_body(lane="review_safety", type="plan_review"),
        "safety": {"dispatch": False, "ready_or_running": False},
    }

    contract = klr.parse_contract_body(json.dumps(envelope, ensure_ascii=False))

    assert contract.lane == "review_safety"
    assert contract.card_type == "plan_review"
    assert contract.repository_or_root == "/tmp/repo"
    assert not hasattr(contract, "source_ref")


def test_pickup_ready_requires_real_assignee_profile():
    result = klr.ready_check_task(
        {"status": "ready", "assignee": "missing", "body": json.dumps(_contract_body())},
        existing_profiles={"default"},
    )

    assert result.pickup_ready is False
    assert result.assignee_valid is False
    assert "assignee_profile" in result.missing_fields
    assert result.recommended_next_action == "assign_real_profile"


def test_recommended_assignee_does_not_count_as_real_task_assignee():
    result = klr.ready_check_task(
        {"status": "ready", "assignee": "", "body": json.dumps(_contract_body(recommended_assignee="default"))},
        existing_profiles={"default"},
    )

    assert result.pickup_ready is False
    assert result.assignee_valid is False
    assert "assignee_profile" in result.missing_fields
    assert result.recommended_next_action == "assign_real_profile"


def test_profile_discovery_uses_root_profiles_when_active_home_is_named_profile(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    active_home = root / "profiles" / "worker-a"
    sibling_profile = root / "profiles" / "worker-b"
    active_home.mkdir(parents=True)
    sibling_profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(active_home))

    result = klr.ready_check_task(
        {"status": "ready", "assignee": "worker-b", "body": json.dumps(_contract_body())},
        toolset_names={"terminal", "web"},
    )

    assert result.pickup_ready is True
    assert result.assignee_valid is True
    assert result.missing_fields == []
    assert result.errors == []


def test_pickup_ready_rejects_blocked_status():
    result = klr.ready_check_task(
        {"status": "blocked", "assignee": "default", "body": json.dumps(_contract_body())},
        existing_profiles={"default"},
    )

    assert result.pickup_ready is False
    assert "blocked cards are not dispatchable" in result.warnings
    assert result.recommended_next_action == "keep_blocked"


def test_contract_rejects_toolset_names_in_skills():
    result = klr.ready_check_task(
        {"status": "ready", "assignee": "default", "body": json.dumps(_contract_body(recommended_skills=["terminal"]))},
        existing_profiles={"default"},
        toolset_names={"terminal", "web"},
    )

    assert result.pickup_ready is False
    assert "skill_is_toolset" in result.errors


def test_contract_reports_missing_acceptance_criteria_verification_stop_conditions():
    body = _contract_body(acceptance_criteria=[], verification=None, stop_conditions=[])

    result = klr.ready_check_task(
        {"status": "ready", "assignee": "default", "body": json.dumps(body)},
        existing_profiles={"default"},
    )

    assert result.pickup_ready is False
    assert "acceptance_criteria" in result.missing_fields
    assert "verification" in result.missing_fields
    assert "stop_conditions" in result.missing_fields


def test_lane_mapping_reuses_existing_subagent_role_names_without_renaming_them():
    contract = klr.parse_contract_body(_contract_body(subagent_task_role="focused_reviewer"))

    assert contract.subagent_task_role == "focused_reviewer"
    assert contract.subagent_task_role in klr.SUBAGENT_TASK_ROLES


def test_unknown_subagent_task_role_is_error_not_assignee_fallback():
    result = klr.ready_check_task(
        {"status": "ready", "assignee": "default", "body": json.dumps(_contract_body(subagent_task_role="planner_bot"))},
        existing_profiles={"default", "planner_bot"},
    )

    assert result.pickup_ready is False
    assert "invalid_subagent_task_role" in result.errors
    assert result.assignee_valid is True
