import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from hermes_cli.change_gate import (
    ARCHITECTURE_INVENTORY_SCHEMA,
    ArtifactBinding,
    ArchitectureInventoryRecord,
    ChangeGateReason,
    DurableReleaseArtifact,
    EvidencePacket,
    HumanReleaseReceipt,
    ReleasePurpose,
    ReviewRoute,
    ReviewerClass,
    RiskLevel,
    RouteProjection,
    SourceIdentity,
    UpstreamRouteSelector,
    WorkIdentity,
    canonical_sha256,
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
    artifact = repo / "gate-artifact.txt"
    artifact.write_text("candidate\n", encoding="utf-8")
    _run(["git", "add", "gate-artifact.txt"], repo)
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
) -> str:
    from hermes_cli.change_gate_runtime import (
        load_runtime_policy,
        load_task_gate_artifacts,
        read_current_source_identity,
    )
    from tools.workflow_authority import fingerprint_user_action

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
        created_at_epoch=now - 1,
        expires_at_epoch=now + 599,
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
    release = DurableReleaseArtifact(
        release_id="cgr_" + "a" * 64,
        purpose=ReleasePurpose.CLAIM,
        handoff_sha256=handoff.digest(),
        evidence_sha256=evidence.digest(),
        inventory_sha256=handoff.inventory_sha256,
        artifact_set_sha256=canonical_sha256(
            {
                "allowed_paths": handoff.allowed_paths,
                "required_inputs": handoff.required_inputs,
                "produced_artifacts": handoff.produced_artifacts,
            }
        ),
        route_sha256=canonical_sha256(handoff.route),
        task_id=parent_id,
        work_id=evidence.work.work_id,
        source=source,
        authority_receipt=HumanReleaseReceipt(
            purpose=ReleasePurpose.CLAIM,
            handoff_sha256=handoff.digest(),
            action_fingerprint=fingerprint_user_action(statement),
            turn_id_sha256="1" * 64,
            session_scope_sha256="2" * 64,
            platform_scope_sha256="3" * 64,
            user_message_index=1,
            source_role="user",
        ),
        transition_anchor=transition_anchor,
        issued_at_epoch=now,
        expires_at_epoch=now + 300,
    )
    kb._store_change_gate_release(conn, release)
    claimed = kb.claim_task(conn, parent_id, claimer="claim-g2")
    assert claimed is not None
    state = kb.change_gate_release_state(conn, release.release_id)
    assert state is not None and state["state"] == "CONSUMED"
    claimed = kb.get_task(conn, parent_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claim-g2")
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
        "expires_at_epoch": int(time.time()) + 300,
    }


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
        assert kb.claim_task(conn, parent_id, claimer="claim-replay") is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (parent_id,),
        ).fetchone()[0] == 1

        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert result["ok"] is True
        child = kb.get_task(conn, result["task_id"])
        assert child is not None
        assert child.status == "todo"
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

        duplicate = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "already issued a G2 handoff" in duplicate["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 1
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
        result = json.loads(_handle_g2_handoff(_args(binding, repo)))
        assert "exact consumed CLAIM provenance" in result["error"]
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert not any((repo / ".worktrees").glob("t_*"))
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
