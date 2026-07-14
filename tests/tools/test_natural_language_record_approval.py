"""Execution tests for natural-language approval hardening no-live probes."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.tirith_security import check_command_security

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "scripts" / "smoke_natural_language_approval_no_live.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("approval_no_live_smoke", SMOKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases_by_id() -> dict[str, dict]:
    matrix = _load_smoke_module().build_matrix()
    assert matrix["schema"] == "approval-decision-matrix/v1"
    return {case["id"]: case for case in matrix["cases"]}


def test_matrix_records_observed_real_seam_outcomes():
    cases = _cases_by_id()
    assert cases["kanban_comment_direct"]["observed_prompt_count"] == 0
    assert cases["kanban_comment_direct"]["observed_execution"] == "allow"

    pipe_case = cases["kanban_comment_pipe_python_readback"]
    assert pipe_case["action_class"] == "status_memory"
    assert pipe_case["observed_prompt_count"] == 1
    assert pipe_case["observed_execution"] == "prompt"
    assert pipe_case["observed_guard_rule"] == "pipe-to-interpreter"
    assert pipe_case["decision_evidence"]["guard_source"] == "tirith"

    assert cases["diet_registered_direct"]["action_class"] == "trusted_local_record"
    assert cases["diet_registered_direct"]["observed_prompt_count"] == 0
    assert cases["arbitrary_python_sqlite"]["observed_prompt_count"] == 1


def test_matrix_matches_only_currently_registered_recorder_scope():
    cases = _cases_by_id()
    expected = {
        "blocked_card_create_idempotent": "explicit_blocked_card_create",
        "planned_record_intent": "read_only",
        "ambiguous_record_intent": "read_only",
    }
    for case_id, action_class in expected.items():
        assert cases[case_id]["action_class"] == action_class

    assert cases["blocked_card_create_idempotent"]["observed_execution"] == "allow"
    assert cases["blocked_card_create_idempotent"]["command_shape"].endswith(
        "with_idempotency_key"
    )
    assert cases["planned_record_intent"]["observed_execution"] == "no_write"
    assert cases["ambiguous_record_intent"]["observed_execution"] == "no_write"
    assert cases["child_health_unregistered"]["action_class"] == (
        "approval_required_live_mutation"
    )
    assert cases["child_health_unregistered"]["observed_execution"] == (
        "deny_unregistered"
    )
    assert cases["medication_unregistered"]["action_class"] == (
        "approval_required_live_mutation"
    )
    assert cases["medication_unregistered"]["observed_execution"] == (
        "deny_unregistered"
    )


def test_every_observed_matrix_row_has_raw_free_decision_evidence():
    for case in _cases_by_id().values():
        evidence = case["decision_evidence"]
        assert evidence["schema"] == "approval-decision-evidence/v1"
        if case["id"] == "kanban_comment_pipe_python_readback":
            assert case["action_class"] == "status_memory"
            assert case["guard_action_class"] == "approval_required_live_mutation"
            assert evidence["action_class"] == case["guard_action_class"]
            assert evidence["command_shape"] == "interpreter_pipe"
        else:
            assert evidence["action_class"] == case["action_class"]
            assert evidence["command_shape"] == case["command_shape"]
        assert evidence["prompt_count"] == case["observed_prompt_count"]
        rendered = repr(evidence)
        assert "discord:" not in rendered
        assert "SELECT " not in rendered
        assert "UPDATE " not in rendered
        assert "Synthetic verification checkpoint" not in rendered


def test_session_cache_is_observed_without_hiding_action_class():
    cases = _cases_by_id()
    before = cases["arbitrary_python_sqlite"]
    after = cases["session_cache_after_approval"]
    assert before["action_class"] == after["action_class"]
    assert before["command_shape"] == after["command_shape"]
    assert before["decision_evidence"]["session_cache_influenced"] is False
    assert after["decision_evidence"]["session_cache_influenced"] is True
    assert after["observed_prompt_count"] == 0


def test_historical_compound_pipe_remains_tirith_warning(monkeypatch):
    synthetic_command = (
        "hermes kanban comment --board synthetic --card t_deadbeef --body note ; "
        "hermes kanban show --board synthetic --card t_deadbeef --json | "
        "python3 -c 'import json,sys; json.load(sys.stdin)'"
    )
    result = SimpleNamespace(
        returncode=2,
        stdout=(
            '{"summary":"synthetic warning","findings":['
            '{"rule_id":"pipe-to-interpreter","severity":"high"}]}'
        ),
        stderr="",
    )
    monkeypatch.setattr("tools.tirith_security._resolve_tirith_path", lambda _: "tirith")
    monkeypatch.setattr("tools.tirith_security.is_platform_supported", lambda: True)
    monkeypatch.setattr(
        "tools.tirith_security._load_security_config",
        lambda: {
            "tirith_enabled": True,
            "tirith_path": "tirith",
            "tirith_timeout": 5,
            "tirith_fail_open": True,
        },
    )
    with patch("tools.tirith_security.subprocess.run", return_value=result) as run:
        verdict = check_command_security(synthetic_command)
    assert verdict["action"] == "warn"
    assert verdict["findings"][0]["rule_id"] == "pipe-to-interpreter"
    assert run.call_args.args[0][-1] == synthetic_command
    assert run.call_args.kwargs["stdin"] is subprocess.DEVNULL


def test_shared_action_taxonomy_exists():
    from agent.workflow_action_policy import WorkflowActionClass  # noqa: F401
