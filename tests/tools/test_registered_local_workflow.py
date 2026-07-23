"""Service-gated pending-only registered local workflow tool tests."""

from __future__ import annotations

import json

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    bind_active_workflow_turn,
    bind_current_turn_user_authority,
    clear_current_turn_user_authority,
    fingerprint_user_action,
    fingerprint_workflow_target,
)


@pytest.fixture(autouse=True)
def _clear_context():
    clear_current_turn_user_authority()
    tokens = set_session_vars()
    clear_session_vars(tokens)
    yield
    clear_current_turn_user_authority()
    clear_session_vars([])


def _authority(*, target: str, operation: str):
    authority = CurrentTurnUserAuthority(
        turn_id="turn-do-not-disclose",
        source_role="user",
        session_scope="session-1",
        platform_scope="discord",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("confirmed current action"),
        source_event_fingerprint=fingerprint_user_action("source event message-1"),
        allowed_action_classes=frozenset({"registered_soft_delete"}),
        allowed_operations=frozenset({operation}),
        operation_target_grants=frozenset(
            {(operation, fingerprint_workflow_target(target))}
        ),
        target_fingerprints=frozenset({fingerprint_workflow_target(target)}),
    )
    bind_active_workflow_turn(
        authority.turn_id, authority.platform_scope, authority.session_scope
    )
    return authority


def _set_gateway_context(
    *, session_id: str = "session-1", thread_id: str | None = "thread-1"
):
    return set_session_vars(
        platform="discord",
        chat_id="channel-1",
        thread_id=thread_id or "",
        user_id="user-1",
        session_key="session-key-1",
        session_id=session_id,
        message_id="message-1",
    )


def test_schema_exposes_pending_only_and_no_authority_path_db_or_command_fields():
    from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA

    params = REGISTERED_LOCAL_WORKFLOW_SCHEMA["parameters"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"action", "pending_id", "reason_code"}
    assert params["properties"]["action"]["enum"] == [
        "pending_read",
        "pending_restore",
        "pending_soft_delete",
    ]
    exposed_keys = set(params["properties"])
    for forbidden in (
        "approved",
        "authority",
        "db",
        "path",
        "command",
        "script",
        "sql",
        "payload",
    ):
        assert forbidden not in exposed_keys


@pytest.mark.parametrize(
    "action",
    ["hard_delete", "lifelog_diet_confirmed_create", "start-or-reconcile"],
)
def test_out_of_scope_owner_actions_are_unregistered_zero_write(action):
    import tools.registered_local_workflow as tool

    result = tool.registered_local_workflow(action=action)
    assert result["decision"] == "deny_unregistered_action"
    assert result["write_count"] == 0
    assert result["prompt_count"] == 0


def test_direct_dispatch_denies_when_feature_or_authority_missing(monkeypatch):
    import tools.registered_local_workflow as tool

    pending_id = "kp_0123456789abcdef"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=pending_id, operation="pending_read")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: False)
    assert tool.registered_local_workflow(
        action="pending_read", pending_id=pending_id
    )["decision"] == "deny_owner_unavailable"

    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    clear_current_turn_user_authority()
    assert tool.registered_local_workflow(
        action="pending_read", pending_id=pending_id
    )["decision"] == "deny_authority_missing"
    clear_session_vars(tokens)


def test_registry_direct_dispatch_repeats_handler_gate(monkeypatch):
    import tools.registered_local_workflow as tool
    from tools.registry import registry

    pending_id = "kp_0123456789abcdef"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=pending_id, operation="pending_read")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: False)
    result = json.loads(
        registry.dispatch(
            "registered_local_workflow",
            {"action": "pending_read", "pending_id": pending_id},
        )
    )
    assert result["decision"] == "deny_owner_unavailable"
    assert result["prompt_count"] == 0
    clear_session_vars(tokens)


def test_registry_visibility_is_absent_when_check_fails_even_if_requested(monkeypatch):
    import tools.registered_local_workflow  # noqa: F401
    from tools.registry import invalidate_check_fn_cache, registry

    entry = registry.get_entry("registered_local_workflow")
    assert entry is not None
    monkeypatch.setattr(entry, "check_fn", lambda: False)
    invalidate_check_fn_cache()
    assert registry.get_definitions({"registered_local_workflow"}, quiet=True) == []


def test_pending_operation_requires_exact_target_operation_and_gateway_scope(monkeypatch):
    import tools.registered_local_workflow as tool

    pending_id = "kp_0123456789abcdef"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=pending_id, operation="pending_soft_delete")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_pending_owner_action",
        lambda **_kwargs: {"status": "dismissed", "replayed": False},
    )
    result = tool.registered_local_workflow(
        action="pending_soft_delete",
        pending_id=pending_id,
        reason_code="user_dismissed",
    )
    assert result["decision"] == "allow"
    assert result["write_count"] == 1

    wrong = tool.registered_local_workflow(
        action="pending_restore", pending_id=pending_id
    )
    assert wrong["decision"] == "deny_authority_missing"
    clear_session_vars(tokens)


def test_cross_session_authority_is_denied_before_pending_owner(monkeypatch):
    import tools.registered_local_workflow as tool

    pending_id = "kp_0123456789abcdef"
    tokens = _set_gateway_context(session_id="different-session")
    bind_current_turn_user_authority(
        _authority(target=pending_id, operation="pending_read")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_pending_owner_action",
        lambda **_kwargs: pytest.fail("owner called"),
    )
    result = tool.registered_local_workflow(
        action="pending_read", pending_id=pending_id
    )
    assert result["decision"] == "deny_authority_missing"
    clear_session_vars(tokens)


def test_matching_threadless_gateway_binding_reaches_pending_owner(monkeypatch):
    import tools.registered_local_workflow as tool

    pending_id = "kp_0123456789abcdef"
    tokens = _set_gateway_context(thread_id=None)
    bind_current_turn_user_authority(
        _authority(target=pending_id, operation="pending_read")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_pending_owner_action",
        lambda **_kwargs: {"status": "pending", "replayed": False},
    )
    result = tool.registered_local_workflow(
        action="pending_read", pending_id=pending_id
    )
    assert result["decision"] == "allow"
    assert result["write_count"] == 0
    clear_session_vars(tokens)


def test_pending_extra_payload_and_invalid_reason_are_rejected(monkeypatch):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(tool, "_owner_ready", lambda _action: True)
    assert tool.registered_local_workflow(
        action="pending_read",
        pending_id="kp_0123456789abcdef",
        payload={},
    )["decision"] == "deny_schema_invalid"
    assert tool.registered_local_workflow(
        action="pending_soft_delete",
        pending_id="kp_0123456789abcdef",
        reason_code="hard_delete",
    )["decision"] == "deny_schema_invalid"


def test_check_fn_requires_false_by_default_flag_and_pending_readiness(monkeypatch):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(tool, "_feature_enabled", lambda: False)
    assert tool.check_registered_workflow_requirements() is False
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    assert tool.check_registered_workflow_requirements() is True


def test_registry_dispatch_accepts_injected_task_context(monkeypatch):
    import tools.registered_local_workflow as tool
    from tools.registry import registry

    monkeypatch.setattr(
        tool,
        "registered_local_workflow",
        lambda **_kwargs: {"decision": "allow", "prompt_count": 0, "write_count": 0},
    )
    raw = registry.dispatch(
        "registered_local_workflow",
        {"action": "pending_read", "pending_id": "kp_0123456789abcdef"},
        task_id="runtime-injected-task",
    )
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result["decision"] == "allow"
    assert result["prompt_count"] == 0
    assert result["write_count"] == 0


def test_registered_workflow_toolset_is_default_off_and_not_in_core():
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _DEFAULT_OFF_TOOLSETS
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    configurable = {name for name, _label, _description in CONFIGURABLE_TOOLSETS}
    assert "registered-workflow" in configurable
    assert "registered-workflow" in _DEFAULT_OFF_TOOLSETS
    assert TOOLSETS["registered-workflow"]["tools"] == [
        "registered_local_workflow"
    ]
    assert "registered_local_workflow" not in _HERMES_CORE_TOOLS
