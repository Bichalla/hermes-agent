"""Service-gated registered local workflow tool tests."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


SOCIAL_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "social_owner_ops"
)
_EXTERNAL_SOCIAL_FIXTURE_ROOT = os.environ.get("HERMES_TEST_OPS_SOCIAL_ROOT")
SOCIAL_TARGET = "person_park_sanghyun:social-conversation"
SOCIAL_SOURCE = {
    "platform": "discord",
    "guild_id": "100000000000000001",
    "channel_id": "100000000000000002",
    "thread_id": "100000000000000003",
    "message_id": "100000000000000004",
}


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
        if operation in {"diet_intake_record", "social_conversation_record"}
        else "registered_soft_delete"
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


def _set_gateway_context(
    *, session_id: str = "session-1", thread_id: str | None = "thread-1"
):
    return set_session_vars(
        platform="discord",
        scope_id="guild-1",
        chat_id="channel-1",
        thread_id=thread_id or "",
        user_id="user-1",
        session_key="session-key-1",
        session_id=session_id,
        message_id="message-1",
    )


def _set_social_gateway_context(
    *,
    session_id: str = "session-1",
    scope_id: str = SOCIAL_SOURCE["guild_id"],
    chat_id: str = SOCIAL_SOURCE["channel_id"],
    thread_id: str = SOCIAL_SOURCE["thread_id"],
    message_id: str = SOCIAL_SOURCE["message_id"],
):
    return set_session_vars(
        platform="discord",
        scope_id=scope_id,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id="user-1",
        session_key="session-key-1",
        session_id=session_id,
        message_id=message_id,
    )


def _social_payload() -> dict:
    return {
        "schema_version": "social_conversation/v1",
        "intent": "confirmed_social_conversation",
        "occurred_at": "2026-07-24T10:00:00+09:00",
        "ended_at": "2026-07-24T10:30:00+09:00",
        "timezone": "Asia/Seoul",
        "person_id": "person_park_sanghyun",
        "partner_label": "SYNTHETIC_PARTNER",
        "confirmed_points": ["SYNTHETIC_POINT_1", "SYNTHETIC_POINT_2"],
        "tags": ["social", "conversation", "confirmed_event", "career"],
        "sources": [dict(SOCIAL_SOURCE)],
    }


def _write_social_payload(root: Path, document: dict, name: str = "social.json") -> Path:
    payload_root = root / ".runtime-inputs" / "social-conversation"
    payload_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload_root.chmod(0o700)
    payload = payload_root / name
    payload.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    payload.chmod(0o600)
    return payload


def _synthetic_social_lifelog(tmp_path: Path) -> Path:
    root = tmp_path / "state" / "lifelog"
    if _EXTERNAL_SOCIAL_FIXTURE_ROOT:
        external = Path(_EXTERNAL_SOCIAL_FIXTURE_ROOT).resolve(strict=True)
        shutil.copytree(external / "config", root / "config")
        shutil.copytree(external / "scripts", root / "scripts")
        subprocess.run(
            ["git", "init", "-q", str(tmp_path)],
            check=True,
            text=True,
            capture_output=True,
        )
        (tmp_path / ".gitignore").write_text(
            "state/lifelog/lifelog.db\nstate/lifelog/.runtime-backups/\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                str(external / "scripts" / "lifelog_migrate.py"),
                "--db",
                str(root / "lifelog.db"),
            ],
            cwd=external,
            check=True,
            text=True,
            capture_output=True,
        )
        _write_social_payload(root, _social_payload())
        return root

    shutil.copytree(SOCIAL_FIXTURE_ROOT, root)
    db = root / "lifelog.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE life_events(
                id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, ended_at TEXT,
                timezone TEXT NOT NULL, event_type TEXT NOT NULL, title TEXT NOT NULL,
                summary TEXT NOT NULL, source_type TEXT NOT NULL, source_ref TEXT NOT NULL,
                confidence REAL NOT NULL, raw_text_hash TEXT, payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE life_event_people(
                event_id TEXT NOT NULL REFERENCES life_events(id), person_id TEXT NOT NULL,
                role TEXT NOT NULL, confidence REAL NOT NULL,
                PRIMARY KEY(event_id, person_id, role)
            );
            CREATE TABLE life_event_tags(
                event_id TEXT NOT NULL REFERENCES life_events(id), tag TEXT NOT NULL,
                PRIMARY KEY(event_id, tag)
            );
            CREATE TABLE life_sources(
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES life_events(id),
                platform TEXT NOT NULL, guild_id TEXT, channel_id TEXT, thread_id TEXT,
                message_id TEXT, path TEXT, redacted_excerpt TEXT, captured_at TEXT NOT NULL
            );
            CREATE TABLE childcare_events(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE commute_events(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE condition_observations(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE event_relations(
                id TEXT PRIMARY KEY, source_event_id TEXT REFERENCES life_events(id),
                target_event_id TEXT REFERENCES life_events(id)
            );
            CREATE TABLE life_metrics(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE life_milestones(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE profile_facts(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE reading_action_experiments(
                id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id),
                reflection_event_id TEXT REFERENCES life_events(id)
            );
            CREATE TABLE reading_item_events(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE reading_note_events(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE reading_reflections(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE reading_sessions(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE sleep_blocks(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE training_parts(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE training_sessions(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE travel_artifacts(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE travel_event_places(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE travel_route_segments(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE travel_trip_events(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE value_grill_sessions(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            CREATE TABLE work_blocks(id TEXT PRIMARY KEY, event_id TEXT REFERENCES life_events(id));
            """
        )
    _write_social_payload(root, _social_payload())
    return root


def _bind_social_authority():
    bind_current_turn_user_authority(
        _authority(target=SOCIAL_TARGET, operation="social_conversation_record")
    )


def _prepare_social_action(monkeypatch, root: Path):
    import tools.registered_local_workflow as tool

    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    assert tool._pin_social_dependencies(root) is True
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    tokens = _set_social_gateway_context()
    _bind_social_authority()
    return tool, tokens


def test_schema_exposes_closed_pending_and_diet_actions_without_paths_or_payloads():
    from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA

    params = REGISTERED_LOCAL_WORKFLOW_SCHEMA["parameters"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {
        "action",
        "pending_id",
        "reason_code",
        "payload_name",
    }
    assert params["properties"]["action"]["enum"] == [
        "diet_intake_record",
        "pending_read",
        "pending_restore",
        "pending_soft_delete",
        "social_conversation_record",
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
        "payload_path",
    ):
        assert forbidden not in exposed_keys


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


def test_social_schema_adds_action_without_new_parameter():
    from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA

    parameters = REGISTERED_LOCAL_WORKFLOW_SCHEMA["parameters"]
    assert "social_conversation_record" in parameters["properties"]["action"]["enum"]
    assert tuple(parameters["properties"]) == (
        "action",
        "pending_id",
        "reason_code",
        "payload_name",
    )
    assert parameters["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutate_context,mutate_document",
    [
        (lambda: {}, lambda document: document.update(sources=[])),
        (
            lambda: {},
            lambda document: document["sources"].append(dict(SOCIAL_SOURCE)),
        ),
        (
            lambda: {"scope_id": "100000000000000009"},
            lambda _document: None,
        ),
        (
            lambda: {"chat_id": "100000000000000009"},
            lambda _document: None,
        ),
        (
            lambda: {"thread_id": "100000000000000009"},
            lambda _document: None,
        ),
        (
            lambda: {"message_id": "100000000000000009"},
            lambda _document: None,
        ),
    ],
    ids=("zero-source", "two-sources", "scope", "chat", "thread", "message"),
)
def test_social_payload_requires_exact_single_current_source_tuple(
    monkeypatch, tmp_path, mutate_context, mutate_document
):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    document = _social_payload()
    mutate_document(document)
    payload = _write_social_payload(root, document)
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    context = {
        "scope_id": SOCIAL_SOURCE["guild_id"],
        "chat_id": SOCIAL_SOURCE["channel_id"],
        "thread_id": SOCIAL_SOURCE["thread_id"],
        "message_id": SOCIAL_SOURCE["message_id"],
    }
    context.update(mutate_context())
    tokens = _set_social_gateway_context(**context)
    _bind_social_authority()
    monkeypatch.setattr(
        tool,
        "_social_owner_action",
        lambda **_kwargs: pytest.fail("owner called"),
        raising=False,
    )

    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert result["decision"] == "deny_target_mismatch"
    assert result["write_count"] == 0
    assert payload.exists()
    clear_session_vars(tokens)


def test_social_payload_fd_mode_owner_link_size_and_closed_schema(monkeypatch, tmp_path):
    import tools.registered_local_workflow as tool

    root = tmp_path / "lifelog"
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    tokens = _set_social_gateway_context()
    _bind_social_authority()
    monkeypatch.setattr(
        tool,
        "_social_owner_action",
        lambda **_kwargs: pytest.fail("owner called"),
        raising=False,
    )

    wrong_mode = _write_social_payload(root, _social_payload(), "wrong-mode.json")
    wrong_mode.chmod(0o640)
    hardlinked = _write_social_payload(root, _social_payload(), "hardlinked.json")
    os.link(hardlinked, hardlinked.with_name("hardlinked-copy.json"))
    oversized = _write_social_payload(root, _social_payload(), "oversized.json")
    oversized.write_bytes(b"{" + b" " * 16_384 + b"}")
    malformed = _write_social_payload(root, _social_payload(), "malformed.json")
    malformed.write_text("{", encoding="utf-8")
    extra = _social_payload()
    extra["private_extra"] = "SYNTHETIC_PRIVATE_SENTINEL"
    extra_field = _write_social_payload(root, extra, "extra.json")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_social_payload()), encoding="utf-8")
    symlink = root / ".runtime-inputs" / "social-conversation" / "symlink.json"
    symlink.symlink_to(outside)

    for candidate in (wrong_mode, hardlinked, oversized, malformed, extra_field, symlink):
        result = tool.registered_local_workflow(
            action="social_conversation_record", payload_name=candidate.name
        )
        assert result["decision"] == "deny_target_mismatch"
        assert result["write_count"] == 0
        assert candidate.exists() or candidate.is_symlink()

    wrong_root = root / ".runtime-inputs" / "social-conversation"
    wrong_root.chmod(0o750)
    root_mode_candidate = wrong_root / "root-mode.json"
    root_mode_candidate.write_text(
        json.dumps(_social_payload(), ensure_ascii=False), encoding="utf-8"
    )
    root_mode_candidate.chmod(0o600)
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=root_mode_candidate.name
    )
    assert result["decision"] == "deny_target_mismatch"
    assert root_mode_candidate.exists()
    wrong_root.chmod(0o700)

    owned = _write_social_payload(root, _social_payload(), "wrong-owner.json")
    monkeypatch.setattr(tool.os, "getuid", lambda: os.stat(owned).st_uid + 1)
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=owned.name
    )
    assert result["decision"] == "deny_target_mismatch"
    assert owned.exists()
    clear_session_vars(tokens)


def test_social_owner_revalidates_authority_at_pre_live_seam(monkeypatch, tmp_path):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    calls = []

    def run_document(_document_bytes, pre_live_check, _root):
        calls.append("dry-run-finished")
        clear_current_turn_user_authority()
        pre_live_check()
        pytest.fail("live write reached")

    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: SimpleNamespace(run_registered_social_document=run_document),
        raising=False,
    )
    payload = root / ".runtime-inputs" / "social-conversation" / "social.json"

    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert calls == ["dry-run-finished"]
    assert result["decision"] == "deny_owner_unavailable"
    assert set(result) == {"schema", "decision", "prompt_count", "write_count", "action_id"}
    assert not payload.exists()
    with sqlite3.connect(root / "lifelog.db") as connection:
        assert connection.execute("SELECT count(*) FROM life_events").fetchone()[0] == 0
    clear_session_vars(tokens)


def test_social_owner_rejects_ops_source_changed_after_pin(monkeypatch, tmp_path):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    dispatcher_path = root / "scripts" / "run_registered_recorder.py"
    dispatcher_path.write_bytes(
        dispatcher_path.read_bytes() + b"\n# changed after dependency pin\n"
    )
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name="social.json"
    )
    assert result["decision"] == "deny_owner_unavailable"
    assert not (
        root / ".runtime-inputs" / "social-conversation" / "social.json"
    ).exists()
    with sqlite3.connect(root / "lifelog.db") as connection:
        assert connection.execute("SELECT count(*) FROM life_events").fetchone()[0] == 0
    clear_session_vars(tokens)


def test_social_owner_reads_once_and_ignores_path_replacement(monkeypatch, tmp_path):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    payload = root / ".runtime-inputs" / "social-conversation" / "social.json"
    real_dispatcher = tool._load_social_dispatcher(root)
    replacement = _social_payload()
    replacement["confirmed_points"] = ["SYNTHETIC_REPLACEMENT_MUST_NOT_WIN"]

    def run_document(document_bytes, pre_live_check, owner_root):
        payload.write_text(json.dumps(replacement), encoding="utf-8")
        payload.chmod(0o600)
        return real_dispatcher.run_registered_social_document(
            document_bytes, pre_live_check, owner_root
        )

    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: SimpleNamespace(run_registered_social_document=run_document),
    )
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert result["decision"] == "allow"
    assert payload.exists()
    with sqlite3.connect(root / "lifelog.db") as connection:
        summary = connection.execute(
            "SELECT summary FROM life_events WHERE id=?", (result["event_ids"][0],)
        ).fetchone()[0]
    assert "SYNTHETIC_POINT_1" in summary
    assert "SYNTHETIC_REPLACEMENT_MUST_NOT_WIN" not in summary
    clear_session_vars(tokens)


def test_social_owner_rejects_coherent_alternate_result_and_db(monkeypatch, tmp_path):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    real_dispatcher = tool._load_social_dispatcher(root)
    alternate = _social_payload()
    alternate["confirmed_points"] = ["SYNTHETIC_COHERENT_ALTERNATE"]
    alternate_bytes = json.dumps(alternate, ensure_ascii=False).encode("utf-8")

    def run_alternate(_document_bytes, pre_live_check, owner_root):
        return real_dispatcher.run_registered_social_document(
            alternate_bytes, pre_live_check, owner_root
        )

    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: SimpleNamespace(run_registered_social_document=run_alternate),
    )
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name="social.json"
    )

    assert result["decision"] == "deny_owner_unavailable"
    assert set(result) == {"schema", "decision", "prompt_count", "write_count", "action_id"}
    clear_session_vars(tokens)


def test_social_owner_exact_result_and_constant_safe_output(monkeypatch, tmp_path):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name="social.json"
    )

    assert set(result) == {
        "schema",
        "decision",
        "prompt_count",
        "write_count",
        "action_id",
        "idempotency_result",
        "validation_status",
        "event_ids",
        "payload_hash",
        "backup_status",
        "replay_status",
        "payload_cleanup",
        "readback",
    }
    assert result["decision"] == "allow"
    assert result["prompt_count"] == 0
    assert type(result["write_count"]) is int and result["write_count"] == 1
    assert result["idempotency_result"] == "inserted"
    assert result["validation_status"] == "validator_and_exact_readback_passed"
    assert result["backup_status"] == "verified"
    assert result["replay_status"] == "verified"
    assert result["payload_cleanup"] == "deleted"
    assert result["readback"] == "passed"
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for private in (
        "SYNTHETIC_PARTNER",
        "SYNTHETIC_POINT_1",
        SOCIAL_SOURCE["guild_id"],
        SOCIAL_SOURCE["channel_id"],
        str(root),
        "run_registered_recorder.py",
    ):
        assert private not in rendered
    clear_session_vars(tokens)


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_social_owner_unlinks_valid_bound_payload_on_success_failure_timeout(
    monkeypatch, tmp_path, outcome
):
    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    payload = root / ".runtime-inputs" / "social-conversation" / "social.json"
    real_dispatcher = tool._load_social_dispatcher(root)

    if outcome == "success":
        owner = real_dispatcher.run_registered_social_document
    elif outcome == "failure":
        def owner(_document_bytes, _pre_live_check, _root):
            raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE")
    else:
        def owner(_document_bytes, _pre_live_check, _root):
            raise subprocess.TimeoutExpired("SYNTHETIC_PRIVATE_ARGV", 1)

    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: SimpleNamespace(run_registered_social_document=owner),
    )
    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert not payload.exists()
    if outcome == "success":
        assert result["decision"] == "allow"
    else:
        assert result["decision"] == "deny_owner_unavailable"
        assert set(result) == {"schema", "decision", "prompt_count", "write_count", "action_id"}
        rendered = json.dumps(result, sort_keys=True)
        assert "SYNTHETIC_PRIVATE" not in rendered
    clear_session_vars(tokens)


def test_social_policy_denial_cleans_valid_bound_payload_without_ops_write(
    monkeypatch, tmp_path
):
    import tools.registered_local_workflow as tool

    root = _synthetic_social_lifelog(tmp_path)
    tool, tokens = _prepare_social_action(monkeypatch, root)
    monkeypatch.setattr(tool, "_owner_ready", lambda _action: False)
    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: pytest.fail("ops dispatcher loaded"),
    )
    payload = root / ".runtime-inputs" / "social-conversation" / "social.json"

    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert result["decision"] == "deny_owner_unavailable"
    assert not payload.exists()
    with sqlite3.connect(root / "lifelog.db") as connection:
        assert connection.execute("SELECT count(*) FROM life_events").fetchone()[0] == 0
    clear_session_vars(tokens)


def test_social_authority_denial_cleans_valid_bound_payload_without_ops_write(
    monkeypatch, tmp_path
):
    import tools.registered_local_workflow as tool

    root = _synthetic_social_lifelog(tmp_path)
    monkeypatch.setattr(tool, "_LIFELOG_ROOT", root)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda _action: True)
    monkeypatch.setattr(
        tool,
        "_load_social_dispatcher",
        lambda _root: pytest.fail("ops dispatcher loaded"),
        raising=False,
    )
    tokens = _set_social_gateway_context(session_id="different-session")
    _bind_social_authority()
    payload = root / ".runtime-inputs" / "social-conversation" / "social.json"

    result = tool.registered_local_workflow(
        action="social_conversation_record", payload_name=payload.name
    )

    assert result["decision"] == "deny_authority_missing"
    assert not payload.exists()
    with sqlite3.connect(root / "lifelog.db") as connection:
        assert connection.execute("SELECT count(*) FROM life_events").fetchone()[0] == 0
    clear_session_vars(tokens)
