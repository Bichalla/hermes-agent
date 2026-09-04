import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    ArtifactBinding,
    ArchitectureInventoryRecord,
    ChangeGateReason,
    EvidencePacket,
    ReleasePurpose,
    ReviewRoute,
    ReviewerClass,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    expected_release_statement,
    freeze_handoff,
)
from hermes_cli.change_gate_codec import (
    decode_artifact,
    encode_artifact,
)
from hermes_cli.change_gate_runtime import (
    EVIDENCE_ATTACHMENT_FILENAME,
    EVIDENCE_PACKET_SCHEMA,
    FROZEN_HANDOFF_SCHEMA,
    HANDOFF_ATTACHMENT_FILENAME,
)


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _worktree_paths(repo: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        line.removeprefix("worktree ")
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }


def _local_branches(repo: Path) -> set[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _write_policy(home: Path) -> None:
    path = home / "policies" / "change-gate"
    path.mkdir(parents=True)
    policy_path = path / "policy.yaml"
    policy_path.write_text(
        f"""
schema: change-gate-policy/v1
policy_version: change-gate-policy/v1
owner:
  canonical_owner: {policy_path}
  mutation_owner: Planner
risk_tiers:
  LOW:
    route_key: LOW
  NORMAL:
    route_key: NORMAL
  HIGH:
    route_key: HIGH
route_matrix:
  LOW:
    planner: {{role: PLANNER, profile: change-gate-high, model: gpt-5.6-sol, reasoning_effort: high}}
    executor: {{role: EXECUTOR, profile: change-gate-xhigh, provider: openai-codex, model: gpt-5.6-luna, reasoning_effort: xhigh}}
    reviewers:
      - {{role: REVIEWER, profile: change-gate-medium, model: gpt-5.6-sol, reasoning_effort: medium}}
  NORMAL:
    planner: {{role: PLANNER, profile: change-gate-high, model: gpt-5.6-sol, reasoning_effort: high}}
    executor: {{role: EXECUTOR, profile: change-gate-xhigh, provider: openai-codex, model: gpt-5.6-luna, reasoning_effort: xhigh}}
    reviewers:
      - {{role: REVIEWER, profile: change-gate-high, model: gpt-5.6-sol, reasoning_effort: high}}
  HIGH:
    planner: {{role: PLANNER, profile: change-gate-high, model: gpt-5.6-sol, reasoning_effort: high}}
    executor: {{role: EXECUTOR, profile: change-gate-xhigh, provider: openai-codex, model: gpt-5.6-luna, reasoning_effort: xhigh}}
    reviewers:
      - {{role: NORMAL_REVIEWER, profile: change-gate-high, model: gpt-5.6-sol, reasoning_effort: high}}
      - {{role: DEEP_REVIEWER, profile: change-gate-xhigh, model: gpt-5.6-sol, reasoning_effort: xhigh}}
""".lstrip(),
        encoding="utf-8",
    )


def _write_config(home: Path, inventory_root: Path) -> None:
    (home / "config.yaml").write_text(
        f"""
change_gate:
  enabled: true
  inventory_root: {inventory_root}
  planner_assignee: change-gate-high
""".lstrip(),
        encoding="utf-8",
    )


def _git_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "tests@example.invalid"], repo)
    _run(["git", "config", "user.name", "Tests"], repo)
    _run(["git", "remote", "add", "origin", "https://github.com/Bichalla/hermes-agent.git"], repo)
    (repo / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    artifact = repo / "gate-artifact.txt"
    artifact.write_text("candidate\n", encoding="utf-8")
    _run(["git", "add", ".gitignore", "gate-artifact.txt"], repo)
    _run(["git", "commit", "-m", "seed"], repo)
    binding = {
        "path": "gate-artifact.txt",
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "role": "produced",
    }
    return repo, binding


def _inventory(inventory_root: Path, inventory_id: str, risk: RiskLevel = RiskLevel.NORMAL) -> None:
    inventory_root.mkdir(parents=True, exist_ok=True)
    record = ArchitectureInventoryRecord(
        schema=ARCHITECTURE_INVENTORY_SCHEMA,
        inventory_id=inventory_id,
        capability="kanban-g2-handoff-test",
        owner="change-gate-contract-owner",
        consumers=("kanban_g2_handoff_test",),
        authority_contract="current-turn-frozen-handoff",
        activation_state="DEFAULT_OFF",
        source_paths=("tools/kanban_tools.py",),
        artifact_paths=("gate-artifact.txt",),
        risk=risk,
        blast_radius=("planner-to-executor",),
    )
    (inventory_root / f"{inventory_id}.json").write_bytes(encode_artifact(record))


def _clear_config_cache() -> None:
    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def _setup_env(monkeypatch, tmp_path: Path, risk: RiskLevel = RiskLevel.NORMAL):
    from hermes_cli import kanban_db as kb

    home = tmp_path / "home"
    home.mkdir()
    inventory_root = home / "inventory"
    _write_policy(home)
    _write_config(home, inventory_root)
    _inventory(inventory_root, "inv-normal", risk)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CONFIG", str(home / "config.yaml"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_PROFILE", "change-gate-high")
    monkeypatch.setattr(
        "agent.delegation_context.is_dispatcher_owned_worker_context",
        lambda: True,
    )
    monkeypatch.setattr(
        "agent.delegation_context.is_delegated_child_context",
        lambda: False,
    )
    _clear_config_cache()
    conn = kb.connect()
    return kb, conn


def _active_planner(
    conn,
    kb,
    monkeypatch,
    repo: Path,
    risk: RiskLevel = RiskLevel.NORMAL,
    *,
    claimer: str = "claim-g2",
    evidence_age_seconds: int = 1,
) -> str:
    from hermes_cli.change_gate_runtime import (
        load_runtime_policy,
        load_task_gate_artifacts,
        read_current_source_identity,
    )
    from tools.workflow_authority import _scoped_test_current_turn_user_authority

    planner_route = UpstreamRouteSelector(
        assignee="change-gate-high",
        model_override="gpt-5.6-sol",
        provider_override=None,
        reasoning_effort="high",
    )
    parent_id = kb.create_task(
        conn,
        title="plan",
        assignee=planner_route.assignee,
        workspace_kind="worktree",
        workspace_path=str(repo),
        model_override=planner_route.model_override,
        provider_override=planner_route.provider_override,
        reasoning_effort=planner_route.reasoning_effort,
    )
    source = read_current_source_identity(str(repo), "Bichalla/hermes-agent")
    assert type(source) is SourceIdentity
    artifact = repo / "gate-artifact.txt"
    produced = ArtifactBinding(
        path="gate-artifact.txt",
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        role="produced",
        git_oid=subprocess.run(
            ["git", "-C", str(repo), "hash-object", "gate-artifact.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )
    now = int(time.time())
    evidence = EvidencePacket(
        source=source,
        work=WorkIdentity(
            task_id=parent_id,
            run_id="planner-preclaim",
            work_id=f"planner-{parent_id}",
            operation="plan-g2-handoff",
            effect="SOURCE_CHANGE",
        ),
        risk=risk,
        route=RouteProjection(
            risk=risk,
            executor=planner_route,
            reviews=(
                (
                    ReviewRoute(ReviewerClass.NORMAL, planner_route),
                    ReviewRoute(ReviewerClass.DEEP, planner_route),
                )
                if risk is RiskLevel.HIGH
                else (ReviewRoute(ReviewerClass.REVIEWER, planner_route),)
            ),
        ),
        allowed_paths=("gate-artifact.txt",),
        required_inputs=(),
        produced_artifacts=(produced,),
        inventory_id="inv-normal",
        created_at_epoch=now - evidence_age_seconds,
        expires_at_epoch=now - evidence_age_seconds + 3600,
    )
    inventory_read = decode_artifact(
        (Path(os.environ["HERMES_HOME"]) / "inventory" / "inv-normal.json").read_bytes(),
        expected_schema=ARCHITECTURE_INVENTORY_SCHEMA,
    )
    assert inventory_read.ok
    assert type(inventory_read.value) is ArchitectureInventoryRecord
    handoff = freeze_handoff(
        evidence,
        inventory=inventory_read.value,
        inventory_consumer="kanban_g2_handoff_test",
        scope=("SOURCE_CHANGE",),
        forbidden_effects=("LIVE_SERVICE_MUTATION",),
    )
    kb.store_attachment_bytes(
        conn,
        parent_id,
        "change-gate-evidence.json",
        encode_artifact(evidence),
        content_type="application/json",
    )
    kb.store_attachment_bytes(
        conn,
        parent_id,
        "change-gate-frozen-handoff.json",
        encode_artifact(handoff),
        content_type="application/json",
    )
    kb.initialize_change_gate_runtime_schema(conn)
    load = load_task_gate_artifacts(
        conn,
        parent_id,
        policy=load_runtime_policy(),
        attachment_root=kb.task_attachments_dir(parent_id),
    )
    assert load.ok and load.artifacts is not None
    transition_anchor = kb.derive_change_gate_transition_anchor(
        conn,
        parent_id,
        purpose=ReleasePurpose.CLAIM,
        artifacts=load.artifacts,
    )
    assert transition_anchor is not None
    statement = expected_release_statement(
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id="g2-parent-session",
        turn_id=f"g2-parent-{parent_id}",
        platform_scope="manual",
    ):
        release = _issue_foreground_change_gate_release(parent_id, ReleasePurpose.CLAIM)
    assert release.ok, release.reason
    assert release.release_id is not None
    claimed = kb.claim_task(conn, parent_id, claimer=claimer)
    assert claimed is not None
    state = kb.change_gate_release_state(conn, release.release_id)
    assert state is not None and state["state"] == "CONSUMED"
    claimed = kb.get_task(conn, parent_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimer)
    return parent_id


def _args(binding: dict[str, str], repo: Path, risk: str = "NORMAL") -> dict:
    return {
        "title": "execute bounded source candidate",
        "body": "Use existing Change Gate contract only.",
        "risk": risk,
        "repository": "Bichalla/hermes-agent",
        "run_id": "preclaim-run",
        "work_id": "work-g2",
        "operation": "apply-source-candidate",
        "effect": "SOURCE_CHANGE",
        "allowed_paths": ["gate-artifact.txt"],
        "required_inputs": [],
        "produced_artifacts": [binding],
        "inventory_id": "inv-normal",
        "inventory_consumer": "kanban_g2_handoff_test",
        "scope": ["SOURCE_CHANGE"],
        "forbidden_effects": ["LIVE_SERVICE_MUTATION"],
        "evidence_ttl_seconds": 3600,
    }


def _legacy_abs_args(binding: dict[str, str], repo: Path, risk: str = "NORMAL") -> dict:
    args = _args(binding, repo, risk)
    args.pop("evidence_ttl_seconds")
    args["expires_at_epoch"] = int(time.time()) + 600
    return args


def _issue_foreground_change_gate_release(task_id: str, purpose: ReleasePurpose):
    from hermes_cli.change_gate_release import issue_change_gate_release

    saved_worker_env = {
        key: os.environ.get(key)
        for key in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
        )
    }
    for key in saved_worker_env:
        os.environ.pop(key, None)
    try:
        return issue_change_gate_release(
            task_id=task_id,
            purpose=purpose.value,
        )
    finally:
        for key, value in saved_worker_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _store_task_release(conn, kb, task_id: str, purpose: ReleasePurpose, suffix: str):
    from hermes_cli.change_gate_runtime import (
        load_runtime_policy,
        load_task_gate_artifacts,
        project_upstream_reviews,
    )
    from tools.workflow_authority import _scoped_test_current_turn_user_authority

    load = load_task_gate_artifacts(
        conn,
        task_id,
        policy=load_runtime_policy(),
        attachment_root=kb.task_attachments_dir(task_id),
    )
    assert load.ok and load.artifacts is not None
    artifacts = load.artifacts
    reviews = ()
    if purpose is ReleasePurpose.G4:
        projection = project_upstream_reviews(
            conn,
            task_id,
            handoff=artifacts.handoff,
        )
        assert projection.ok
        reviews = projection.reviews
    transition_anchor = kb.derive_change_gate_transition_anchor(
        conn,
        task_id,
        purpose=purpose,
        artifacts=artifacts,
        reviews=reviews,
    )
    assert transition_anchor is not None
    now = int(time.time())
    statement = expected_release_statement(
        purpose=purpose,
        handoff_sha256=artifacts.handoff.digest(),
    )
    with _scoped_test_current_turn_user_authority(
        statement,
        session_id=f"g2-{purpose.value.lower()}-session",
        turn_id=f"g2-{purpose.value.lower()}-{suffix}",
        platform_scope="manual",
    ):
        release = _issue_foreground_change_gate_release(task_id, purpose)
    assert release.ok, release.reason
    assert release.release_id is not None
    assert release.issued_at_epoch == now
    assert release.expires_at_epoch is not None
    assert release.expires_at_epoch - release.issued_at_epoch <= 600
    return release


def _review_and_complete(conn, kb, task_id: str, g4_suffix: str) -> None:
    task = kb.get_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    reviewed, reason = kb.request_review(
        conn,
        task_id,
        expected_run_id=task.current_run_id,
        with_reason=True,
    )
    assert reviewed, reason
    reviewer = kb.claim_review_task(conn, task_id)
    assert reviewer is not None and reviewer.current_run_id is not None
    assert kb.request_review(
        conn,
        task_id,
        expected_run_id=reviewer.current_run_id,
        change_gate_review={
            "reviewer_class": "REVIEWER",
            "verdict": "PASS",
            "finding_codes": [],
        },
    )
    _store_task_release(conn, kb, task_id, ReleasePurpose.G4, g4_suffix)
    assert kb.complete_task(conn, task_id, result="verified")


def _g2_material_state(conn, repo: Path) -> dict[str, object]:
    tables = (
        "tasks",
        "task_runs",
        "task_links",
        "task_attachments",
        "task_events",
        "task_comments",
        "change_gate_releases",
    )
    attachments = conn.execute(
        "SELECT stored_path FROM task_attachments ORDER BY task_id, filename"
    ).fetchall()
    return {
        "tables": {
            table: tuple(
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            for table in tables
        },
        "attachment_files": tuple(
            (
                row["stored_path"],
                hashlib.sha256(Path(row["stored_path"]).read_bytes()).hexdigest(),
            )
            for row in attachments
        ),
        "inventory_files": tuple(
            (
                str(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in sorted(
                (Path(os.environ["HERMES_HOME"]) / "inventory").glob("*.json")
            )
        ),
        "worktrees": _worktree_paths(repo),
        "branches": _local_branches(repo),
        "child_worktree_paths": tuple(
            sorted(str(path) for path in (repo / ".worktrees").glob("t_*") if path.exists())
        ),
    }


def _assert_no_g2_material_delta(conn, before: dict[str, object], repo: Path) -> None:
    assert _g2_material_state(conn, repo) == before


def _apply_additional_provenance_corruption(
    case: str,
    conn,
    kb,
    parent_id: str,
) -> None:
    task = kb.get_task(conn, parent_id)
    assert task is not None and task.current_run_id is not None
    run_id = int(task.current_run_id)
    release = conn.execute(
        "SELECT * FROM change_gate_releases "
        "WHERE task_id = ? AND purpose = 'CLAIM'",
        (parent_id,),
    ).fetchone()
    assert release is not None and release["consumed_event_id"] is not None
    event_id = int(release["consumed_event_id"])
    event = conn.execute(
        "SELECT * FROM task_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert event is not None

    if case == "wrong_event_task":
        conn.execute(
            "UPDATE task_events SET task_id = 't_wrong' WHERE id = ?",
            (event_id,),
        )
    elif case == "wrong_event_run":
        conn.execute(
            "UPDATE task_events SET run_id = ? WHERE id = ?",
            (run_id + 1, event_id),
        )
    elif case == "wrong_consumed_event":
        other_event_id = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND id != ? "
            "ORDER BY id LIMIT 1",
            (parent_id, event_id),
        ).fetchone()[0]
        conn.execute(
            "UPDATE change_gate_releases SET consumed_event_id = ? "
            "WHERE release_id = ?",
            (other_event_id, release["release_id"]),
        )
    elif case == "wrong_expiry_type":
        payload = json.loads(event["payload"])
        payload["expires"] = str(payload["expires"])
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), event_id),
        )
    elif case == "duplicate_consumed_release":
        columns = tuple(release.keys())
        values = dict(zip(columns, tuple(release), strict=True))
        values["release_id"] = "cgr_" + "b" * 64
        values["artifact_sha256"] = "b" * 64
        conn.execute(
            f"INSERT INTO change_gate_releases ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
    elif case == "release_drift":
        conn.execute(
            "UPDATE change_gate_releases SET state = 'REVOKED', revoked_at = 1, "
            "revoked_reason = 'test drift' WHERE release_id = ?",
            (release["release_id"],),
        )
    elif case == "artifact_drift":
        attachment = conn.execute(
            "SELECT stored_path FROM task_attachments "
            "WHERE task_id = ? AND filename = ?",
            (parent_id, EVIDENCE_ATTACHMENT_FILENAME),
        ).fetchone()
        assert attachment is not None
        Path(attachment["stored_path"]).write_bytes(b"tampered")
    elif case == "inventory_drift":
        inventory = Path(os.environ["HERMES_HOME"]) / "inventory" / "inv-normal.json"
        inventory.write_bytes(b"tampered")
    elif case == "route_drift":
        conn.execute(
            "UPDATE tasks SET model_override = 'drift-model' WHERE id = ?",
            (parent_id,),
        )
    elif case == "ended_run":
        conn.execute(
            "UPDATE task_runs SET ended_at = ? WHERE id = ?",
            (int(time.time()), run_id),
        )
    elif case == "reclaimed_run":
        conn.execute(
            "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
            "ended_at = ? WHERE id = ?",
            (int(time.time()), run_id),
        )
    elif case == "superseded_run":
        replacement = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, started_at) VALUES (?, ?, 'running', ?, ?, ?)",
            (
                parent_id,
                task.assignee,
                task.claim_lock,
                task.claim_expires,
                int(time.time()),
            ),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (int(replacement.lastrowid), parent_id),
        )
    elif case == "current_claim_lock_drift":
        conn.execute(
            "UPDATE tasks SET claim_lock = 'other-lock' WHERE id = ?",
            (parent_id,),
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock = 'other-lock' WHERE id = ?",
            (run_id,),
        )
    else:
        raise AssertionError(f"unknown corruption case: {case}")
    conn.commit()


def test_kanban_g2_handoff_creates_child_and_frozen_artifacts(monkeypatch, tmp_path):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    try:
        worker_provenance = kb._read_worker_runtime_provenance()
        assert worker_provenance.source_commit
        assert worker_provenance.source_tree
        assert worker_provenance.g2_tool_name == "kanban_g2_handoff"
        assert worker_provenance.g2_toolset == "kanban"

        post_claim = kb.evaluate_change_gate_claim_runtime(conn, parent_id)
        assert post_claim.applicable
        assert post_claim.result.reason is ChangeGateReason.RELEASE_TRANSITION_STALE
        replay_preimage = _g2_material_state(conn, repo)
        assert kb.claim_task(conn, parent_id, claimer="claim-replay") is None
        _assert_no_g2_material_delta(conn, replay_preimage, repo)
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (parent_id,),
        ).fetchone()[0] == 1

        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        child = kb.get_task(conn, result["task_id"])
        assert child is not None
        assert child.status == "ready"
        assert child.assignee == "change-gate-xhigh"
        assert child.model_override == "gpt-5.6-luna"
        assert child.provider_override == "openai-codex"
        assert child.reasoning_effort == "xhigh"
        assert kb.parent_ids(conn, child.id) == [parent_id]
        assert Path(child.workspace_path).name == child.id
        assert Path(child.workspace_path).is_dir()

        attachments = {a.filename: a for a in kb.list_attachments(conn, child.id)}
        assert set(attachments) == {
            EVIDENCE_ATTACHMENT_FILENAME,
            HANDOFF_ATTACHMENT_FILENAME,
        }
        evidence_data = Path(attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path).read_bytes()
        handoff_data = Path(attachments[HANDOFF_ATTACHMENT_FILENAME].stored_path).read_bytes()
        evidence = decode_artifact(evidence_data, expected_schema=EVIDENCE_PACKET_SCHEMA)
        handoff = decode_artifact(handoff_data, expected_schema=FROZEN_HANDOFF_SCHEMA)
        assert evidence.ok and handoff.ok
        assert evidence.value.work.task_id == child.id
        assert evidence.value.route.executor.assignee == "change-gate-xhigh"
        assert handoff.value.route == evidence.value.route
        assert handoff.value.inventory_consumer == "kanban_g2_handoff_test"
        from hermes_cli.change_gate_runtime import load_runtime_policy, load_task_gate_artifacts

        load = load_task_gate_artifacts(
            conn,
            child.id,
            policy=load_runtime_policy(),
            attachment_root=kb.task_attachments_dir(child.id),
        )
        assert load.ok

        duplicate_preimage = _g2_material_state(conn, repo)
        duplicate = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "active dispatcher worker run is required" in duplicate["error"]
        _assert_no_g2_material_delta(conn, duplicate_preimage, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_uses_relative_ttl_from_child_creation_time(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    stamped_now = int(time.time()) + 100
    monkeypatch.setattr(time, "time", lambda: stamped_now)
    try:
        args = _args(binding, repo)
        args["evidence_ttl_seconds"] = 3600
        result = json.loads(_handle_g2_handoff(args))
        assert result["ok"] is True

        attachments = {a.filename: a for a in kb.list_attachments(conn, result["task_id"])}
        evidence_data = Path(attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path).read_bytes()
        assert "evidence_ttl_seconds" not in json.loads(evidence_data)
        evidence = decode_artifact(evidence_data, expected_schema=EVIDENCE_PACKET_SCHEMA)
        assert evidence.ok
        assert evidence.value.created_at_epoch == stamped_now
        assert evidence.value.expires_at_epoch == stamped_now + 3600
    finally:
        conn.close()


def test_kanban_g2_handoff_parent_near_expiry_gets_fresh_relative_hour(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    fake_now = int(time.time()) + 100
    monkeypatch.setattr(time, "time", lambda: fake_now)
    parent_id = _active_planner(
        conn,
        kb,
        monkeypatch,
        repo,
        evidence_age_seconds=3590,
    )
    try:
        parent_attachments = {
            attachment.filename: attachment
            for attachment in kb.list_attachments(conn, parent_id)
        }
        parent_read = decode_artifact(
            Path(
                parent_attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path
            ).read_bytes(),
            expected_schema=EVIDENCE_PACKET_SCHEMA,
        )
        assert parent_read.ok
        assert parent_read.value.expires_at_epoch - fake_now == 10

        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        child_attachments = {
            attachment.filename: attachment
            for attachment in kb.list_attachments(conn, result["task_id"])
        }
        child_read = decode_artifact(
            Path(
                child_attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path
            ).read_bytes(),
            expected_schema=EVIDENCE_PACKET_SCHEMA,
        )
        assert child_read.ok
        assert child_read.value.created_at_epoch == fake_now
        assert child_read.value.expires_at_epoch == fake_now + 3600
    finally:
        conn.close()


def test_kanban_g2_handoff_accepts_legacy_absolute_expiry(monkeypatch, tmp_path):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    fake_now = int(time.time()) + 100
    monkeypatch.setattr(time, "time", lambda: fake_now)
    _active_planner(conn, kb, monkeypatch, repo)
    try:
        result = json.loads(_handle_g2_handoff(_legacy_abs_args(binding, repo)))
        assert result["ok"] is True
        child = kb.get_task(conn, result["task_id"])
        assert child is not None and child.status == "ready"
        attachments = {
            attachment.filename: attachment
            for attachment in kb.list_attachments(conn, result["task_id"])
        }
        evidence = decode_artifact(
            Path(attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path).read_bytes(),
            expected_schema=EVIDENCE_PACKET_SCHEMA,
        )
        assert evidence.ok
        assert evidence.value.created_at_epoch == fake_now
        assert evidence.value.expires_at_epoch == fake_now + 600
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("case", "mutate", "message"),
    [
        (
            "conflict",
            lambda args: args.update({"expires_at_epoch": int(time.time()) + 300}),
            "exactly one of expires_at_epoch or evidence_ttl_seconds is required",
        ),
        (
            "missing",
            lambda args: args.pop("evidence_ttl_seconds"),
            "exactly one of expires_at_epoch or evidence_ttl_seconds is required",
        ),
        (
            "bool",
            lambda args: args.update({"evidence_ttl_seconds": True}),
            "evidence_ttl_seconds must be an integer",
        ),
        (
            "negative",
            lambda args: args.update({"evidence_ttl_seconds": -1}),
            "evidence_ttl_seconds must be between 1 and 3600",
        ),
        (
            "too-small",
            lambda args: args.update({"evidence_ttl_seconds": 0}),
            "evidence_ttl_seconds must be between 1 and 3600",
        ),
        (
            "too-large",
            lambda args: args.update({"evidence_ttl_seconds": 3601}),
            "evidence_ttl_seconds must be between 1 and 3600",
        ),
        (
            "absolute-bool",
            lambda args: (
                args.pop("evidence_ttl_seconds"),
                args.update({"expires_at_epoch": True}),
            ),
            "expires_at_epoch must be an integer",
        ),
        (
            "absolute-too-large",
            lambda args: (
                args.pop("evidence_ttl_seconds"),
                args.update({"expires_at_epoch": int(time.time()) + 601}),
            ),
            "expires_at_epoch must be within 600 seconds",
        ),
    ],
)
def test_kanban_g2_handoff_rejects_bad_relative_timing_without_material_delta(
    monkeypatch,
    tmp_path,
    case,
    mutate,
    message,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    validation_now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: validation_now)
    args = _args(binding, repo)
    mutate(args)
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(args))
        assert "error" in result, case
        assert message in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_schema_and_runtime_timing_validation_agree():
    jsonschema = pytest.importorskip("jsonschema")
    from hermes_cli import kanban_db as kb
    from tools.schema_sanitizer import sanitize_tool_schemas
    from tools.kanban_tools import KANBAN_G2_HANDOFF_SCHEMA

    published = sanitize_tool_schemas(
        [
            {
                "type": "function",
                "function": KANBAN_G2_HANDOFF_SCHEMA,
            }
        ]
    )[0]["function"]
    schema = published["parameters"]
    assert "oneOf" not in schema
    assert "not" not in schema
    assert schema["properties"]["evidence_ttl_seconds"]["maximum"] == 3600
    assert schema["properties"]["expires_at_epoch"]["type"] == "integer"
    validator = jsonschema.Draft202012Validator(schema)
    base = {
        "title": "execute bounded source candidate",
        "risk": "NORMAL",
        "repository": "Bichalla/hermes-agent",
        "run_id": "preclaim-run",
        "work_id": "work-g2",
        "operation": "apply-source-candidate",
        "effect": "SOURCE_CHANGE",
        "allowed_paths": ["gate-artifact.txt"],
        "required_inputs": [],
        "produced_artifacts": [{"path": "gate-artifact.txt", "sha256": "a" * 64, "role": "produced"}],
        "inventory_id": "inv-normal",
        "inventory_consumer": "kanban_g2_handoff_test",
        "scope": ["SOURCE_CHANGE"],
        "forbidden_effects": ["LIVE_SERVICE_MUTATION"],
    }

    valid_relative = {**base, "evidence_ttl_seconds": 3600}
    assert list(validator.iter_errors(valid_relative)) == []
    assert kb._resolve_g2_evidence_timing(
        expires_at_epoch=None,
        evidence_ttl_seconds=3600,
    ) == ("relative", 3600)

    valid_absolute = {**base, "expires_at_epoch": int(time.time()) + 300}
    assert list(validator.iter_errors(valid_absolute)) == []
    assert kb._resolve_g2_evidence_timing(
        expires_at_epoch=valid_absolute["expires_at_epoch"],
        evidence_ttl_seconds=None,
    ) == ("absolute", valid_absolute["expires_at_epoch"])

    both = {**valid_relative, "expires_at_epoch": int(time.time()) + 300}
    assert list(validator.iter_errors(both)) == []
    with pytest.raises(ValueError, match="exactly one"):
        kb._resolve_g2_evidence_timing(
            expires_at_epoch=both["expires_at_epoch"],
            evidence_ttl_seconds=both["evidence_ttl_seconds"],
        )

    missing = dict(base)
    assert list(validator.iter_errors(missing)) == []
    with pytest.raises(ValueError, match="exactly one"):
        kb._resolve_g2_evidence_timing(
            expires_at_epoch=None,
            evidence_ttl_seconds=None,
        )

    too_large = {**base, "evidence_ttl_seconds": 3601}
    assert list(validator.iter_errors(too_large))
    with pytest.raises(ValueError, match="between 1 and 3600"):
        kb._resolve_g2_evidence_timing(
            expires_at_epoch=None,
            evidence_ttl_seconds=3601,
        )


def test_g2_terminal_planner_to_child_full_lifecycle_has_two_claims_one_g4(
    monkeypatch,
    tmp_path,
):
    from hermes_cli.change_gate_runtime import (
        load_runtime_policy,
        load_task_gate_artifacts,
    )
    from tools.kanban_tools import _handle_g2_handoff, _handle_heartbeat

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    fake_clock = {"now": int(time.time()) + 1_000}
    monkeypatch.setattr(time, "time", lambda: fake_clock["now"])
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    try:
        parent_attachments = {
            attachment.filename: attachment
            for attachment in kb.list_attachments(conn, parent_id)
        }
        parent_evidence = decode_artifact(
            Path(
                parent_attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path
            ).read_bytes(),
            expected_schema=EVIDENCE_PACKET_SCHEMA,
        )
        assert parent_evidence.ok
        assert (
            parent_evidence.value.expires_at_epoch
            - parent_evidence.value.created_at_epoch
            == 3600
        )
        assert json.loads(_handle_heartbeat({"task_id": parent_id}))["ok"] is True
        fake_clock["now"] += 100
        handoff = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert handoff["ok"] is True
        child_id = handoff["task_id"]
        child_attachments = {
            attachment.filename: attachment
            for attachment in kb.list_attachments(conn, child_id)
        }
        child_evidence = decode_artifact(
            Path(
                child_attachments[EVIDENCE_ATTACHMENT_FILENAME].stored_path
            ).read_bytes(),
            expected_schema=EVIDENCE_PACKET_SCHEMA,
        )
        assert child_evidence.ok
        assert child_evidence.value.created_at_epoch == fake_clock["now"]
        assert child_evidence.value.expires_at_epoch == fake_clock["now"] + 3600
        assert (
            child_evidence.value.expires_at_epoch
            > parent_evidence.value.expires_at_epoch
        )
        parent = kb.get_task(conn, parent_id)
        assert parent.status == "done"
        assert parent.current_run_id is None
        assert parent.claim_lock is None
        assert kb.get_task(conn, child_id).status == "ready"
        parent_run = kb.list_runs(conn, parent_id)[-1]
        assert parent_run.status == "done"
        assert parent_run.outcome == "g2_handoff"
        assert parent_run.claim_lock is None
        parent_claim = conn.execute(
            "SELECT release_id, consumed_event_id, authority_receipt_sha256, "
            "handoff_sha256 FROM change_gate_releases "
            "WHERE task_id = ? AND purpose = 'CLAIM'",
            (parent_id,),
        ).fetchone()
        assert parent_claim is not None
        expected_terminal_payload = {
            "child_task_id": child_id,
            "claim_release_id": parent_claim["release_id"],
            "claim_consumed_event_id": parent_claim["consumed_event_id"],
            "claim_authority_receipt_sha256": parent_claim[
                "authority_receipt_sha256"
            ],
            "planner_handoff_sha256": parent_claim["handoff_sha256"],
        }
        assert parent_run.metadata == expected_terminal_payload
        terminal_event = [
            event
            for event in kb.list_events(conn, parent_id)
            if event.kind == "g2_handoff_completed"
        ]
        assert len(terminal_event) == 1
        assert terminal_event[0].run_id == parent_run.id
        assert terminal_event[0].payload == expected_terminal_payload
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?",
            (child_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?",
            (parent_id,),
        ).fetchone()[0] == 2
        for task_id in (parent_id, child_id):
            artifacts = load_task_gate_artifacts(
                conn,
                task_id,
                policy=load_runtime_policy(),
                attachment_root=kb.task_attachments_dir(task_id),
            )
            assert artifacts.ok and artifacts.artifacts is not None
            assert artifacts.artifacts.handoff.claim_release_required is True
            assert artifacts.artifacts.handoff.g4_release_required is True

        missing_child_claim = _g2_material_state(conn, repo)
        assert kb.claim_task(conn, child_id, claimer="parent-claim-reuse") is None
        _assert_no_g2_material_delta(conn, missing_child_claim, repo)

        fake_clock["now"] += 100
        child_claim = _store_task_release(
            conn,
            kb,
            child_id,
            ReleasePurpose.CLAIM,
            "4",
        )
        child = kb.claim_task(conn, child_id, claimer="child-claim")
        assert child is not None
        fake_clock["now"] += 100
        _review_and_complete(conn, kb, child_id, "5")

        releases = conn.execute(
            "SELECT task_id, purpose, state, issued_at, expires_at FROM change_gate_releases "
            "ORDER BY rowid"
        ).fetchall()
        assert [(row["task_id"], row["purpose"], row["state"]) for row in releases] == [
            (parent_id, "CLAIM", "CONSUMED"),
            (child_id, "CLAIM", "CONSUMED"),
            (child_id, "G4", "CONSUMED"),
        ]
        assert [row["issued_at"] for row in releases] == [
            fake_clock["now"] - 300,
            fake_clock["now"] - 100,
            fake_clock["now"],
        ]
        assert all(row["expires_at"] - row["issued_at"] <= 600 for row in releases)
        assert child_claim.task_id == child_id
        assert kb.get_task(conn, parent_id).status == "done"
        assert kb.get_task(conn, child_id).status == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND profile = ?",
            (child_id, "change-gate-high"),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_g2_child_ready_is_not_observable_before_parent_terminal_commit(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_db as kb_module
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    real_unblock = kb_module.unblock_task
    observed: dict[str, object] = {}

    def observe_before_commit(writer, child_id, **kwargs):
        assert real_unblock(writer, child_id, **kwargs)
        observed["writer_parent"] = kb.get_task(writer, parent_id).status
        observed["writer_child"] = kb.get_task(writer, child_id).status
        db_path = Path(os.environ["HERMES_KANBAN_DB"])
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as reader:
            observed["reader_parent"] = reader.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (parent_id,),
            ).fetchone()[0]
            observed["reader_child"] = reader.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (child_id,),
            ).fetchone()
        return True

    monkeypatch.setattr(kb_module, "unblock_task", observe_before_commit)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        child_id = result["task_id"]
        assert observed == {
            "writer_parent": "done",
            "writer_child": "ready",
            "reader_parent": "running",
            "reader_child": None,
        }
        assert kb.get_task(conn, parent_id).status == "done"
        assert kb.get_task(conn, child_id).status == "ready"
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_consumed_claim_from_another_run(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    conn.execute(
        "UPDATE change_gate_releases SET consumed_run_id = ? "
        "WHERE task_id = ? AND purpose = 'CLAIM'",
        (int(parent.current_run_id) + 1, parent_id),
    )
    conn.commit()
    try:
        preimage = _g2_material_state(conn, repo)
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        _assert_no_g2_material_delta(conn, preimage, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_accepts_consumed_claim_after_heartbeat_extends_lease(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff, _handle_heartbeat

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    claimed = conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
        (parent_id, parent.current_run_id),
    ).fetchone()
    claimed_payload = json.loads(claimed["payload"])
    monkeypatch.setattr(kb.time, "time", lambda: int(claimed["created_at"]) + 3)

    heartbeat = json.loads(_handle_heartbeat({"task_id": parent_id}))
    assert heartbeat["ok"] is True
    extended_task = kb.get_task(conn, parent_id)
    extended_run = conn.execute(
        "SELECT claim_expires FROM task_runs WHERE id = ?",
        (parent.current_run_id,),
    ).fetchone()
    assert extended_task.claim_expires == extended_run["claim_expires"]
    assert extended_task.claim_expires > claimed_payload["expires"]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND kind = 'heartbeat'",
        (parent_id,),
    ).fetchone()[0] == 1

    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 1
    finally:
        conn.close()


def test_kanban_g2_handoff_accepts_consumed_claim_after_live_pid_lease_extension(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    host = kb._claimer_id().split(":", 1)[0]
    parent_id = _active_planner(
        conn,
        kb,
        monkeypatch,
        repo,
        claimer=f"{host}:live-pid-test",
    )
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    claimed = conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
        (parent_id, parent.current_run_id),
    ).fetchone()
    claimed_payload = json.loads(claimed["payload"])
    extension_now = int(claimed["created_at"]) + 5
    monkeypatch.setattr(kb.time, "time", lambda: extension_now)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    kb._set_worker_pid(conn, parent_id, 424242)
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
        (extension_now - 1, extension_now, parent_id),
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ?, last_heartbeat_at = ? WHERE id = ?",
        (extension_now - 1, extension_now, int(parent.current_run_id)),
    )
    conn.commit()
    assert kb.release_stale_claims(conn) == 0
    deferred_task = kb.get_task(conn, parent_id)
    deferred_run = conn.execute(
        "SELECT claim_expires FROM task_runs WHERE id = ?",
        (parent.current_run_id,),
    ).fetchone()
    assert deferred_task.claim_expires == deferred_run["claim_expires"]
    assert deferred_task.claim_expires > claimed_payload["expires"]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id = ? AND kind = 'claim_extended'",
        (parent_id,),
    ).fetchone()[0] == 1

    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 1
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_task_run_lease_disagreement_without_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    conn.execute(
        "UPDATE task_runs SET claim_expires = claim_expires + 1 WHERE id = ?",
        (parent.current_run_id,),
    )
    conn.commit()
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_malformed_claimed_payload_without_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    conn.execute(
        "UPDATE task_events SET payload = ? "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
        (json.dumps({"lock": "claim-g2", "run_id": parent.current_run_id}), parent_id, parent.current_run_id),
    )
    conn.commit()
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_wrong_claim_lock_event_without_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    conn.execute(
        "UPDATE task_events SET payload = ? "
        "WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
        (
            json.dumps(
                {
                    "lock": "other-lock",
                    "expires": parent.claim_expires,
                    "run_id": parent.current_run_id,
                }
            ),
            parent_id,
            parent.current_run_id,
        ),
    )
    conn.commit()
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_multiple_claimed_events_without_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    kb._append_event(
        conn,
        parent_id,
        "claimed",
        {"lock": "claim-g2", "expires": parent.claim_expires, "run_id": parent.current_run_id},
        run_id=parent.current_run_id,
    )
    conn.commit()
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_rejects_terminal_planner_run_without_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    assert parent is not None and parent.current_run_id is not None
    conn.execute(
        "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
        "claim_expires = NULL, current_run_id = NULL WHERE id = ?",
        (parent_id,),
    )
    conn.execute(
        "UPDATE task_runs SET status = 'blocked', outcome = 'blocked', "
        "ended_at = ?, claim_lock = NULL, claim_expires = NULL WHERE id = ?",
        (int(time.time()), parent.current_run_id),
    )
    conn.commit()
    before = _g2_material_state(conn, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "active dispatcher worker run is required" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "case",
    (
        "wrong_event_task",
        "wrong_event_run",
        "wrong_consumed_event",
        "wrong_expiry_type",
        "duplicate_consumed_release",
        "release_drift",
        "artifact_drift",
        "inventory_drift",
        "route_drift",
        "ended_run",
        "reclaimed_run",
        "superseded_run",
        "current_claim_lock_drift",
    ),
)
def test_kanban_g2_handoff_rejects_additional_provenance_drift_without_delta(
    monkeypatch,
    tmp_path,
    case,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    try:
        _apply_additional_provenance_corruption(case, conn, kb, parent_id)
        preimage = _g2_material_state(conn, repo)
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "error" in result
        _assert_no_g2_material_delta(conn, preimage, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_projects_exact_policy_table(monkeypatch, tmp_path):
    from hermes_cli.change_gate_runtime import load_runtime_policy, load_task_gate_artifacts
    from tools.kanban_tools import _handle_g2_handoff

    expected_reviewers = {
        "LOW": [("REVIEWER", "change-gate-medium", "gpt-5.6-sol", None, "medium")],
        "NORMAL": [("REVIEWER", "change-gate-high", "gpt-5.6-sol", None, "high")],
        "HIGH": [
            ("NORMAL", "change-gate-high", "gpt-5.6-sol", None, "high"),
            ("DEEP", "change-gate-xhigh", "gpt-5.6-sol", None, "xhigh"),
        ],
    }
    for risk_name in ("LOW", "NORMAL", "HIGH"):
        case_dir = tmp_path / risk_name.lower()
        case_dir.mkdir()
        kb, conn = _setup_env(monkeypatch, case_dir, RiskLevel(risk_name))
        repo, binding = _git_repo(case_dir)
        _active_planner(conn, kb, monkeypatch, repo, RiskLevel(risk_name))
        try:
            result = json.loads(_handle_g2_handoff(_args(binding, repo, risk_name)))
            child_id = result["task_id"]
            child = kb.get_task(conn, child_id)
            assert child.assignee == "change-gate-xhigh"
            assert child.model_override == "gpt-5.6-luna"
            assert child.provider_override == "openai-codex"
            assert child.reasoning_effort == "xhigh"
            load = load_task_gate_artifacts(
                conn,
                child_id,
                policy=load_runtime_policy(),
                attachment_root=kb.task_attachments_dir(child_id),
            )
            assert load.ok
            got = [
                (
                    review.reviewer_class.value,
                    review.selector.assignee,
                    review.selector.model_override,
                    review.selector.provider_override,
                    review.selector.reasoning_effort,
                )
                for review in load.artifacts.handoff.route.reviews
            ]
            assert got == expected_reviewers[risk_name]
        finally:
            conn.close()


def test_kanban_g2_handoff_fails_closed_for_non_planner(monkeypatch, tmp_path):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    monkeypatch.setenv("HERMES_PROFILE", "change-gate-xhigh")
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "canonical planner profile" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_kanban_g2_handoff_rolls_back_child_link_artifacts_and_worktree(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_db as kb_module
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    real_store = kb_module.store_attachment_bytes

    def boom_after_first_attachment(*args, **kwargs):
        out = real_store(*args, **kwargs)
        raise RuntimeError("injected attachment failure")

    monkeypatch.setattr(kb_module, "store_attachment_bytes", boom_after_first_attachment)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "injected attachment failure" in result["error"]
        rows = conn.execute("SELECT id FROM tasks ORDER BY created_at").fetchall()
        assert len(rows) == 1
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 2
        assert not any((repo / ".worktrees").glob("t_*"))
        assert not any("/.worktrees/t_" in path for path in _worktree_paths(repo))
        assert not any(branch.startswith("wt/t_") for branch in _local_branches(repo))
        attachments_root = kb.task_attachments_dir("not-used").parent
        assert (
            {path.name for path in attachments_root.glob("t_*")} == {parent_id}
            if attachments_root.exists()
            else False
        )
    finally:
        conn.close()


def test_kanban_g2_handoff_rolls_back_parent_terminal_and_all_outputs(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_db as kb_module
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    before = _g2_material_state(conn, repo)
    monkeypatch.setattr(kb_module, "unblock_task", lambda *_args, **_kwargs: False)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "child handoff unblock failed" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
        parent = kb.get_task(conn, parent_id)
        assert parent is not None and parent.status == "running"
        assert parent.current_run_id is not None
        assert parent.claim_lock == "claim-g2"
    finally:
        conn.close()


def test_kanban_g2_handoff_wrong_worker_task_has_zero_material_delta(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    before = _g2_material_state(conn, repo)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_wrong_task")
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "active dispatcher worker run is required" in result["error"]
        _assert_no_g2_material_delta(conn, before, repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_rolls_back_after_workspace_resolver_failure(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_db as kb_module
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)

    def fail_resolve(*args, **kwargs):
        raise RuntimeError("injected workspace failure")

    monkeypatch.setattr(kb_module, "_resolve_worktree_workspace", fail_resolve)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "injected workspace failure" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 2
        assert not any("/.worktrees/t_" in path for path in _worktree_paths(repo))
    finally:
        conn.close()


def test_kanban_g2_handoff_preserves_preexisting_branch_on_collision(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    child_id = "t_preexisting_branch"
    branch_name = f"wt/{child_id}"
    _run(["git", "branch", branch_name], repo)
    monkeypatch.setattr(kb, "_new_task_id", lambda: child_id)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "already exists" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert branch_name in _local_branches(repo)
        assert not (repo / ".worktrees" / child_id).exists()
    finally:
        conn.close()


def test_kanban_g2_handoff_rolls_back_project_named_branch(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_db as kb_module
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    parent_id = _active_planner(conn, kb, monkeypatch, repo)
    parent = kb.get_task(conn, parent_id)
    parent_worktree, parent_branch = kb._resolve_worktree_workspace(parent)
    conn.execute(
        "UPDATE tasks SET workspace_path = ?, branch_name = ?, project_id = ? WHERE id = ?",
        (str(parent_worktree), parent_branch, "project-test", parent_id),
    )
    child_id = "t_project_child"
    monkeypatch.setattr(kb, "_new_task_id", lambda: child_id)
    real_store = kb_module.store_attachment_bytes

    def boom_after_first_attachment(*args, **kwargs):
        out = real_store(*args, **kwargs)
        raise RuntimeError("injected project attachment failure")

    monkeypatch.setattr(kb_module, "store_attachment_bytes", boom_after_first_attachment)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "injected project attachment failure" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert not (repo / ".worktrees" / child_id).exists()
        assert not any(
            branch.rsplit("/", 1)[-1] == child_id
            or branch.rsplit("/", 1)[-1].startswith(f"{child_id}-")
            for branch in _local_branches(repo)
        )
        assert parent_branch in _local_branches(repo)
    finally:
        conn.close()


def test_kanban_g2_handoff_preserves_preexisting_attachment_directory(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    child_id = "t_preexisting_attachments"
    monkeypatch.setattr(kb, "_new_task_id", lambda: child_id)
    attachment_dir = kb.task_attachments_dir(child_id)
    attachment_dir.mkdir(parents=True)
    sentinel = attachment_dir / "keep.txt"
    sentinel.write_text("pre-existing\n", encoding="utf-8")
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "attachment directory already exists" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert sentinel.read_text(encoding="utf-8") == "pre-existing\n"
        assert not (repo / ".worktrees" / child_id).exists()
        assert not any(
            branch.rsplit("/", 1)[-1] == child_id
            for branch in _local_branches(repo)
        )
    finally:
        conn.close()


def test_kanban_g2_handoff_fails_closed_on_undeclared_inventory_consumer(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    args = _args(binding, repo)
    args["inventory_consumer"] = "undeclared-consumer"
    try:
        result = json.loads(_handle_g2_handoff(args))
        assert "inventory_consumer" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert not any((repo / ".worktrees").glob("t_*"))
    finally:
        conn.close()


def test_kanban_g2_handoff_fails_closed_on_inventory_filename_identity_mismatch(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    inventory_root = tmp_path / "home" / "inventory"
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    _inventory(inventory_root, "different-inventory")
    (inventory_root / "inv-normal.json").write_bytes(
        (inventory_root / "different-inventory.json").read_bytes()
    )
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "could not be loaded" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert not any((repo / ".worktrees").glob("t_*"))
    finally:
        conn.close()


def test_kanban_g2_handoff_fails_closed_on_non_exact_policy_role(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    policy_path = tmp_path / "home" / "policies" / "change-gate" / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "role: REVIEWER",
            "role: NORMAL",
        ),
        encoding="utf-8",
    )
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "unsupported reviewer role" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_kanban_g2_handoff_fails_closed_on_policy_owner_mismatch(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    policy_path = tmp_path / "home" / "policies" / "change-gate" / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            f"canonical_owner: {policy_path}",
            "canonical_owner: /tmp/not-the-policy.yaml",
        ),
        encoding="utf-8",
    )
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "canonical change-gate policy is invalid" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_kanban_g2_handoff_fails_closed_on_duplicate_policy_key(
    monkeypatch,
    tmp_path,
):
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    policy_path = tmp_path / "home" / "policies" / "change-gate" / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "    reviewers:\n",
            "    executor: {role: EXECUTOR, profile: duplicate, model: gpt-5.6-luna, reasoning_effort: xhigh}\n    reviewers:\n",
            1,
        ),
        encoding="utf-8",
    )
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "duplicate yaml key" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        conn.close()


def test_change_gate_evaluation_rejects_post_success_selector_drift(
    monkeypatch,
    tmp_path,
):
    from hermes_cli.change_gate import (
        ChangeGateAdapter,
        ChangeGateRequest,
        GateDecision,
        ReleasePurpose,
        StaticArchitectureInventoryReader,
    )
    from hermes_cli.change_gate_runtime import load_runtime_policy, load_task_gate_artifacts
    from tools.kanban_tools import _handle_g2_handoff

    kb, conn = _setup_env(monkeypatch, tmp_path)
    repo, binding = _git_repo(tmp_path)
    _active_planner(conn, kb, monkeypatch, repo)
    try:
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        child_id = result["task_id"]
        conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            ("drift-model", child_id),
        )
        load = load_task_gate_artifacts(
            conn,
            child_id,
            policy=load_runtime_policy(),
            attachment_root=kb.task_attachments_dir(child_id),
        )
        assert load.ok
        assert load.artifacts is not None
        artifacts = load.artifacts
        request = ChangeGateRequest(
            evidence=artifacts.evidence,
            frozen_handoff=artifacts.handoff,
            release_receipt=None,
            source=artifacts.actual_source,
            work=artifacts.evidence.work,
            requested_paths=artifacts.workspace_observation.changed_paths,
            observed_inputs=artifacts.evidence.required_inputs,
            observed_outputs=artifacts.evidence.produced_artifacts,
            reviews=(),
            purpose=ReleasePurpose.CLAIM,
            durable_release=None,
        )
        evaluation = ChangeGateAdapter(
            enabled=True,
            inventory_reader=StaticArchitectureInventoryReader((artifacts.inventory,)),
            clock=lambda: artifacts.evidence.created_at_epoch,
        ).evaluate(
            request,
            actual_task_id=artifacts.evidence.work.task_id,
            actual_route=artifacts.actual_route,
        )
        assert evaluation.decision is GateDecision.DENY
        assert evaluation.reason is ChangeGateReason.ROUTE_PROJECTION_MISMATCH
    finally:
        conn.close()
