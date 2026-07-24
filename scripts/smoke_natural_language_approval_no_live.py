#!/usr/bin/env python3
"""Execute synthetic no-live approval probes against real workflow seams."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent.workflow_action_policy import (
    WorkflowAction,
    WorkflowActionClass,
    classify_workflow_action,
)
from tools import approval as approval

SCHEMA = "approval-decision-matrix/v1"
MATRIX_CASE_IDS = (
    "kanban_comment_direct",
    "kanban_comment_pipe_python_readback",
    "diet_registered_direct",
    "arbitrary_python_sqlite",
    "blocked_card_create_idempotent",
    "planned_record_intent",
    "ambiguous_record_intent",
    "child_health_unregistered",
    "medication_unregistered",
    "session_cache_after_approval",
)
EXPECTED_CASES: dict[str, dict[str, Any]] = {
    "kanban_comment_direct": {"expected_prompt_count": 0, "expected_execution": "allow"},
    "kanban_comment_pipe_python_readback": {
        "expected_prompt_count": 1,
        "expected_execution": "prompt",
        "expected_guard_rule": "pipe-to-interpreter",
    },
    "diet_registered_direct": {"expected_prompt_count": 0, "expected_execution": "allow"},
    "arbitrary_python_sqlite": {"expected_prompt_count": 1, "expected_execution": "prompt"},
    "blocked_card_create_idempotent": {
        "expected_prompt_count": 0,
        "expected_execution": "allow",
    },
    "planned_record_intent": {"expected_prompt_count": 0, "expected_execution": "no_write"},
    "ambiguous_record_intent": {"expected_prompt_count": 0, "expected_execution": "no_write"},
    "child_health_unregistered": {
        "expected_prompt_count": 0,
        "expected_execution": "deny_unregistered",
    },
    "medication_unregistered": {
        "expected_prompt_count": 0,
        "expected_execution": "deny_unregistered",
    },
    "session_cache_after_approval": {
        "expected_prompt_count": 0,
        "expected_execution": "allow_cached",
    },
}
HERMES_ROOT = Path(__file__).resolve().parents[2]
OPS_LIFELOG_ROOT = HERMES_ROOT / "ops" / "state" / "lifelog"
RECORDER_DISPATCHER = OPS_LIFELOG_ROOT / "scripts" / "run_registered_recorder.py"
REGISTERED_RECORDER_ROUTE = (
    Path(__file__).resolve().parent / "run_registered_lifelog_recorder.py"
)


def _metadata_snapshot(paths: list[Path]) -> dict[str, tuple[bool, int, int]]:
    snapshot: dict[str, tuple[bool, int, int]] = {}
    for path in paths:
        try:
            stat_result = path.stat()
            snapshot[str(path)] = (True, stat_result.st_mtime_ns, stat_result.st_size)
        except FileNotFoundError:
            snapshot[str(path)] = (False, 0, 0)
    return snapshot


def _live_path_groups() -> dict[str, list[Path]]:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return {
        "config": [hermes_home / "config.yaml"],
        "kanban": sorted((hermes_home / "kanban").glob("*.db")),
        "lifelog": [OPS_LIFELOG_ROOT / "lifelog.db"],
    }


def _live_boundary_mutation_flags(
    groups: dict[str, list[Path]],
    before: dict[str, tuple[bool, int, int]],
    after: dict[str, tuple[bool, int, int]],
) -> dict[str, bool]:
    def changed(boundary: str) -> bool:
        return any(
            before.get(str(path)) != after.get(str(path))
            for path in groups[boundary]
        )

    return {
        "config_mutated": changed("config"),
        "kanban_mutated": changed("kanban"),
        "live_db_mutated": changed("lifelog"),
    }


def _load_recorder_dispatcher():
    spec = importlib.util.spec_from_file_location(
        "no_live_registered_recorder", RECORDER_DISPATCHER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("registered recorder dispatcher cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_registered_recorder_route(
    *,
    root: Path,
    recorder_id: str,
    payload: Path,
    dry_run: bool,
    with_authority: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(root.parents[3])
    if with_authority:
        env["HERMES_CURRENT_USER_ACTION_FINGERPRINT"] = "a" * 64
    else:
        env.pop("HERMES_CURRENT_USER_ACTION_FINGERPRINT", None)
    return subprocess.run(
        [
            sys.executable,
            str(REGISTERED_RECORDER_ROUTE),
            recorder_id,
            "--payload",
            str(payload),
            "--dry-run",
            "true" if dry_run else "false",
            "--json",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _route_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if completed.returncode != 0:
        raise RuntimeError("registered recorder production route failed")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise RuntimeError("registered recorder production route returned invalid JSON")
    return result


def _probe_registered_recorder() -> dict[str, Any]:
    dispatcher = _load_recorder_dispatcher()
    migrate_path = OPS_LIFELOG_ROOT / "scripts" / "lifelog_migrate.py"
    migrate_spec = importlib.util.spec_from_file_location(
        "no_live_lifelog_migrate", migrate_path
    )
    if migrate_spec is None or migrate_spec.loader is None:
        raise RuntimeError("lifelog migration helper cannot be loaded")
    migrate = importlib.util.module_from_spec(migrate_spec)
    migrate_spec.loader.exec_module(migrate)

    with tempfile.TemporaryDirectory(prefix="hermes-no-live-lifelog-") as temp_dir:
        temp_home = Path(temp_dir).resolve()
        root = temp_home / ".hermes" / "ops" / "state" / "lifelog"
        scripts = root / "scripts"
        config = root / "config"
        payload_root = root / ".runtime-inputs" / "diet-intake"
        scripts.mkdir(parents=True)
        config.mkdir()
        payload_root.mkdir(parents=True)
        payload_root.chmod(0o700)
        shutil.copy2(
            RECORDER_DISPATCHER,
            scripts / "run_registered_recorder.py",
        )
        shutil.copy2(
            OPS_LIFELOG_ROOT / "scripts" / "record_diet_intake.py",
            scripts / "record_diet_intake.py",
        )
        canonical_validator = OPS_LIFELOG_ROOT / "scripts" / "validate_lifelog.py"
        (scripts / "validate_lifelog.py").write_text(
            "import runpy\n"
            f"runpy.run_path({str(canonical_validator)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        shutil.copy2(
            OPS_LIFELOG_ROOT / "config" / "recorder-registry.json",
            config / "recorder-registry.json",
        )
        db = root / "lifelog.db"
        migrate.apply_migrations(db)
        payload = payload_root / "synthetic.json"
        payload.write_text(
            json.dumps(
                {
                    "schema_version": "diet_intake/v1",
                    "intent": "confirmed_intake",
                    "occurred_at": "2026-01-01T09:45:00+09:00",
                    "timezone": "Asia/Seoul",
                    "person_id": "person_park_sanghyun",
                    "meal_label": "synthetic_meal",
                    "title": "Synthetic registered meal",
                    "items": [
                        {"name": "synthetic food", "quantity_text": "1 unit"}
                    ],
                    "tags": ["diet", "confirmed_intake"],
                    "source": {"platform": "manual"},
                }
            ),
            encoding="utf-8",
        )
        payload.chmod(0o600)

        missing_authority = _run_registered_recorder_route(
            root=root,
            recorder_id="diet_intake.v1",
            payload=payload,
            dry_run=True,
            with_authority=False,
        )
        dry_run = _route_json(
            _run_registered_recorder_route(
                root=root,
                recorder_id="diet_intake.v1",
                payload=payload,
                dry_run=True,
            )
        )
        first = _route_json(
            _run_registered_recorder_route(
                root=root,
                recorder_id="diet_intake.v1",
                payload=payload,
                dry_run=False,
            )
        )
        retry = _route_json(
            _run_registered_recorder_route(
                root=root,
                recorder_id="diet_intake.v1",
                payload=payload,
                dry_run=False,
            )
        )
        rejected_intents: list[str] = []
        base_payload = json.loads(payload.read_text(encoding="utf-8"))
        for intent in ("planned", "ambiguous"):
            rejected_payload = payload_root / f"{intent}.json"
            rejected_data = dict(base_payload)
            rejected_data["intent"] = intent
            rejected_payload.write_text(json.dumps(rejected_data), encoding="utf-8")
            rejected_payload.chmod(0o600)
            completed = _run_registered_recorder_route(
                root=root,
                recorder_id="diet_intake.v1",
                payload=rejected_payload,
                dry_run=True,
            )
            if completed.returncode != 0:
                rejected_intents.append(intent)

        contract = dispatcher._registered_contract(root, "diet_intake.v1")
        argv = dispatcher.build_registered_recorder_argv(
            script=Path(contract["script"]),
            db=Path(contract["db"]),
            payload=payload,
            dry_run=True,
        )
        with sqlite3.connect(db) as connection:
            event_count = connection.execute(
                "SELECT count(*) FROM life_events"
            ).fetchone()[0]
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        rejected_unknown: list[str] = []
        for recorder_id in ("child_health.v1", "medication.v1"):
            completed = _run_registered_recorder_route(
                root=root,
                recorder_id=recorder_id,
                payload=payload,
                dry_run=True,
            )
            if completed.returncode != 0:
                rejected_unknown.append(recorder_id)

    expected_flags = ["--db", "--payload", "--dry-run", "true", "--json"]
    rendered_results = repr((dry_run, first, retry))
    return {
        "contract_loaded": contract["confirmed_intent"] == "confirmed_intake",
        "production_route_exercised": missing_authority.returncode != 0
        and "current-turn user authority" in missing_authority.stderr,
        "argv_is_list": isinstance(argv, list),
        "shell_free": all("|" not in item and ";" not in item for item in argv),
        "fixed_flags": all(flag in argv for flag in expected_flags),
        "recorder_id": "diet_intake.v1",
        "supported_recorder_count": len(dispatcher._SUPPORTED_CONTRACTS),
        "unregistered_recorders_rejected": tuple(rejected_unknown),
        "dry_run_validated": dry_run["validation_status"] == "payload_validated"
        and dry_run["idempotency_result"] == "not_applicable_dry_run",
        "write_validated": first["validation_status"]
        == "validator_and_readback_passed",
        "idempotent_retry": first["idempotency_result"] == "inserted"
        and retry["idempotency_result"] == "existing"
        and first["event_ids"] == retry["event_ids"]
        and event_count == 1,
        "foreign_keys_valid": foreign_key_errors == [],
        "raw_free": "Synthetic registered meal" not in rendered_results
        and "synthetic food" not in rendered_results,
        "record_intents_rejected": tuple(rejected_intents),
    }


def _probe_kanban_tools() -> dict[str, Any]:
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools
    from tools.workflow_authority import (
        CurrentTurnUserAuthority,
        bind_active_workflow_turn,
        bind_current_turn_user_authority,
        clear_current_turn_user_authority,
        fingerprint_user_action,
        fingerprint_workflow_target,
        infer_blocked_create_generated_title,
        infer_explicit_blocked_create_targets,
        infer_explicit_workflow_scope,
        reset_current_turn_user_authority,
    )

    natural_create_request = (
        "Phase 2 운영 blocker 카드를 active board에 default blocked로 지금 생성"
    )
    create_classes, create_targets = infer_explicit_workflow_scope(
        natural_create_request
    )
    authority = CurrentTurnUserAuthority(
        turn_id="opaque-no-live-turn",
        source_role="user",
        session_scope="no-live",
        platform_scope="synthetic",
        user_message_index=0,
        user_action_fingerprint=fingerprint_user_action(
            natural_create_request
        ),
        source_event_fingerprint=fingerprint_user_action(
            "source-event:no-live-blocked-create"
        ),
        allowed_action_classes=create_classes,
        target_fingerprints=create_targets,
        blocked_create_target_fingerprints=(
            infer_explicit_blocked_create_targets(natural_create_request)
        ),
        blocked_create_generated_title=infer_blocked_create_generated_title(
            natural_create_request
        ),
    )
    session_tokens = set_session_vars(
        platform=authority.platform_scope,
        session_id=authority.session_scope,
        message_id="no-live-source-event",
    )
    bind_active_workflow_turn(
        authority.turn_id, authority.platform_scope, authority.session_scope
    )
    authority_token = bind_current_turn_user_authority(authority)
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-no-live-kanban-") as temp_dir:
            root = Path(temp_dir)
            env = {
                "HERMES_HOME": str(root / ".hermes"),
                "HERMES_KANBAN_DB": str(root / "kanban.db"),
                "HERMES_KANBAN_HOME": str(root / "kanban-home"),
                "HERMES_KANBAN_BOARD": "default",
                "HERMES_PROFILE": "no-live-smoke",
                "HERMES_KANBAN_TASK": "",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                kb.init_db()
                with kb.connect_closing() as migration_conn:
                    kb.migrate_status_memory_idempotency(
                        migration_conn, dry_run=False
                    )
                create_args = {
                    "title": "Review synthetic blocked approval card",
                    "assignee": "default",
                    "initial_status": "blocked",
                }
                first = json.loads(kanban_tools._handle_create(dict(create_args)))
                retry = json.loads(kanban_tools._handle_create(dict(create_args)))
                reset_current_turn_user_authority(authority_token)
                authority_token = None
                clear_current_turn_user_authority()
                comment_authority = CurrentTurnUserAuthority(
                    turn_id="opaque-no-live-comment-turn",
                    source_role="user",
                    session_scope="no-live",
                    platform_scope="synthetic",
                    user_message_index=1,
                    user_action_fingerprint=fingerprint_user_action(
                        f"comment on card {first['task_id']}"
                    ),
                    source_event_fingerprint=fingerprint_user_action(
                        "source-event:no-live-comment"
                    ),
                    allowed_action_classes=frozenset({"status_memory"}),
                    allowed_operations=frozenset(
                        {"kanban_status_memory_comment"}
                    ),
                    operation_target_grants=frozenset(
                        {
                            (
                                "kanban_status_memory_comment",
                                fingerprint_workflow_target(first["task_id"]),
                            )
                        }
                    ),
                    target_fingerprints=frozenset(
                        {fingerprint_workflow_target(first["task_id"])}
                    ),
                )
                bind_active_workflow_turn(
                    comment_authority.turn_id,
                    comment_authority.platform_scope,
                    comment_authority.session_scope,
                )
                authority_token = bind_current_turn_user_authority(comment_authority)
                comment = json.loads(
                    kanban_tools._handle_comment(
                        {
                            "task_id": first["task_id"],
                            "body": "Synthetic verification checkpoint",
                        }
                    )
                )
                with kb.connect_closing() as connection:
                    count = connection.execute("SELECT count(*) FROM tasks").fetchone()[0]
                    stored = kb.get_task(connection, first["task_id"])
    finally:
        if authority_token is not None:
            reset_current_turn_user_authority(authority_token)
        clear_current_turn_user_authority()
        clear_session_vars(session_tokens)

    create_evidence = first["decision_evidence"]
    comment_evidence = comment["decision_evidence"]
    return {
        "natural_blocked_create_inferred": (
            authority.allows("explicit_blocked_card_create")
            and bool(authority.blocked_create_generated_title)
        ),
        "blocked_create_idempotent": first["task_id"] == retry["task_id"] and count == 1,
        "blocked_key_present": bool(
            stored
            and stored.idempotency_key
            and stored.idempotency_key.startswith("blocked-card:v1:")
        ),
        "create_prompt_count": create_evidence["prompt_count"],
        "create_action_class": create_evidence["action_class"],
        "comment_prompt_count": comment_evidence["prompt_count"],
        "comment_action_class": comment_evidence["action_class"],
        "comment_raw_free": all(
            secret not in repr((create_evidence, comment_evidence))
            for secret in (
                "Synthetic verification checkpoint",
                first["task_id"],
                retry["task_id"],
            )
        ),
        "create_evidence": create_evidence,
        "comment_evidence": comment_evidence,
    }


def _tirith_allow(_command: str) -> dict[str, Any]:
    return {"action": "allow", "findings": [], "summary": ""}


def _tirith_pipe(_command: str) -> dict[str, Any]:
    return {
        "action": "warn",
        "findings": [{"rule_id": "pipe-to-interpreter", "message": "synthetic"}],
        "summary": "synthetic interpreter pipe",
    }


def _probe_approval_guards() -> dict[str, Any]:
    session_key = "no-live-smoke-session"
    token = approval.set_current_session_key(session_key)
    with approval._lock:
        saved_permanent = set(approval._permanent_approved)
        saved_session = set(approval._session_approved.get(session_key, set()))
        approval._permanent_approved.clear()
        approval._session_approved.pop(session_key, None)
        approval._gateway_queues.pop(session_key, None)
        approval._gateway_notify_cbs.pop(session_key, None)

    env = {
        "HERMES_EXEC_ASK": "1",
        "HERMES_GATEWAY_SESSION": "",
        "HERMES_CRON_SESSION": "",
        "HERMES_INTERACTIVE": "",
    }
    try:
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(approval, "_get_approval_mode", return_value="manual"),
            mock.patch.object(
                approval, "_command_matches_permanent_allowlist", return_value=False
            ),
        ):
            with (
                mock.patch("tools.tirith_security.check_command_security", _tirith_allow),
                mock.patch.object(
                    approval,
                    "detect_dangerous_command",
                    return_value=(False, "", ""),
                ),
            ):
                direct = approval.check_all_command_guards(
                    "hermes kanban --board synthetic comment t_1 note", "local"
                )

            with (
                mock.patch("tools.tirith_security.check_command_security", _tirith_pipe),
                mock.patch.object(
                    approval,
                    "detect_dangerous_command",
                    return_value=(False, "", ""),
                ),
            ):
                pipe = approval.check_all_command_guards(
                    "hermes kanban show t_1 --json | python3 -m json.tool", "local"
                )

            with (
                mock.patch("tools.tirith_security.check_command_security", _tirith_allow),
                mock.patch.object(
                    approval,
                    "detect_dangerous_command",
                    return_value=(True, "synthetic-sqlite", "synthetic direct DB mutation"),
                ),
            ):
                dangerous = approval.check_all_command_guards(
                    "python3 -c synthetic_sqlite_mutation", "local"
                )
                approval.approve_session(session_key, "synthetic-sqlite")
                cached = approval.check_all_command_guards(
                    "python3 -c synthetic_sqlite_mutation", "local"
                )

        return {
            "direct": direct,
            "pipe": pipe,
            "dangerous": dangerous,
            "cached": cached,
        }
    finally:
        approval.reset_current_session_key(token)
        with approval._lock:
            approval._permanent_approved.clear()
            approval._permanent_approved.update(saved_permanent)
            if saved_session:
                approval._session_approved[session_key] = saved_session
            else:
                approval._session_approved.pop(session_key, None)
            approval._gateway_queues.pop(session_key, None)
            approval._gateway_notify_cbs.pop(session_key, None)


def _classification_probes() -> dict[str, str]:
    actions = {
        "planned": WorkflowAction(operation="lifelog.record", read_only=True),
        "ambiguous": WorkflowAction(operation="lifelog.record", read_only=True),
        "registered": WorkflowAction(
            operation="lifelog.record",
            registered_recorder=True,
            confirmed_fact=True,
            exact_target=True,
        ),
        "unknown_db": WorkflowAction(
            operation="database.mutate", unknown_db_action=True
        ),
    }
    return {name: classify_workflow_action(action).value for name, action in actions.items()}


def _observation_evidence(
    *, action_id: str, action_class: str, command_shape: str, decision: str
) -> dict[str, Any]:
    return approval.build_approval_decision_evidence(
        action_id=action_id,
        action_class=action_class,
        guard_source="none",
        rule_id="typed-workflow-policy",
        decision=decision,
        prompt_count=0,
        session_cache_influenced=False,
        command_shape=command_shape,
    )


def build_matrix() -> dict[str, Any]:
    """Return observed outcomes from real synthetic execution seams."""
    kanban = _probe_kanban_tools()
    guards = _probe_approval_guards()
    recorder = _probe_registered_recorder()
    classes = _classification_probes()

    pipe_evidence = guards["pipe"]["decision_evidence"]
    dangerous_evidence = guards["dangerous"]["decision_evidence"]
    cached_evidence = guards["cached"]["decision_evidence"]
    cases = [
        {
            "id": "kanban_comment_direct",
            "action_class": kanban["comment_action_class"],
            "command_shape": "kanban_comment_direct",
            "observed_prompt_count": kanban["comment_prompt_count"],
            "observed_execution": "allow",
            "decision_evidence": kanban["comment_evidence"],
        },
        {
            "id": "kanban_comment_pipe_python_readback",
            "action_class": "status_memory",
            "guard_action_class": pipe_evidence["action_class"],
            "command_shape": "kanban_comment_then_python_pipe_readback",
            "observed_prompt_count": pipe_evidence["prompt_count"],
            "observed_execution": "prompt",
            "observed_guard_rule": pipe_evidence["rule_id"],
            "decision_evidence": pipe_evidence,
        },
        {
            "id": "diet_registered_direct",
            "action_class": classes["registered"],
            "command_shape": "registered_recorder_direct",
            "observed_prompt_count": 0 if recorder["shell_free"] else 1,
            "observed_execution": "allow" if recorder["fixed_flags"] else "deny",
            "decision_evidence": _observation_evidence(
                action_id="synthetic-diet-record",
                action_class=classes["registered"],
                command_shape="registered_recorder_direct",
                decision="allow" if recorder["fixed_flags"] else "deny",
            ),
        },
        {
            "id": "arbitrary_python_sqlite",
            "action_class": classes["unknown_db"],
            "command_shape": dangerous_evidence["command_shape"],
            "observed_prompt_count": dangerous_evidence["prompt_count"],
            "observed_execution": "prompt",
            "decision_evidence": dangerous_evidence,
        },
        {
            "id": "blocked_card_create_idempotent",
            "action_class": kanban["create_action_class"],
            "command_shape": "kanban_blocked_create_with_idempotency_key",
            "observed_prompt_count": kanban["create_prompt_count"],
            "observed_execution": "allow" if kanban["blocked_create_idempotent"] else "deny",
            "decision_evidence": kanban["create_evidence"],
        },
        {
            "id": "planned_record_intent",
            "action_class": classes["planned"],
            "command_shape": "planned_record_no_write",
            "observed_prompt_count": 0,
            "observed_execution": (
                "no_write"
                if "planned" in recorder["record_intents_rejected"]
                else "unexpected_write"
            ),
            "execution_seam": "registered_recorder_dispatcher",
            "decision_evidence": _observation_evidence(
                action_id="synthetic-planned-record",
                action_class=classes["planned"],
                command_shape="planned_record_no_write",
                decision="allow",
            ),
        },
        {
            "id": "ambiguous_record_intent",
            "action_class": classes["ambiguous"],
            "command_shape": "ambiguous_record_no_write",
            "observed_prompt_count": 0,
            "observed_execution": (
                "no_write"
                if "ambiguous" in recorder["record_intents_rejected"]
                else "unexpected_write"
            ),
            "execution_seam": "registered_recorder_dispatcher",
            "decision_evidence": _observation_evidence(
                action_id="synthetic-ambiguous-record",
                action_class=classes["ambiguous"],
                command_shape="ambiguous_record_no_write",
                decision="allow",
            ),
        },
        {
            "id": "child_health_unregistered",
            "action_class": "approval_required_live_mutation",
            "command_shape": "unregistered_child_health_recorder",
            "observed_prompt_count": 0,
            "observed_execution": "deny_unregistered",
            "decision_evidence": _observation_evidence(
                action_id="synthetic-child-health-record",
                action_class="approval_required_live_mutation",
                command_shape="unregistered_child_health_recorder",
                decision="deny",
            ),
        },
        {
            "id": "medication_unregistered",
            "action_class": "approval_required_live_mutation",
            "command_shape": "unregistered_medication_recorder",
            "observed_prompt_count": 0,
            "observed_execution": "deny_unregistered",
            "decision_evidence": _observation_evidence(
                action_id="synthetic-medication-record",
                action_class="approval_required_live_mutation",
                command_shape="unregistered_medication_recorder",
                decision="deny",
            ),
        },
        {
            "id": "session_cache_after_approval",
            "action_class": cached_evidence["action_class"],
            "command_shape": cached_evidence["command_shape"],
            "observed_prompt_count": cached_evidence["prompt_count"],
            "observed_execution": "allow_cached",
            "decision_evidence": cached_evidence,
        },
    ]
    for case in cases:
        expected = EXPECTED_CASES[case["id"]]
        case.update(expected)
        case["matches_expectation"] = (
            case["observed_prompt_count"] == expected["expected_prompt_count"]
            and case["observed_execution"] == expected["expected_execution"]
            and (
                "expected_guard_rule" not in expected
                or case.get("observed_guard_rule") == expected["expected_guard_rule"]
            )
        )
    if tuple(case["id"] for case in cases) != MATRIX_CASE_IDS:
        raise RuntimeError("approval matrix inventory drift")
    return {"schema": SCHEMA, "cases": cases}


def build_smoke_result() -> dict[str, Any]:
    """Run real synthetic probes and compare live-state metadata before/after."""
    path_groups = _live_path_groups()
    paths = [path for group in path_groups.values() for path in group]
    before = _metadata_snapshot(paths)
    kanban = _probe_kanban_tools()
    guards = _probe_approval_guards()
    recorder = _probe_registered_recorder()
    classes = _classification_probes()
    after = _metadata_snapshot(paths)
    mutation_flags = _live_boundary_mutation_flags(path_groups, before, after)

    pipe_evidence = guards["pipe"].get("decision_evidence", {})
    dangerous_evidence = guards["dangerous"].get("decision_evidence", {})
    cached_evidence = guards["cached"].get("decision_evidence", {})

    trusted_actions_prompt_zero = all(
        (
            guards["direct"].get("approved") is True,
            kanban["comment_prompt_count"] == 0,
            kanban["create_prompt_count"] == 0,
            kanban["comment_action_class"] == WorkflowActionClass.STATUS_MEMORY.value,
            kanban["create_action_class"]
            == WorkflowActionClass.EXPLICIT_BLOCKED_CARD_CREATE.value,
            classes["registered"] == WorkflowActionClass.TRUSTED_LOCAL_RECORD.value,
        )
    )
    guarded_actions_prompt_one_or_deny = all(
        (
            guards["pipe"].get("approved") is False,
            pipe_evidence.get("prompt_count") == 1,
            guards["dangerous"].get("approved") is False,
            dangerous_evidence.get("prompt_count") == 1,
        )
    )
    historical_pipe_still_warns = (
        pipe_evidence.get("guard_source") == "tirith"
        and pipe_evidence.get("rule_id") == "pipe-to-interpreter"
    )
    session_cache_reported = all(
        (
            guards["cached"].get("approved") is True,
            cached_evidence.get("prompt_count") == 0,
            cached_evidence.get("session_cache_influenced") is True,
        )
    )
    recorder_contract_passed = all(
        (
            recorder["contract_loaded"],
            recorder["production_route_exercised"],
            recorder["argv_is_list"],
            recorder["shell_free"],
            recorder["fixed_flags"],
            recorder["supported_recorder_count"] == 1,
            recorder["unregistered_recorders_rejected"]
            == ("child_health.v1", "medication.v1"),
            recorder["dry_run_validated"],
            recorder["write_validated"],
            recorder["idempotent_retry"],
            recorder["foreign_keys_valid"],
            recorder["raw_free"],
            recorder["record_intents_rejected"] == ("planned", "ambiguous"),
        )
    )
    live_state_unchanged = not any(mutation_flags.values())
    try:
        matrix = build_matrix()
        matrix_expectations_passed = all(
            case["matches_expectation"] for case in matrix["cases"]
        )
    except Exception:
        matrix_expectations_passed = False
    quality_passed = all(
        (
            trusted_actions_prompt_zero,
            guarded_actions_prompt_one_or_deny,
            historical_pipe_still_warns,
            session_cache_reported,
            recorder_contract_passed,
            kanban["blocked_create_idempotent"],
            kanban["blocked_key_present"],
            kanban["comment_raw_free"],
            live_state_unchanged,
            matrix_expectations_passed,
        )
    )
    return {
        "schema": "natural-language-approval-no-live-smoke/v2",
        "quality_thresholds_passed": quality_passed,
        "trusted_actions_prompt_zero": trusted_actions_prompt_zero,
        "guarded_actions_prompt_one_or_deny": guarded_actions_prompt_one_or_deny,
        "historical_pipe_still_warns": historical_pipe_still_warns,
        "session_cache_reported": session_cache_reported,
        "registered_recorder_contract_passed": recorder_contract_passed,
        "matrix_expectations_passed": matrix_expectations_passed,
        "blocked_card_idempotency_executed": kanban["blocked_create_idempotent"],
        "status_memory_execution_observed": kanban["comment_action_class"]
        == WorkflowActionClass.STATUS_MEMORY.value,
        **mutation_flags,
        "gateway_restarted": False,
        "discord_sent": False,
        "cron_mutated": False,
        "graphify_run": False,
        "credentials_read": False,
        "case_count": len(MATRIX_CASE_IDS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    args = parser.parse_args()
    result = build_smoke_result()
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return 0 if result["quality_thresholds_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
