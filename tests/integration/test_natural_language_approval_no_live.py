"""No-live integration contract for executable approval smoke probes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = ROOT / "scripts" / "smoke_natural_language_approval_no_live.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "approval_no_live_smoke_integration", SMOKE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_live_smoke_executes_real_synthetic_seams_with_temp_state():
    smoke = _load_smoke_module()
    result = smoke.build_smoke_result()

    assert result["schema"] == "natural-language-approval-no-live-smoke/v2"
    assert result["quality_thresholds_passed"] is True
    assert result["trusted_actions_prompt_zero"] is True
    assert result["guarded_actions_prompt_one_or_deny"] is True
    assert result["historical_pipe_still_warns"] is True
    assert result["session_cache_reported"] is True
    assert result["registered_recorder_contract_passed"] is True
    assert result["matrix_expectations_passed"] is True
    assert result["blocked_card_idempotency_executed"] is True
    assert result["status_memory_execution_observed"] is True
    assert result["case_count"] == 10

    for field in (
        "live_db_mutated",
        "kanban_mutated",
        "config_mutated",
        "gateway_restarted",
        "discord_sent",
        "cron_mutated",
        "graphify_run",
        "credentials_read",
    ):
        assert result[field] is False

    rendered = repr(result).lower()
    for forbidden in (
        "user_message",
        "discord_id",
        "payload_path",
        "command_text",
        "database_row",
        "content_hash",
    ):
        assert forbidden not in rendered


def test_quality_turns_red_when_a_real_guard_probe_fails(monkeypatch):
    smoke = _load_smoke_module()
    original = smoke._probe_approval_guards

    def broken_probe():
        observed = original()
        observed["pipe"] = {"approved": True, "message": None}
        return observed

    monkeypatch.setattr(smoke, "_probe_approval_guards", broken_probe)
    result = smoke.build_smoke_result()
    assert result["quality_thresholds_passed"] is False
    assert result["historical_pipe_still_warns"] is False
    assert result["guarded_actions_prompt_one_or_deny"] is False


def test_registered_recorder_probe_uses_executable_route_and_rejects_missing_authority():
    smoke = _load_smoke_module()
    result = smoke._probe_registered_recorder()

    assert result["production_route_exercised"] is True
    assert result["write_validated"] is True
    assert result["idempotent_retry"] is True
    assert result["record_intents_rejected"] == ("planned", "ambiguous")
    assert result["unregistered_recorders_rejected"] == (
        "child_health.v1",
        "medication.v1",
    )


def test_live_boundaries_are_compared_independently():
    smoke = _load_smoke_module()
    groups = {
        "config": [Path("/live/config.yaml")],
        "kanban": [Path("/live/kanban.db")],
        "lifelog": [Path("/live/lifelog.db")],
    }
    before = {
        "/live/config.yaml": (True, 1, 10),
        "/live/kanban.db": (True, 1, 20),
        "/live/lifelog.db": (True, 1, 30),
    }

    expected = {
        "config": "config_mutated",
        "kanban": "kanban_mutated",
        "lifelog": "live_db_mutated",
    }
    for changed_boundary, expected_flag in expected.items():
        after = dict(before)
        changed_path = str(groups[changed_boundary][0])
        exists, mtime, size = after[changed_path]
        after[changed_path] = (exists, mtime + 1, size)
        flags = smoke._live_boundary_mutation_flags(groups, before, after)
        assert flags[expected_flag] is True
        assert sum(flags.values()) == 1
