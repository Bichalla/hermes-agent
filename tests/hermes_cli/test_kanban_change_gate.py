"""Phase 2 Change Gate enforcement and model-route contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
import hermes_cli.kanban_lane_roles as lane_roles
from hermes_cli.kanban_lane_roles import parse_contract_body
from gateway.kanban_watchers import _gate_skip_changed


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


def _claim_blocked_without_side_effects(conn, task_id, expected_code):
    before = kb.get_task(conn, task_id)
    assert before is not None
    before_events = len(kb.list_events(conn, task_id))
    before_runs = len(kb.list_runs(conn, task_id))
    with pytest.raises(kb.ChangeGateBlocked) as exc_info:
        kb.claim_task(conn, task_id)
    after = kb.get_task(conn, task_id)
    assert after is not None
    assert after.status == "ready"
    assert after.claim_lock is None
    assert len(kb.list_events(conn, task_id)) == before_events
    assert len(kb.list_runs(conn, task_id)) == before_runs
    assert after.workspace_path == before.workspace_path
    assert exc_info.value.reason_codes == [expected_code]


def _rewrite_handoff(conn, task_id, stored: Path, mutate_handoff=None, mutate_evidence=None):
    handoff = json.loads(stored.read_text(encoding="utf-8"))
    evidence_path = Path(handoff["evidence_ref"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if mutate_evidence is not None:
        mutate_evidence(evidence)
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        handoff["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    if mutate_handoff is not None:
        mutate_handoff(handoff)
    stored.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")
    task = kb.get_task(conn, task_id)
    assert task is not None and task.body is not None
    body = json.loads(task.body)
    body["contract"]["change_gate"]["artifact_sha256"] = hashlib.sha256(stored.read_bytes()).hexdigest()
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (json.dumps(body), task_id))
    conn.commit()


def _rebind_handoff_card(conn, task_id, stored: Path):
    task = kb.get_task(conn, task_id)
    assert task is not None and task.body is not None
    body = json.loads(task.body)
    body["contract"]["change_gate"]["artifact_sha256"] = hashlib.sha256(stored.read_bytes()).hexdigest()
    conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (json.dumps(body), task_id))
    conn.commit()


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


def test_present_malformed_change_gate_is_schema_invalid(kanban_home):
    body = implementation_body(
        stage="PLAN_APPROVED",
        artifact_ref="/Users/honbul/.hermes/missing.json",
        artifact_sha256="a" * 64,
        role="EXECUTOR",
        unexpected="reject",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="malformed gate", body=body, assignee="worker")
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)
    assert exc_info.value.reason_codes == ["CHANGE_GATE_SCHEMA_INVALID"]


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


def test_evidence_missing_and_canonical_root_escape_fail_closed(canonical_kanban_home):
    with kb.connect() as conn:
        missing_id, missing_stored = _prepare_gate_task(conn, canonical_kanban_home)
        missing_handoff = json.loads(missing_stored.read_text(encoding="utf-8"))
        Path(missing_handoff["evidence_ref"]).unlink()
        _claim_blocked_without_side_effects(conn, missing_id, "CHANGE_GATE_ARTIFACT_UNSAFE")

        escape_id, escape_stored = _prepare_gate_task(conn, canonical_kanban_home)
        _rewrite_handoff(
            conn,
            escape_id,
            escape_stored,
            mutate_handoff=lambda handoff: handoff.update({"evidence_ref": "/tmp/change-gate-escape.json"}),
        )
        _claim_blocked_without_side_effects(conn, escape_id, "CHANGE_GATE_ARTIFACT_UNSAFE")


def test_evidence_digest_mismatch_maps_to_evidence_invalid(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        handoff = json.loads(stored.read_text(encoding="utf-8"))
        evidence_path = Path(handoff["evidence_ref"])
        evidence_path.write_text(evidence_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_EVIDENCE_INVALID")


def test_evidence_schema_and_blocking_unknown_fail_closed(canonical_kanban_home):
    with kb.connect() as conn:
        schema_id, schema_stored = _prepare_gate_task(conn, canonical_kanban_home)
        _rewrite_handoff(conn, schema_id, schema_stored, mutate_evidence=lambda evidence: evidence.clear())
        _claim_blocked_without_side_effects(conn, schema_id, "CHANGE_GATE_EVIDENCE_INVALID")

        unknown_id, unknown_stored = _prepare_gate_task(conn, canonical_kanban_home)
        _rewrite_handoff(
            conn,
            unknown_id,
            unknown_stored,
            mutate_evidence=lambda evidence: evidence.update({
                "unknowns": [{
                    "item": "blocking unknown",
                    "blocking": True,
                    "impact": "cannot verify",
                    "required_resolution": "planner decision",
                }]
            }),
        )
        _claim_blocked_without_side_effects(conn, unknown_id, "CHANGE_GATE_BLOCKING_UNKNOWN")


def test_handoff_card_digest_mismatch_remains_distinct(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.body is not None
        body = json.loads(task.body)
        body["contract"]["change_gate"]["artifact_sha256"] = "0" * 64
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (json.dumps(body), task_id))
        conn.commit()
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_DIGEST_MISMATCH")


def test_task_profile_model_effort_and_handoff_identity_mismatches_fail_closed(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home, handoff_task_id="other-task")
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_TASK_MISMATCH")

        profile_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        conn.execute("UPDATE tasks SET assignee = 'other-profile' WHERE id = ?", (profile_id,))
        conn.commit()
        _claim_blocked_without_side_effects(conn, profile_id, "CHANGE_GATE_PROFILE_MISMATCH")

        model_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        conn.execute("UPDATE tasks SET model_override = 'gpt-5.6-sol' WHERE id = ?", (model_id,))
        conn.commit()
        _claim_blocked_without_side_effects(conn, model_id, "CHANGE_GATE_MODEL_MISMATCH")

        effort_id, effort_stored = _prepare_gate_task(conn, canonical_kanban_home)
        _rewrite_handoff(
            conn,
            effort_id,
            effort_stored,
            mutate_handoff=lambda handoff: handoff["execution_route"].update({"reasoning_effort": "high"}),
        )
        _claim_blocked_without_side_effects(conn, effort_id, "CHANGE_GATE_EFFORT_MISMATCH")


def test_wrong_stage_and_role_fail_before_artifact_authority(canonical_kanban_home):
    with kb.connect() as conn:
        stage_id = kb.create_task(
            conn,
            title="wrong stage",
            body=implementation_body(
                stage="DRAFT",
                artifact_ref=str(canonical_kanban_home / "missing.json"),
                artifact_sha256="a" * 64,
                role="EXECUTOR",
            ),
            assignee="change-gate-xhigh",
            model_override="gpt-5.6-luna",
        )
        _claim_blocked_without_side_effects(conn, stage_id, "CHANGE_GATE_STAGE_INVALID")

        role_id = kb.create_task(
            conn,
            title="wrong role",
            body=implementation_body(
                stage="PLAN_APPROVED",
                artifact_ref=str(canonical_kanban_home / "missing.json"),
                artifact_sha256="a" * 64,
                role="REVIEWER",
            ),
            assignee="change-gate-xhigh",
            model_override="gpt-5.6-luna",
        )
        _claim_blocked_without_side_effects(conn, role_id, "CHANGE_GATE_ROLE_INVALID")


def test_attached_handoff_path_missing_is_artifact_missing(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        stored.unlink()
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_ARTIFACT_MISSING")


def test_attached_handoff_non_regular_is_artifact_unsafe(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        stored.unlink()
        stored.mkdir()
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_ARTIFACT_UNSAFE")


def test_attached_handoff_attachment_root_escape_is_artifact_unsafe(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        escaped = canonical_kanban_home / "escaped-handoff.json"
        escaped.write_bytes(stored.read_bytes())
        conn.execute("UPDATE task_attachments SET stored_path = ? WHERE task_id = ?", (str(escaped), task_id))
        task = kb.get_task(conn, task_id)
        assert task is not None and task.body is not None
        body = json.loads(task.body)
        body["contract"]["change_gate"]["artifact_ref"] = str(escaped)
        body["contract"]["change_gate"]["artifact_sha256"] = hashlib.sha256(escaped.read_bytes()).hexdigest()
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (json.dumps(body), task_id))
        conn.commit()
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_ARTIFACT_UNSAFE")


def test_malformed_handoff_with_correct_card_digest_is_schema_invalid(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        stored.write_bytes(b"{ malformed handoff")
        _rebind_handoff_card(conn, task_id, stored)
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_SCHEMA_INVALID")


def test_policy_version_mismatch_with_rebound_digests_is_blocked(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        _rewrite_handoff(
            conn,
            task_id,
            stored,
            mutate_handoff=lambda handoff: handoff.update({"policy_version": "change-gate-policy/v999"}),
        )
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_POLICY_VERSION_MISMATCH")


def test_validator_source_a_to_b_reloads_between_readiness_validations(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_a, _ = _prepare_gate_task(conn, canonical_kanban_home)
        task_b, _ = _prepare_gate_task(conn, canonical_kanban_home)
        a = kb.get_task(conn, task_a)
        b = kb.get_task(conn, task_b)
        assert a is not None and b is not None
        attachments_a = kb.list_attachments(conn, task_a)
        attachments_b = kb.list_attachments(conn, task_b)
        original_read = lane_roles._read_trusted_file
        original_load = lane_roles._load_change_gate_validator
        source_path = lane_roles._CANONICAL_CHANGE_GATE_ROOT / "scripts/change_gate_validate.py"
        source_reads = {"count": 0}
        loaded_digests = []

        def source_a_then_b(root, path, *, limit):
            snapshot = original_read(root, path, limit=limit)
            if Path(path) == source_path:
                source_reads["count"] += 1
                if source_reads["count"] >= 3:
                    changed = snapshot.data + b"\n# deterministic source B\n"
                    return replace(snapshot, data=changed, sha256=hashlib.sha256(changed).hexdigest())
            return snapshot

        def load_record(source):
            loaded_digests.append(source.sha256)
            return original_load(source)

        monkeypatch.setattr(lane_roles, "_read_trusted_file", source_a_then_b)
        monkeypatch.setattr(lane_roles, "_load_change_gate_validator", load_record)
        first = lane_roles.check_change_gate_readiness(
            a, attachments_a, attachment_root=kb.task_attachments_dir(task_a)
        )
        second = lane_roles.check_change_gate_readiness(
            b, attachments_b, attachment_root=kb.task_attachments_dir(task_b)
        )

    assert first.ok is True
    assert second.ok is True
    assert len(loaded_digests) == 2
    assert loaded_digests[0] != loaded_digests[1]
    assert source_reads["count"] >= 4


def test_intermediate_directory_identity_is_bound_to_snapshot(canonical_kanban_home):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        root = stored.parent.parent
        replacement_dir = stored.parent.with_name(stored.parent.name + "-replacement")
        replacement_dir.mkdir()
        os.link(stored, replacement_dir / stored.name)
        pre = lane_roles._read_trusted_file(root, stored, limit=1024 * 1024)
        old_dir = stored.parent.with_name(stored.parent.name + "-old")
        os.rename(stored.parent, old_dir)
        os.rename(replacement_dir, stored.parent)
        post = lane_roles._read_trusted_file(root, stored, limit=1024 * 1024)

    assert pre.path == post.path
    assert pre.identity == post.identity
    assert pre.sha256 == post.sha256
    assert pre.directory_identities != post.directory_identities
    assert lane_roles._snapshot_same(pre, post) is False


def test_claim_fails_on_real_intermediate_directory_replacement(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        original_read = lane_roles._read_trusted_file
        original_load = lane_roles._load_change_gate_validator
        state = {"validated": False, "handoff_reads": 0, "swapped": False}
        replacement_dir = stored.parent.with_name(stored.parent.name + "-replacement")
        replacement_dir.mkdir()
        os.link(stored, replacement_dir / stored.name)

        def load_proxy(source):
            actual = original_load(source)

            class Proxy:
                ValidationError = actual.ValidationError

                @staticmethod
                def validate_artifact(*args, **kwargs):
                    result = actual.validate_artifact(*args, **kwargs)
                    state["validated"] = True
                    return result

            lane_roles._validator_module_digest = source.sha256
            return Proxy()

        def swap_before_post_snapshot(root, path, *, limit):
            if Path(path) == stored and state["validated"]:
                state["handoff_reads"] += 1
                if state["handoff_reads"] == 1 and not state["swapped"]:
                    old_dir = stored.parent.with_name(stored.parent.name + "-old")
                    os.rename(stored.parent, old_dir)
                    os.rename(replacement_dir, stored.parent)
                    state["swapped"] = True
            return original_read(root, path, limit=limit)

        monkeypatch.setattr(lane_roles, "_load_change_gate_validator", load_proxy)
        monkeypatch.setattr(lane_roles, "_read_trusted_file", swap_before_post_snapshot)
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_ARTIFACT_UNSAFE")

    assert state == {"validated": True, "handoff_reads": 1, "swapped": True}


def test_handoff_final_and_intermediate_symlinks_fail_closed(canonical_kanban_home):
    with kb.connect() as conn:
        final_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        outside = canonical_kanban_home / "outside.json"
        outside.write_bytes(stored.read_bytes())
        stored.unlink()
        stored.symlink_to(outside)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, final_id)
        assert exc_info.value.reason_codes == ["CHANGE_GATE_ARTIFACT_UNSAFE"]

        intermediate_id, intermediate = _prepare_gate_task(conn, canonical_kanban_home)
        outside_dir = canonical_kanban_home / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "handoff.json").write_bytes(intermediate.read_bytes())
        task_dir = intermediate.parent
        intermediate.unlink()
        task_dir.rmdir()
        task_dir.symlink_to(outside_dir, target_is_directory=True)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, intermediate_id)
        assert exc_info.value.reason_codes == ["CHANGE_GATE_ARTIFACT_UNSAFE"]


def test_handoff_replacement_during_validation_is_unsafe(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        original = lane_roles._read_trusted_file
        reads = {"handoff": 0}

        def replaced_after_validation(root, target, *, limit):
            snapshot = original(root, target, limit=limit)
            if Path(target) == stored:
                reads["handoff"] += 1
                if reads["handoff"] == 2:
                    return replace(snapshot, identity=tuple(value + 1 for value in snapshot.identity))
            return snapshot

        monkeypatch.setattr(lane_roles, "_read_trusted_file", replaced_after_validation)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)

    assert exc_info.value.reason_codes == ["CHANGE_GATE_ARTIFACT_UNSAFE"]
    assert reads["handoff"] == 2


def test_cached_validator_reloads_when_canonical_source_digest_changes():
    source = lane_roles._read_trusted_file(
        lane_roles._CANONICAL_CHANGE_GATE_ROOT,
        lane_roles._CANONICAL_CHANGE_GATE_ROOT / "scripts/change_gate_validate.py",
        limit=lane_roles._CHANGE_GATE_POLICY_LIMIT,
    )
    first = lane_roles._load_change_gate_validator(source)
    changed = replace(
        source,
        data=source.data + b"\n# synthetic source revision\n",
        sha256=hashlib.sha256(source.data + b"\n# synthetic source revision\n").hexdigest(),
    )
    second = lane_roles._load_change_gate_validator(changed)
    assert second is not first
    assert lane_roles._validator_module_digest == changed.sha256

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


def test_authoritative_gate_runs_inside_active_write_transaction(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        seen = []
        original = kb._check_change_gate_before_claim

        def wrapped(connection, *args, **kwargs):
            seen.append(connection.in_transaction)
            return original(connection, *args, **kwargs)

        monkeypatch.setattr(kb, "_check_change_gate_before_claim", wrapped)
        claimed = kb.claim_task(conn, task_id)

    assert claimed is not None
    assert seen == [True]


def test_competing_sqlite_writer_is_locked_during_authority_snapshot(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        original = kb._check_change_gate_before_claim
        blocked = []

        def wrapped(connection, *args, **kwargs):
            contender = sqlite3.connect(str(kb.kanban_db_path()), timeout=0.05)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
                blocked.append(True)
            finally:
                contender.close()
            return original(connection, *args, **kwargs)

        monkeypatch.setattr(kb, "_check_change_gate_before_claim", wrapped)
        assert kb.claim_task(conn, task_id) is not None

    assert blocked == [True]


def _assert_authority_input_mutation_is_unsafe(canonical_kanban_home, monkeypatch, target_name):
    with kb.connect() as conn:
        task_id, _ = _prepare_gate_task(conn, canonical_kanban_home)
        original_read = lane_roles._read_trusted_file
        original_load = lane_roles._load_change_gate_validator
        mutated = {"done": False}
        target = lane_roles._CANONICAL_CHANGE_GATE_ROOT / target_name

        def load_proxy(source):
            actual = original_load(source)

            class Proxy:
                ValidationError = actual.ValidationError

                @staticmethod
                def validate_artifact(*args, **kwargs):
                    result = actual.validate_artifact(*args, **kwargs)
                    mutated["done"] = True
                    return result

            lane_roles._validator_module_digest = source.sha256
            return Proxy()

        def changed_post_snapshot(root, path, *, limit):
            snapshot = original_read(root, path, limit=limit)
            if mutated["done"] and Path(path) == target:
                changed = snapshot.data + b"\n# simulated concurrent replacement\n"
                return replace(
                    snapshot,
                    data=changed,
                    sha256=hashlib.sha256(changed).hexdigest(),
                )
            return snapshot

        monkeypatch.setattr(lane_roles, "_load_change_gate_validator", load_proxy)
        monkeypatch.setattr(lane_roles, "_read_trusted_file", changed_post_snapshot)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)

    assert mutated["done"] is True
    assert exc_info.value.reason_codes == ["CHANGE_GATE_ARTIFACT_UNSAFE"]


def test_policy_mutation_during_canonical_validation_is_unsafe(canonical_kanban_home, monkeypatch):
    _assert_authority_input_mutation_is_unsafe(
        canonical_kanban_home, monkeypatch, "policies/change-gate/policy.yaml"
    )


def test_schema_mutation_during_canonical_validation_is_unsafe(canonical_kanban_home, monkeypatch):
    _assert_authority_input_mutation_is_unsafe(
        canonical_kanban_home,
        monkeypatch,
        "policies/change-gate/frozen-handoff.schema.json",
    )


def test_evidence_mutation_during_canonical_validation_is_unsafe(canonical_kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id, stored = _prepare_gate_task(conn, canonical_kanban_home)
        handoff = json.loads(stored.read_text(encoding="utf-8"))
        evidence_path = Path(handoff["evidence_ref"])
        original_read = lane_roles._read_trusted_file
        original_load = lane_roles._load_change_gate_validator
        mutated = {"done": False, "reads": 0}

        def load_proxy(source):
            actual = original_load(source)

            class Proxy:
                ValidationError = actual.ValidationError

                @staticmethod
                def validate_artifact(*args, **kwargs):
                    result = actual.validate_artifact(*args, **kwargs)
                    mutated["done"] = True
                    return result

            lane_roles._validator_module_digest = source.sha256
            return Proxy()

        def changed_post_snapshot(root, path, *, limit):
            snapshot = original_read(root, path, limit=limit)
            if Path(path) == evidence_path:
                mutated["reads"] += 1
                if mutated["done"] and mutated["reads"] == 2:
                    changed = snapshot.data + b"\n# simulated evidence replacement\n"
                    return replace(snapshot, data=changed, sha256=hashlib.sha256(changed).hexdigest())
            return snapshot

        monkeypatch.setattr(lane_roles, "_load_change_gate_validator", load_proxy)
        monkeypatch.setattr(lane_roles, "_read_trusted_file", changed_post_snapshot)
        _claim_blocked_without_side_effects(conn, task_id, "CHANGE_GATE_ARTIFACT_UNSAFE")

    assert mutated["done"] is True
    assert mutated["reads"] == 2


def test_validator_source_mutation_during_canonical_validation_is_unsafe(canonical_kanban_home, monkeypatch):
    _assert_authority_input_mutation_is_unsafe(
        canonical_kanban_home, monkeypatch, "scripts/change_gate_validate.py"
    )


def _purity_snapshot(home):
    with kb.connect() as conn:
        tables = {}
        for table in ("tasks", "task_links", "task_events", "task_runs", "task_attachments"):
            tables[table] = [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    files = []
    attachments = home / "attachments"
    if attachments.exists():
        for path in sorted(p for p in attachments.rglob("*") if p.is_file()):
            files.append((str(path.relative_to(home)), path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()))
    workspace_files = []
    workspace_root = kb.workspaces_root()
    if workspace_root.exists():
        workspace_files = sorted(str(p.relative_to(workspace_root)) for p in workspace_root.rglob("*") if p.is_file())
    lock = kb.kanban_db_path().with_name(kb.kanban_db_path().name + ".dispatch.lock")
    return tables, files, workspace_files, lock.exists()


def test_dispatch_dry_run_is_board_wide_read_only(kanban_home, monkeypatch):
    with kb.connect() as conn:
        invalid_id = kb.create_task(
            conn,
            title="invalid implementation",
            body=json.dumps({"contract": {"lane": "implementation"}}),
            assignee="default",
        )
        normal_id = kb.create_task(conn, title="normal ready", body="legacy", assignee="default")
        parent_id = kb.create_task(conn, title="parent", body="legacy", assignee="default", initial_status="blocked")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent_id,))
        child_id = kb.create_task(conn, title="todo child", body="legacy", assignee="default", parents=(parent_id,))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child_id,))
        stale_id = kb.create_task(conn, title="stale running", body="legacy", assignee="default")
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'stale-lock', claim_expires = 0 WHERE id = ?",
            (stale_id,),
        )
        conn.commit()
        before = _purity_snapshot(kanban_home)
        monkeypatch.setattr(kb, "profile_exists", lambda _name: True, raising=False)
        result = kb.dispatch_once(conn, dry_run=True)
        after = _purity_snapshot(kanban_home)

    assert result.skipped_change_gate == [{"task_id": invalid_id, "reason_codes": ["CHANGE_GATE_METADATA_MISSING"]}]
    assert any(row[0] == normal_id for row in before[0]["tasks"])
    assert before == after


def test_gateway_gate_skip_telemetry_is_quiet_until_state_changes():
    state = {}
    skips = [{"task_id": "t1", "reason_codes": ["CHANGE_GATE_METADATA_MISSING"]}]
    assert _gate_skip_changed(state, "default", skips) is True
    assert _gate_skip_changed(state, "default", skips) is False
    assert _gate_skip_changed(
        state,
        "default",
        [{"task_id": "t1", "reason_codes": ["CHANGE_GATE_SCHEMA_INVALID"]}],
    ) is True
    assert _gate_skip_changed(state, "default", []) is False
    assert _gate_skip_changed(state, "default", skips) is True
