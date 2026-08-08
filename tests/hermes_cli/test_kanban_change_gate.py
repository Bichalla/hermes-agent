"""Phase 2 Change Gate enforcement and model-route contract tests."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_lane_roles import parse_contract_body


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(home / "attachments"))
    kb.init_db()
    return home


@pytest.fixture
def canonical_kanban_home(monkeypatch):
    home = Path("/Users/honbul/.hermes/tmp") / f"phase2-kanban-test-{uuid.uuid4().hex}"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(home / "attachments"))
    kb.init_db()
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def implementation_body(**change_gate):
    return json.dumps(
        {
            "contract": {
                "lane": "implementation",
                "type": "code",
                "risk_class": "S2",
                "human_required": False,
                "approval_boundary": ["manual approval"],
                "acceptance_criteria": ["tests pass"],
                "verification": ["pytest"],
                "stop_conditions": ["scope deviation"],
                "change_gate": change_gate,
            }
        }
    )


def _write_handoff(home: Path, task_id: str, *, model: str = "gpt-5.6-luna", bootstrap: bool = False, handoff_task_id: str | None = None) -> Path:
    evidence_path = home / f"evidence-{task_id}.json"
    evidence = {
        "schema": "change-gate-evidence/v1",
        "packet_id": f"ev-{task_id}",
        "task_id": handoff_task_id or task_id,
        "created_at": "2026-08-08T16:00:00+09:00",
        "baseline": {
            "repository_or_root": "/tmp/change-gate-test",
            "revision": "abc123",
            "dirty_state": "clean",
            "watched_hashes": {"AGENTS.md": "a" * 64},
        },
        "requirement": {"objective": "bounded gate", "applicability": "implementation"},
        "existing_capabilities": [],
        "similar_responsibilities": [],
        "ownership": {"canonical_owner": "change-gate policy", "mutation_owner": "Planner", "projections": []},
        "runtime_activation": {"entry": "scripts/change_gate_validate.py", "activation_gate": "manual", "status": "CONFIRMED", "direct_consumers": ["tests"], "shared_contracts": ["JSON Schema"]},
        "consumers": [],
        "decision_candidates": {"reuse": [], "extend": [], "new": ["claim adapter"]},
        "unknowns": [],
        "source_refs": [{"path": "reports/HERMES_CHANGE_GATE_DESIGN.md", "symbol": "# 1", "finding": "policy owner"}],
    }
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    handoff = {
        "schema": "change-gate-handoff/v1",
        "handoff_id": f"ho-{task_id}",
        "revision": 1,
        "previous_handoff": None,
        "policy_version": "change-gate-policy/v1",
        "task_id": handoff_task_id or task_id,
        "objective": "Execute bounded implementation",
        "decision": "EXTEND",
        "risk_tier": "HIGH",
        "baseline": {"repository": "/tmp/change-gate-test", "revision": "abc123", "evidence_revision": "ev-001", "watched_hashes": {"AGENTS.md": "a" * 64}},
        "evidence_ref": str(evidence_path),
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "scope": {"allowed_paths": ["/Users/honbul/.hermes/policies/change-gate/policy.yaml"], "protected_paths": ["/Users/honbul/.hermes/config.yaml"], "required_changes": ["claim adapter"], "forbidden_actions": ["live activation"]},
        "contracts": {"preserved": ["legacy tasks"], "acceptance_criteria": ["invalid claim blocked"], "targeted_tests": ["test_kanban_change_gate.py"]},
        "execution_route": {"role": "EXECUTOR", "profile": "change-gate-xhigh", "provider": "openai-codex", "model": model, "reasoning_effort": "xhigh"},
        "architecture_inventory_effect": "NONE",
        "state": "PLAN_APPROVED",
        "issued_by": "Sol Planner",
        "issued_at": "2026-08-08T16:01:00+09:00",
    }
    if bootstrap:
        handoff["bootstrap"] = {"active": True, "reason": "CHANGE_GATE_RUNTIME_NOT_YET_AVAILABLE"}
    source = home / f"handoff-{task_id}.json"
    source.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")
    return source


def _attach_handoff(conn, home: Path, task_id: str, *, bootstrap: bool = False, model: str = "gpt-5.6-luna", handoff_task_id: str | None = None) -> Path:
    source = _write_handoff(home, task_id, bootstrap=bootstrap, model=model, handoff_task_id=handoff_task_id)
    stored = home / "attachments" / task_id / "handoff.json"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(source.read_bytes())
    kb.add_attachment(conn, task_id, filename="handoff.json", stored_path=str(stored), size=stored.stat().st_size)
    return stored


def _prepare_gate_task(conn, home: Path, *, bootstrap: bool = False, model: str = "gpt-5.6-luna", handoff_task_id: str | None = None):
    task_id = kb.create_task(conn, title="gated implementation", body=json.dumps({"contract": {"lane": "implementation"}}), assignee="change-gate-xhigh", model_override=model)
    stored = _attach_handoff(conn, home, task_id, bootstrap=bootstrap, model=model, handoff_task_id=handoff_task_id)
    body = implementation_body(stage="PLAN_APPROVED", artifact_ref=str(stored), artifact_sha256=hashlib.sha256(stored.read_bytes()).hexdigest(), role="EXECUTOR")
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
    conn.commit()
    return task_id, stored


def test_lane_contract_exposes_bounded_change_gate_object():
    contract = parse_contract_body(
        implementation_body(
            stage="PLAN_APPROVED",
            artifact_ref="/Users/honbul/.hermes/tmp/handoff.json",
            artifact_sha256="a" * 64,
            role="EXECUTOR",
            review_outcome="PASS",
        )
    )

    assert contract.parseable is True
    assert contract.lane == "implementation"
    assert contract.change_gate.stage == "PLAN_APPROVED"
    assert contract.change_gate.artifact_ref.endswith("handoff.json")
    assert contract.change_gate.artifact_sha256 == "a" * 64
    assert contract.change_gate.role == "EXECUTOR"
    assert contract.change_gate.review_outcome == "PASS"


def test_lane_contract_rejects_unknown_or_malformed_change_gate_fields():
    unknown = parse_contract_body(
        implementation_body(
            stage="PLAN_APPROVED",
            artifact_ref="/tmp/handoff.json",
            artifact_sha256="a" * 64,
            role="EXECUTOR",
            unexpected="nope",
        )
    )
    malformed = parse_contract_body(
        json.dumps({"contract": {"lane": "implementation", "change_gate": "not-an-object"}})
    )

    assert unknown.parseable is False
    assert malformed.parseable is False


def test_create_task_model_override_round_trips_without_sql(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="model route",
            assignee="worker",
            model_override="gpt-5.6-luna",
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.model_override == "gpt-5.6-luna"


def test_implementation_without_metadata_is_blocked_before_claim_side_effects(kanban_home):
    body = json.dumps({"contract": {"lane": "implementation"}})
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="needs gate", body=body, assignee="worker")
        before_events = len(kb.list_events(conn, task_id))
        before_runs = len(kb.list_runs(conn, task_id))
        blocked_type = getattr(kb, "ChangeGateBlocked", RuntimeError)

        with pytest.raises(blocked_type) as exc_info:
            kb.claim_task(conn, task_id)

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None
        assert len(kb.list_runs(conn, task_id)) == before_runs
        assert len(kb.list_events(conn, task_id)) == before_events
        assert "CHANGE_GATE_METADATA_MISSING" in str(exc_info.value)


def test_valid_handoff_claims_once_and_invalid_bootstrap_has_no_side_effects(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        before_events = len(kb.list_events(conn, task_id))
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.status == "running"
        assert len(kb.list_runs(conn, task_id)) == 1
        assert kb.claim_task(conn, task_id) is None

        bootstrap_id, _ = _prepare_gate_task(conn, canonical_kanban_home, bootstrap=True)
        bootstrap_events = len(kb.list_events(conn, bootstrap_id))
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, bootstrap_id)
        assert exc_info.value.reason_codes == ["CHANGE_GATE_BOOTSTRAP_NOT_ALLOWED"]
        blocked = kb.get_task(conn, bootstrap_id)
        assert blocked is not None and blocked.status == "ready" and blocked.claim_lock is None
        assert len(kb.list_runs(conn, bootstrap_id)) == 0
        assert len(kb.list_events(conn, bootstrap_id)) == bootstrap_events
        assert len(kb.list_events(conn, task_id)) == before_events + 1


def test_attachment_authority_and_digest_fail_closed(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        current = kb.get_task(conn, task_id)
        assert current is not None and current.body is not None
        body = json.loads(current.body)
        body["contract"]["change_gate"]["artifact_sha256"] = "0" * 64
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (json.dumps(body), task_id))
        conn.commit()
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)
        assert exc_info.value.reason_codes == ["CHANGE_GATE_DIGEST_MISMATCH"]
        assert kb.get_task(conn, task_id).status == "ready"

        unattached_id = kb.create_task(
            conn,
            title="unattached implementation",
            body=implementation_body(
                stage="PLAN_APPROVED",
                artifact_ref=str(canonical_kanban_home / "missing-handoff.json"),
                artifact_sha256="a" * 64,
                role="EXECUTOR",
            ),
            assignee="change-gate-xhigh",
            model_override="gpt-5.6-luna",
        )
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, unattached_id)
        assert exc_info.value.reason_codes == ["CHANGE_GATE_ARTIFACT_NOT_ATTACHED"]


def test_dispatch_dry_run_skips_invalid_gate_without_mutation(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="dry-run invalid implementation",
            body=json.dumps({"contract": {"lane": "implementation"}}),
            assignee="change-gate-xhigh",
            model_override="gpt-5.6-luna",
        )
        before = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        before_events = len(kb.list_events(conn, task_id))
        from hermes_cli import profiles
        monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
        result = kb.dispatch_once(conn, dry_run=True)
        after = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    assert result.spawned == []
    assert result.skipped_change_gate == [{"task_id": task_id, "reason_codes": ["CHANGE_GATE_METADATA_MISSING"]}]
    assert tuple(before) == tuple(after)
    with kb.connect() as check_conn:
        assert len(kb.list_events(check_conn, task_id)) == before_events
        checked = kb.get_task(check_conn, task_id)
        assert checked is not None and checked.status == "ready"


def test_nonimplementation_lane_ignores_gate_and_legacy_body_stays_unchanged(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="planning card mentioning implementation",
            body=json.dumps({"contract": {"lane": "planning", "type": "plan"}}),
            assignee="default",
        )
        claimed = kb.claim_task(conn, task_id)

    assert claimed is not None
    assert claimed.status == "running"


def test_model_override_cli_parser_and_default_spawn_argv(monkeypatch):
    from hermes_cli.kanban import build_parser
    import argparse
    from hermes_cli import kanban_db as db

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_parser(sub)
    args = parser.parse_args(["kanban", "create", "route", "--assignee", "worker", "--model", "gpt-5.6-luna"])
    assert args.model == "gpt-5.6-luna"

    task = db.Task(
        id="t_route", title="route", body=None, assignee="worker", status="running", priority=0,
        created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path="/tmp", claim_lock="lock",
        claim_expires=None, tenant=None, model_override="gpt-5.6-luna",
    )
    calls = {}
    monkeypatch.setattr(db, "resolve_workspace", lambda _task: "/tmp")
    monkeypatch.setattr(db, "_resolve_hermes_argv", lambda: ["hermes"])
    def fake_popen(cmd, **kwargs):
        calls["cmd"] = cmd
        return type("P", (), {"pid": 123})()
    monkeypatch.setattr(db.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(db, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(db, "worker_logs_dir", lambda board=None: Path("/tmp"))
    monkeypatch.setattr(db, "worker_log_rotation_config", lambda: (1024, 1))
    monkeypatch.setattr(db, "kanban_db_path", lambda board=None: Path("/tmp/kanban.db"))
    monkeypatch.setattr(db, "workspaces_root", lambda board=None: Path("/tmp/workspaces"))
    monkeypatch.setattr(db, "get_current_board", lambda: "default")
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _profile: "/tmp")
    db._default_spawn(task, "/tmp")
    assert calls["cmd"][calls["cmd"].index("-m") + 1] == "gpt-5.6-luna"


def test_cli_create_and_kanban_tool_round_trip_model_override(kanban_home, capsys):
    from hermes_cli.kanban import build_parser, kanban_command
    from tools import kanban_tools
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_parser(sub)
    args = parser.parse_args(["kanban", "create", "cli route", "--assignee", "default", "--model", "gpt-5.6-luna"])
    assert kanban_command(args) == 0
    output = capsys.readouterr().out
    task_id = output.strip().split()[1]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.model_override == "gpt-5.6-luna"

    result = json.loads(kanban_tools._handle_create({
        "title": "tool route",
        "assignee": "default",
        "model": "gpt-5.6-luna",
    }))
    assert result["ok"] is True
    assert result["model"] == "gpt-5.6-luna"
    with kb.connect() as conn:
        tool_task = kb.get_task(conn, result["task_id"])
        assert tool_task is not None and tool_task.model_override == "gpt-5.6-luna"
