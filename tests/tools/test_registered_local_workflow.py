"""Service-gated registered local workflow tool tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools.workflow_authority import (
    CurrentTurnUserAuthority,
    _mint_host_current_turn_user_authority,
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
    action_class = (
        "trusted_local_record"
        if operation in {
            "diet_intake_record",
            "childcare_event_record",
            "company_work_os_initial_seed_record",
        }
        else (
            "approval_required_live_mutation"
            if operation == "semantic_debug_issue"
            else "registered_soft_delete"
        )
    )
    authority = CurrentTurnUserAuthority(
        turn_id="turn-do-not-disclose",
        source_role="user",
        session_scope="session-1",
        platform_scope="discord",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action("confirmed current action"),
        source_event_fingerprint=fingerprint_user_action("source event message-1"),
        allowed_action_classes=frozenset({action_class}),
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


def _host_authority(*, target: str, operation: str):
    authority = _authority(target=target, operation=operation)
    return _mint_host_current_turn_user_authority(
        **{
            field: getattr(authority, field)
            for field in CurrentTurnUserAuthority.__dataclass_fields__
            if field != "host_seal"
        }
    )


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


def test_schema_is_discriminated_oneof_and_preserves_every_closed_action_branch():
    from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA

    params = REGISTERED_LOCAL_WORKFLOW_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert set(params) == {"type", "oneOf"}
    branches = {
        branch["properties"]["action"]["const"]: branch
        for branch in params["oneOf"]
    }
    assert tuple(branches) == (
        "childcare_event_record",
        "diet_intake_record",
        "pending_read",
        "pending_soft_delete",
        "pending_restore",
        "semantic_debug_issue",
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
    )
    expected_properties = {
        "childcare_event_record": ("action", "payload_name"),
        "diet_intake_record": ("action", "payload_name"),
        "pending_read": ("action", "pending_id"),
        "pending_soft_delete": ("action", "pending_id", "reason_code"),
        "pending_restore": ("action", "pending_id"),
        "semantic_debug_issue": ("action", "run_id"),
        "company_work_os_initial_seed_preview": ("action",),
        "company_work_os_initial_seed_record": ("action",),
    }
    for action, branch in branches.items():
        assert branch["type"] == "object"
        assert branch["additionalProperties"] is False
        assert tuple(branch["properties"]) == expected_properties[action]
        assert tuple(branch["required"]) == expected_properties[action]
    exposed_keys = {
        key for branch in branches.values() for key in branch["properties"]
    }
    for forbidden in (
        "approved",
        "authority",
        "db",
        "path",
        "command",
        "script",
        "sql",
        "payload_path",
    ):
        assert forbidden not in exposed_keys


def _company_preview_owner_result(outcome="ready_insert"):
    zero = {
        "org_units": 0,
        "persons": 0,
        "positions": 0,
        "assignments": 0,
        "command_receipts": 0,
        "change_events": 0,
    }
    inserted = {
        "org_units": 2,
        "persons": 1,
        "positions": 2,
        "assignments": 2,
        "command_receipts": 1,
        "change_events": 1,
    }
    return {
        "schema": "company-work-os/initial-seed-preview-result/v1",
        "outcome": outcome,
        "target_token": "company-work-os:canonical-initial-seed",
        "payload_sha256": "a" * 64,
        "semantic_sha256": "b" * 64,
        "migration_versions": [1, 2, 3, 4, 5, 6, 7],
        "current_counts": inserted if outcome == "exact_replay" else zero,
        "expected_delta": inserted if outcome == "ready_insert" else zero,
    }


def _company_record_owner_result(outcome="inserted"):
    zero = {
        "org_units": 0,
        "persons": 0,
        "positions": 0,
        "assignments": 0,
        "command_receipts": 0,
        "change_events": 0,
    }
    inserted = {
        "org_units": 2,
        "persons": 1,
        "positions": 2,
        "assignments": 2,
        "command_receipts": 1,
        "change_events": 1,
    }
    return {
        "schema": "company-work-os/initial-seed-record-result/v1",
        "outcome": outcome,
        "target_token": "company-work-os:canonical-initial-seed",
        "semantic_sha256": "b" * 64,
        "committed_delta": (
            inserted if outcome == "inserted" else None if outcome == "manual_recovery_required" else zero
        ),
        "receipt_count": 1 if outcome in {"inserted", "existing"} else None,
        "event_count": 1 if outcome in {"inserted", "existing"} else None,
    }


def test_semantic_debug_issue_requires_authenticated_current_turn_exact_target(monkeypatch):
    import tools.registered_local_workflow as tool

    run_id = "semantic-debug-impact-v3-oauth-20260725-001"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _host_authority(target=run_id, operation="semantic_debug_issue")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    calls = []
    monkeypatch.setattr(
        tool,
        "_semantic_debug_issue_owner_action",
        lambda **kwargs: calls.append(kwargs) or {
            "run_id": run_id,
            "request_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "bundle_state": "committed",
        },
    )
    result = tool.registered_local_workflow(
        action="semantic_debug_issue", run_id=run_id,
    )
    assert result["decision"] == "allow"
    assert result["run_id"] == run_id and result["readback"] == "passed"
    assert result["provider_calls"] == 0 and result["network_actions"] == 0
    assert len(calls) == 1
    clear_session_vars(tokens)


def test_semantic_debug_issue_rejects_valid_seal_outside_exact_session_or_turn(monkeypatch):
    import tools.registered_local_workflow as tool

    run_id = "semantic-debug-impact-v3-oauth-20260725-001"
    for case in ("wrong_session", "wrong_active_turn", "stale_replay"):
        clear_current_turn_user_authority()
        tokens = _set_gateway_context(
            session_id="session-2" if case == "wrong_session" else "session-1"
        )
        authority = _host_authority(
            target=run_id, operation="semantic_debug_issue"
        )
        if case == "wrong_active_turn":
            bind_active_workflow_turn("different-turn", "discord", "session-1")
        elif case == "stale_replay":
            clear_current_turn_user_authority()
        bind_current_turn_user_authority(authority)
        calls = []
        monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
        monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
        monkeypatch.setattr(
            tool,
            "_semantic_debug_issue_owner_action",
            lambda **kwargs: calls.append(kwargs) or pytest.fail("owner called"),
        )

        result = tool.registered_local_workflow(
            action="semantic_debug_issue", run_id=run_id,
        )

        assert result["decision"] == "deny_authority_missing", case
        assert result["write_count"] == 0, case
        assert calls == [], case
        clear_session_vars(tokens)


def test_semantic_debug_issue_forged_or_missing_authority_publishes_nothing(monkeypatch):
    import tools.registered_local_workflow as tool

    run_id = "semantic-debug-impact-v3-oauth-20260725-001"
    tokens = _set_gateway_context()
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    calls = []
    monkeypatch.setattr(
        tool, "_semantic_debug_issue_owner_action",
        lambda **kwargs: calls.append(kwargs) or pytest.fail("owner called"),
    )
    missing = tool.registered_local_workflow(
        action="semantic_debug_issue", run_id=run_id,
    )
    assert missing["decision"] == "deny_authority_missing"
    assert missing["write_count"] == 0 and calls == []

    bind_current_turn_user_authority(
        _host_authority(
            target="semantic-debug-different-run-001",
            operation="semantic_debug_issue",
        )
    )
    forged = tool.registered_local_workflow(
        action="semantic_debug_issue", run_id=run_id,
    )
    assert forged["decision"] == "deny_authority_missing"
    assert forged["write_count"] == 0 and calls == []
    clear_session_vars(tokens)


def test_semantic_debug_issue_self_minted_exact_authority_never_reaches_owner(monkeypatch):
    import tools.registered_local_workflow as tool

    run_id = "semantic-debug-impact-v3-oauth-20260725-001"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=run_id, operation="semantic_debug_issue")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    calls = []
    monkeypatch.setattr(
        tool,
        "_semantic_debug_issue_owner_action",
        lambda **kwargs: calls.append(kwargs) or pytest.fail("owner called"),
    )

    forged = tool.registered_local_workflow(
        action="semantic_debug_issue", run_id=run_id,
    )

    assert forged["decision"] == "deny_authority_missing"
    assert forged["write_count"] == 0
    assert calls == []
    clear_session_vars(tokens)


def test_semantic_debug_issue_copied_seal_cannot_authorize_changed_target(monkeypatch):
    import tools.registered_local_workflow as tool

    run_id = "semantic-debug-impact-v3-oauth-20260725-001"
    tokens = _set_gateway_context()
    valid = _host_authority(
        target="semantic-debug-different-run-001",
        operation="semantic_debug_issue",
    )
    changed = replace(
        valid,
        operation_target_grants=frozenset(
            {("semantic_debug_issue", fingerprint_workflow_target(run_id))}
        ),
    )
    bind_current_turn_user_authority(changed)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    calls = []
    monkeypatch.setattr(
        tool,
        "_semantic_debug_issue_owner_action",
        lambda **kwargs: calls.append(kwargs) or pytest.fail("owner called"),
    )

    result = tool.registered_local_workflow(
        action="semantic_debug_issue", run_id=run_id,
    )

    assert result["decision"] == "deny_authority_missing"
    assert result["write_count"] == 0
    assert calls == []
    clear_session_vars(tokens)


def test_childcare_record_requires_exact_authority_and_returns_closed_owner_evidence(monkeypatch):
    import tools.registered_local_workflow as tool

    target = "person_park_haesoo:childcare:fever"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=target, operation="childcare_event_record")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_childcare_payload_binding",
        lambda _name: tool.ChildcarePayloadBinding(
            path=Path("/synthetic/fever.json"),
            digest="a" * 64,
            target=target,
            receipt_key="b" * 64,
            payload_bytes=b"{}",
        ),
    )
    monkeypatch.setattr(
        tool,
        "_childcare_owner_action",
        lambda **_kwargs: {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "childcare_event.v1",
            "validation_status": "validator_and_readback_passed",
            "idempotency_result": "inserted",
            "event_ids": ["evt_childcare_v1_deadbeefdeadbeef"],
            "dry_run": False,
        },
    )
    result = tool.registered_local_workflow(
        action="childcare_event_record", payload_name="2026-07-24-fever.json"
    )
    assert result["decision"] == "allow"
    assert result["write_count"] == 1
    assert result["validation_status"] == "validator_and_readback_passed"
    assert result["idempotency_result"] == "inserted"
    assert result["event_ids"] == ["evt_childcare_v1_deadbeefdeadbeef"]
    clear_session_vars(tokens)


@pytest.mark.parametrize(
    "payload_name",
    ["../escape.json", "/tmp/escape.json", "nested/file.json", "event.txt", ""],
)
def test_childcare_record_rejects_non_basename_payloads_before_owner(monkeypatch, payload_name):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(
        tool,
        "_childcare_owner_action",
        lambda **_kwargs: pytest.fail("owner called"),
    )
    result = tool.registered_local_workflow(
        action="childcare_event_record", payload_name=payload_name
    )
    assert result["decision"] == "deny_schema_invalid"
    assert result["write_count"] == 0


def test_diet_record_requires_exact_authority_and_returns_closed_owner_evidence(monkeypatch):
    import tools.registered_local_workflow as tool

    target = "person_park_sanghyun:diet"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=target, operation="diet_intake_record")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(tool, "_diet_payload_matches_session", lambda _name: True)
    monkeypatch.setattr(
        tool,
        "_diet_owner_action",
        lambda **_kwargs: {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "diet_intake.v1",
            "validation_status": "validator_and_readback_passed",
            "idempotency_result": "inserted",
            "event_ids": ["diet_v1_deadbeefdeadbeef"],
            "dry_run": False,
        },
    )
    result = tool.registered_local_workflow(
        action="diet_intake_record", payload_name="2026-07-24-breakfast.json"
    )
    assert result["decision"] == "allow"
    assert result["write_count"] == 1
    assert result["validation_status"] == "validator_and_readback_passed"
    assert result["idempotency_result"] == "inserted"
    assert result["event_ids"] == ["diet_v1_deadbeefdeadbeef"]
    clear_session_vars(tokens)


@pytest.mark.parametrize(
    "payload_name",
    ["../escape.json", "/tmp/escape.json", "nested/file.json", "meal.txt", ""],
)
def test_diet_record_rejects_non_basename_payloads_before_owner(monkeypatch, payload_name):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(
        tool,
        "_diet_owner_action",
        lambda **_kwargs: pytest.fail("owner called"),
    )
    result = tool.registered_local_workflow(
        action="diet_intake_record", payload_name=payload_name
    )
    assert result["decision"] == "deny_schema_invalid"
    assert result["write_count"] == 0


def test_childcare_owner_invokes_fixed_dispatcher_dry_run_then_live(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True)
    payload = payload_root / "event.json"
    payload.write_text("{}", encoding="utf-8")
    payload.chmod(0o600)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    dispatcher.parent.mkdir()
    dispatcher.write_text("# synthetic dispatcher\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        dry_run = argv[argv.index("--dry-run") + 1] == "true"
        result = {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "childcare_event.v1",
            "exit_status": 0,
            "validation_status": (
                "payload_validated" if dry_run else "validator_and_readback_passed"
            ),
            "idempotency_result": (
                "not_applicable_dry_run" if dry_run else "inserted"
            ),
            "event_ids": ["evt_childcare_v1_deadbeefdeadbeef"],
            "dry_run": dry_run,
        }
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(result), "stderr": ""},
        )()

    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    binding = tool.ChildcarePayloadBinding(
        path=payload,
        digest=hashlib.sha256(payload.read_bytes()).hexdigest(),
        target="person_park_haesoo:childcare:fever",
        receipt_key="c" * 64,
        payload_bytes=payload.read_bytes(),
    )
    result = tool._childcare_owner_action(binding=binding)
    assert result["validation_status"] == "validator_and_readback_passed"
    assert [call[0][call[0].index("--dry-run") + 1] for call in calls] == [
        "true",
        "false",
    ]
    assert all("childcare_event.v1" in call[0] for call in calls)
    assert all(call[1]["cwd"] == root for call in calls)
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["env"] == {"PATH": os.environ.get("PATH", "")} for call in calls)


def test_childcare_payload_must_bind_to_current_gateway_source(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True)
    payload = payload_root / "event.json"
    document = {
        "category": "health",
        "subcategory": "fever_followup",
        "child_person_id": "person_park_haesoo",
        "metrics": {"temperature_c": 38.6},
        "source": {
            "platform": "discord",
            "channel_id": "channel-1",
            "thread_id": "thread-1",
            "message_id": "message-1",
        }
    }
    payload.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    tokens = _set_gateway_context()
    binding = tool._childcare_payload_binding("event.json")
    assert binding is not None
    assert binding.target == "person_park_haesoo:childcare:fever"
    document["source"]["message_id"] = "old-message"
    payload.write_text(json.dumps(document), encoding="utf-8")
    assert tool._childcare_payload_binding("event.json") is None
    clear_session_vars(tokens)


def test_childcare_semantic_scope_mismatch_denies_before_owner(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True)
    payload = payload_root / "medication.json"
    payload.write_text(
        json.dumps(
            {
                "category": "medication",
                "subcategory": "medication_intake",
                "child_person_id": "person_park_haesoo",
                "source": {
                    "platform": "discord",
                    "channel_id": "channel-1",
                    "thread_id": "thread-1",
                    "message_id": "message-1",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_childcare_owner_action",
        lambda **_kwargs: pytest.fail("owner called for semantic mismatch"),
    )
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(
            target="person_park_haesoo:childcare:fever",
            operation="childcare_event_record",
        )
    )
    result = tool.registered_local_workflow(
        action="childcare_event_record", payload_name="medication.json"
    )
    assert result["decision"] == "deny_authority_missing"
    assert result["write_count"] == 0
    clear_session_vars(tokens)


def test_childcare_same_source_fact_rejects_payload_digest_variation(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    dispatcher.parent.mkdir()
    dispatcher.write_text("# synthetic dispatcher\n", encoding="utf-8")
    payload = payload_root / "event.json"
    payload.write_bytes(b'{"version":1}')
    calls = []

    def fake_invoke(**kwargs):
        calls.append(kwargs["dry_run"])
        return {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "childcare_event.v1",
            "exit_status": 0,
            "validation_status": (
                "payload_validated"
                if kwargs["dry_run"]
                else "validator_and_readback_passed"
            ),
            "idempotency_result": (
                "not_applicable_dry_run" if kwargs["dry_run"] else "inserted"
            ),
            "event_ids": ["evt_childcare_v1_deadbeefdeadbeef"],
            "dry_run": kwargs["dry_run"],
        }

    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_invoke_childcare_dispatcher", fake_invoke)
    first_bytes = payload.read_bytes()
    first = tool.ChildcarePayloadBinding(
        path=payload,
        digest=hashlib.sha256(first_bytes).hexdigest(),
        target="person_park_haesoo:childcare:fever",
        receipt_key="d" * 64,
        payload_bytes=first_bytes,
    )
    tool._childcare_owner_action(binding=first)
    payload.write_bytes(b'{"version":2}')
    second_bytes = payload.read_bytes()
    second = tool.ChildcarePayloadBinding(
        path=payload,
        digest=hashlib.sha256(second_bytes).hexdigest(),
        target=first.target,
        receipt_key=first.receipt_key,
        payload_bytes=second_bytes,
    )
    with pytest.raises(RuntimeError, match="receipt conflict"):
        tool._childcare_owner_action(binding=second)
    assert calls == [True, False]


def test_childcare_original_payload_mutation_after_dry_run_cannot_change_live_bytes(
    monkeypatch, tmp_path
):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    dispatcher.parent.mkdir()
    dispatcher.write_text("# synthetic dispatcher\n", encoding="utf-8")
    payload = payload_root / "event.json"
    original = b'{"temperature_c":38.6}'
    payload.write_bytes(original)
    observed_live = []

    def fake_invoke(**kwargs):
        if kwargs["dry_run"]:
            payload.write_bytes(b'{"temperature_c":99.9}')
        else:
            observed_live.append(kwargs["payload"].read_bytes())
        return {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "childcare_event.v1",
            "exit_status": 0,
            "validation_status": (
                "payload_validated"
                if kwargs["dry_run"]
                else "validator_and_readback_passed"
            ),
            "idempotency_result": (
                "not_applicable_dry_run" if kwargs["dry_run"] else "inserted"
            ),
            "event_ids": ["evt_childcare_v1_deadbeefdeadbeef"],
            "dry_run": kwargs["dry_run"],
        }

    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_invoke_childcare_dispatcher", fake_invoke)
    binding = tool.ChildcarePayloadBinding(
        path=payload,
        digest=hashlib.sha256(original).hexdigest(),
        target="person_park_haesoo:childcare:fever",
        receipt_key="e" * 64,
        payload_bytes=original,
    )
    tool._childcare_owner_action(binding=binding)
    assert observed_live == [original]


def test_diet_owner_invokes_fixed_dispatcher_dry_run_then_live(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "diet-intake"
    payload_root.mkdir(parents=True)
    payload = payload_root / "meal.json"
    payload.write_text("{}", encoding="utf-8")
    payload.chmod(0o600)
    dispatcher = root / "scripts" / "run_registered_recorder.py"
    dispatcher.parent.mkdir()
    dispatcher.write_text("# synthetic dispatcher\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        dry_run = argv[argv.index("--dry-run") + 1] == "true"
        result = {
            "schema": "registered-recorder-result/v1",
            "recorder_id": "diet_intake.v1",
            "exit_status": 0,
            "validation_status": (
                "payload_validated" if dry_run else "validator_and_readback_passed"
            ),
            "idempotency_result": (
                "not_applicable_dry_run" if dry_run else "inserted"
            ),
            "event_ids": ["diet_v1_deadbeefdeadbeef"],
            "dry_run": dry_run,
        }
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(result), "stderr": ""},
        )()

    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    result = tool._diet_owner_action(payload_name="meal.json")
    assert result["validation_status"] == "validator_and_readback_passed"
    assert [call[0][call[0].index("--dry-run") + 1] for call in calls] == [
        "true",
        "false",
    ]
    assert all(call[1]["cwd"] == root for call in calls)
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["env"] == {"PATH": os.environ.get("PATH", "")} for call in calls)


def test_diet_payload_must_bind_to_current_gateway_source(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    payload_root = root / ".runtime-inputs" / "diet-intake"
    payload_root.mkdir(parents=True)
    payload = payload_root / "meal.json"
    payload.write_text(
        json.dumps(
            {
                "source": {
                    "platform": "discord",
                    "channel_id": "channel-1",
                    "thread_id": "thread-1",
                    "message_id": "message-1",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    tokens = _set_gateway_context()
    assert tool._diet_payload_matches_session("meal.json") is True
    payload.write_text(
        json.dumps(
            {
                "source": {
                    "platform": "discord",
                    "channel_id": "channel-1",
                    "thread_id": "thread-1",
                    "message_id": "old-message",
                }
            }
        ),
        encoding="utf-8",
    )
    assert tool._diet_payload_matches_session("meal.json") is False
    clear_session_vars(tokens)


def test_childcare_owner_real_dispatcher_writes_temp_db_and_replays(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    canonical = Path.home() / ".hermes" / "ops" / "state" / "lifelog"
    root = tmp_path / "lifelog"
    (root / "config").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy2(canonical / "config" / "recorder-registry.json", root / "config")
    for name in ("run_registered_recorder.py", "record_childcare_event.py"):
        shutil.copy2(canonical / "scripts" / name, root / "scripts" / name)
    validator = canonical / "scripts" / "validate_lifelog.py"
    (root / "scripts" / "validate_lifelog.py").write_text(
        "import runpy\n"
        f"runpy.run_path({str(validator)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            str(canonical / "scripts" / "lifelog_migrate.py"),
            "--db",
            str(root / "lifelog.db"),
        ],
        cwd=canonical,
        check=True,
        text=True,
        capture_output=True,
    )
    payload_root = root / ".runtime-inputs" / "childcare-event"
    payload_root.mkdir(parents=True, mode=0o700)
    payload = payload_root / "event.json"
    payload.write_text(
        json.dumps(
            {
                "occurred_at": "2026-07-24T17:00:00+09:00",
                "category": "health",
                "subcategory": "fever_followup",
                "child_person_id": "person_park_haesoo",
                "caregiver_person_id": "person_park_sanghyun",
                "metrics": {"temperature_c": 38.6},
                "source": {
                    "platform": "discord",
                    "guild_id": "guild-1",
                    "channel_id": "channel-1",
                    "thread_id": "thread-1",
                    "message_id": "message-1",
                },
                "title": "Synthetic Haesoo fever follow-up",
                "notes": "Confirmed caregiver observation.",
            }
        ),
        encoding="utf-8",
    )
    payload.chmod(0o600)
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(
            target="person_park_haesoo:childcare:fever",
            operation="childcare_event_record",
        )
    )

    first = tool.registered_local_workflow(
        action="childcare_event_record", payload_name="event.json"
    )
    second = tool.registered_local_workflow(
        action="childcare_event_record", payload_name="event.json"
    )

    assert first["decision"] == "allow"
    assert first["validation_status"] == "validator_and_readback_passed"
    assert first["idempotency_result"] == "inserted"
    assert first["write_count"] == 1
    assert second["idempotency_result"] == "existing"
    assert second["write_count"] == 0
    assert first["event_ids"] == second["event_ids"]
    clear_session_vars(tokens)


def test_diet_owner_real_dispatcher_writes_temp_db_and_replays(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    canonical = Path.home() / ".hermes" / "ops" / "state" / "lifelog"
    root = tmp_path / "lifelog"
    (root / "config").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy2(canonical / "config" / "recorder-registry.json", root / "config")
    for name in ("run_registered_recorder.py", "record_diet_intake.py"):
        shutil.copy2(canonical / "scripts" / name, root / "scripts" / name)
    validator = canonical / "scripts" / "validate_lifelog.py"
    (root / "scripts" / "validate_lifelog.py").write_text(
        "import runpy\n"
        f"runpy.run_path({str(validator)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            str(canonical / "scripts" / "lifelog_migrate.py"),
            "--db",
            str(root / "lifelog.db"),
        ],
        cwd=canonical,
        check=True,
        text=True,
        capture_output=True,
    )
    payload_root = root / ".runtime-inputs" / "diet-intake"
    payload_root.mkdir(parents=True, mode=0o700)
    payload = payload_root / "meal.json"
    payload.write_text(
        json.dumps(
            {
                "schema_version": "diet_intake/v1",
                "intent": "confirmed_intake",
                "occurred_at": "2026-07-24T09:00:00+09:00",
                "timezone": "Asia/Seoul",
                "person_id": "person_park_sanghyun",
                "meal_label": "breakfast",
                "title": "Synthetic registered breakfast",
                "items": [{"name": "synthetic meal", "quantity_text": "1 serving"}],
                "nutrition_estimate": {},
                "tags": ["diet", "breakfast", "confirmed_intake"],
                "source": {
                    "platform": "discord",
                    "channel_id": "channel-1",
                    "thread_id": "thread-1",
                    "message_id": "message-1",
                },
            }
        ),
        encoding="utf-8",
    )
    payload.chmod(0o600)
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(
            target="person_park_sanghyun:diet",
            operation="diet_intake_record",
        )
    )

    first = tool.registered_local_workflow(
        action="diet_intake_record", payload_name="meal.json"
    )
    second = tool.registered_local_workflow(
        action="diet_intake_record", payload_name="meal.json"
    )

    assert first["decision"] == "allow"
    assert first["validation_status"] == "validator_and_readback_passed"
    assert first["idempotency_result"] == "inserted"
    assert first["write_count"] == 1
    assert second["idempotency_result"] == "existing"
    assert second["write_count"] == 0
    assert first["event_ids"] == second["event_ids"]
    clear_session_vars(tokens)


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
        "registered_local_workflow",
        "kanban_create_blocked",
    ]
    assert "registered_local_workflow" not in _HERMES_CORE_TOOLS
    assert "kanban_create_blocked" not in _HERMES_CORE_TOOLS


def test_company_preview_and_record_map_exact_closed_owner_results(monkeypatch):
    import tools.registered_local_workflow as tool

    target = "company-work-os:canonical-initial-seed"
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    owner_results = {
        "company_work_os_initial_seed_preview": _company_preview_owner_result(),
        "company_work_os_initial_seed_record": _company_record_owner_result(),
    }
    calls = []
    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda action: calls.append(action) or owner_results[action],
    )

    preview = tool.registered_local_workflow(
        action="company_work_os_initial_seed_preview"
    )
    assert tuple(preview) == (
        "schema",
        "decision",
        "prompt_count",
        "write_count",
        "action_id",
        "owner_result",
    )
    assert preview["decision"] == "allow"
    assert preview["write_count"] == 0
    assert preview["owner_result"] == owner_results["company_work_os_initial_seed_preview"]

    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=target, operation="company_work_os_initial_seed_record")
    )
    record = tool.registered_local_workflow(
        action="company_work_os_initial_seed_record"
    )
    assert tuple(record) == tuple(preview)
    assert record["decision"] == "allow"
    assert record["write_count"] == 9
    assert record["owner_result"] == owner_results["company_work_os_initial_seed_record"]
    assert calls == [
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
    ]
    clear_session_vars(tokens)


@pytest.mark.parametrize(
    "outcome,decision,write_count",
    [
        ("existing", "allow", 0),
        ("conflict", "allow", 0),
        ("manual_recovery_required", "hard_block", None),
    ],
)
def test_company_record_outcome_mapping_is_exact(
    monkeypatch, outcome, decision, write_count
):
    import tools.registered_local_workflow as tool

    target = "company-work-os:canonical-initial-seed"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=target, operation="company_work_os_initial_seed_record")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda _action: _company_record_owner_result(outcome),
    )
    result = tool.registered_local_workflow(
        action="company_work_os_initial_seed_record"
    )
    assert result["decision"] == decision
    assert result["write_count"] is write_count
    assert result["owner_result"]["outcome"] == outcome
    clear_session_vars(tokens)


def test_company_denials_are_base_plus_null_owner_and_never_launch(monkeypatch):
    import tools.registered_local_workflow as tool

    calls = []
    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda action: calls.append(action) or pytest.fail("fixed owner launched"),
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: False)
    preview = tool.registered_local_workflow(
        action="company_work_os_initial_seed_preview"
    )
    assert preview["decision"] == "deny_owner_unavailable"
    assert preview["write_count"] == 0
    assert preview["owner_result"] is None

    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    record = tool.registered_local_workflow(
        action="company_work_os_initial_seed_record"
    )
    assert record["decision"] == "deny_authority_missing"
    assert record["write_count"] == 0
    assert record["owner_result"] is None

    tokens = set_session_vars(
        platform="delegate",
        session_id="session-1",
        chat_id="channel-1",
        user_id="user-1",
        session_key="session-key-1",
    )
    delegated_preview = tool.registered_local_workflow(
        action="company_work_os_initial_seed_preview"
    )
    assert delegated_preview["decision"] == "deny_authority_missing"
    assert delegated_preview["owner_result"] is None
    assert calls == []
    clear_session_vars(tokens)


def test_company_action_only_schema_rejects_every_caller_selected_target_field(monkeypatch):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda _action: pytest.fail("fixed owner launched"),
    )
    for action in (
        "company_work_os_initial_seed_preview",
        "company_work_os_initial_seed_record",
    ):
        for key, value in (
            ("path", "/tmp/other"),
            ("database", "/tmp/other.sqlite"),
            ("payload", {}),
            ("target", "company-work-os:other"),
            ("command", "record"),
            ("authority", True),
        ):
            result = tool.registered_local_workflow(action=action, **{key: value})
            assert result["decision"] == "deny_schema_invalid"
            assert result["write_count"] == 0
            assert result["owner_result"] is None


def test_company_owner_fixed_subprocess_contract_and_interpreter_identity():
    import tools.registered_local_workflow as tool

    config = tool._PRODUCTION_COMPANY_WORK_OS_OWNER
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(_company_preview_owner_result()).encode("utf-8"),
            stderr=b"",
        )

    result = tool._invoke_company_work_os_owner_at(
        action="company_work_os_initial_seed_preview",
        config=config,
        runner=fake_runner,
    )
    assert result == _company_preview_owner_result()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        str(config.interpreter),
        "-I",
        "-B",
        str(config.owner_script),
        "preview",
    ]
    assert kwargs == {
        "cwd": config.project_root,
        "env": {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/Users/honbul",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 30,
        "shell": False,
        "check": False,
    }
    assert config.interpreter == Path(
        "/Users/honbul/.hermes/hermes-agent/venv/bin/python"
    )
    assert config.interpreter_resolved == Path(
        "/Users/honbul/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11"
    )
    assert config.interpreter_sha256 == (
        "4c78423e7d5986362ac04df40edb18cdd1174f9818d653402e3abbd2a5bbf793"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "empty",
        "multiple",
        "malformed",
        "oversized",
        "stderr",
        "nonzero",
        "unknown-key",
        "unknown-outcome",
        "wrong-type",
        "timeout",
    ],
)
def test_company_owner_unavailable_failures_are_closed_zero_write(monkeypatch, mode):
    import tools.registered_local_workflow as tool

    target = "company-work-os:canonical-initial-seed"
    tokens = _set_gateway_context()
    bind_current_turn_user_authority(
        _authority(target=target, operation="company_work_os_initial_seed_record")
    )
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    valid = _company_record_owner_result()

    def fake_runner(argv, **_kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        stdout = json.dumps(valid).encode("utf-8")
        stderr = b""
        returncode = 0
        if mode == "empty":
            stdout = b""
        elif mode == "multiple":
            stdout += stdout
        elif mode == "malformed":
            stdout = b"not-json"
        elif mode == "oversized":
            stdout = b" " * 16385
        elif mode == "stderr":
            stderr = b"private child error"
        elif mode == "nonzero":
            returncode = 2
        elif mode == "unknown-key":
            value = dict(valid, private_name="must not reflect")
            stdout = json.dumps(value).encode("utf-8")
        elif mode == "unknown-outcome":
            value = dict(valid, outcome="repaired")
            stdout = json.dumps(value).encode("utf-8")
        elif mode == "wrong-type":
            value = dict(valid, receipt_count=True)
            stdout = json.dumps(value).encode("utf-8")
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda action: tool._invoke_company_work_os_owner_at(
            action=action,
            config=tool._PRODUCTION_COMPANY_WORK_OS_OWNER,
            runner=fake_runner,
        ),
    )
    result = tool.registered_local_workflow(
        action="company_work_os_initial_seed_record"
    )
    assert tuple(result) == (
        "schema",
        "decision",
        "prompt_count",
        "write_count",
        "action_id",
        "owner_result",
    )
    assert result["decision"] == "deny_owner_unavailable"
    assert result["write_count"] == 0
    assert result["owner_result"] is None
    assert "private" not in json.dumps(result)
    clear_session_vars(tokens)


def test_company_manifest_inventory_and_every_digest_are_rechecked_before_launch(
    monkeypatch, tmp_path
):
    import tools.registered_local_workflow as tool

    production = tool._PRODUCTION_COMPANY_WORK_OS_OWNER
    root = tmp_path / "owner"
    manifest = json.loads(production.manifest.read_text(encoding="utf-8"))
    for entry in manifest["python_files"]:
        source = production.project_root / entry["path"]
        destination = root / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(production.manifest, root / production.manifest.name)
    config = replace(
        production,
        project_root=root,
        owner_script=root / "scripts/run_registered_initial_seed.py",
        manifest=root / "registered-owner-manifest.json",
    )
    calls = []
    runner = lambda *_args, **_kwargs: calls.append(True) or pytest.fail(
        "owner launched after manifest failure"
    )

    rogue = root / "company_work_os/rogue.py"
    rogue.write_text("# unmanifested\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="registered company owner unavailable"):
        tool._invoke_company_work_os_owner_at(
            action="company_work_os_initial_seed_preview",
            config=config,
            runner=runner,
        )
    rogue.unlink()

    bound = root / manifest["python_files"][0]["path"]
    original = bound.read_bytes()
    bound.write_bytes(original + b"# digest drift\n")
    with pytest.raises(RuntimeError, match="registered company owner unavailable"):
        tool._invoke_company_work_os_owner_at(
            action="company_work_os_initial_seed_preview",
            config=config,
            runner=runner,
        )
    assert calls == []


def test_registry_direct_company_dispatch_repeats_feature_authority_and_dependency_gates(
    monkeypatch,
):
    import tools.registered_local_workflow as tool
    from tools.registry import registry

    calls = []
    monkeypatch.setattr(tool, "_feature_enabled", lambda: False)
    monkeypatch.setattr(
        tool,
        "_company_work_os_owner_action",
        lambda action: calls.append(action) or pytest.fail("owner called"),
    )
    result = json.loads(
        registry.dispatch(
            "registered_local_workflow",
            {"action": "company_work_os_initial_seed_preview"},
        )
    )
    assert result["decision"] == "deny_owner_unavailable"
    assert result["owner_result"] is None
    assert calls == []
