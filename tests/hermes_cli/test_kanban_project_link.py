"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(repo, name="Web App"):
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[str(repo)])
        return pdb.get_project(pc, pid)


def test_project_linked_task_gets_deterministic_worktree_and_branch(
    kanban_conn, tmp_path
):
    proj = _make_project(tmp_path / "webapp")
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn, tmp_path):
    proj = _make_project(tmp_path / "webapp")
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


def test_unknown_project_id_falls_back_gracefully(kanban_conn):
    # A project id that doesn't resolve must not crash task creation; the task
    # is created as-is (scratch) and project_id stays unset.
    tid = kb.create_task(kanban_conn, title="x", project_id="does-not-exist")
    task = kb.get_task(kanban_conn, tid)
    assert task.workspace_kind == "scratch"
    assert task.project_id is None


def _set_repo_writer_mode(tmp_path, mode):
    home = tmp_path / ".hermes"
    (home / "config.yaml").write_text(
        f"kanban:\n  repo_writer_mode: {mode}\n",
        encoding="utf-8",
    )
    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()


def test_red_repo_writer_mode_default_is_off_and_values_are_closed():
    """RED: the behavioral config is default-OFF and rejects open-ended modes."""
    from hermes_cli.config import DEFAULT_CONFIG, validate_config_structure

    marker = "repo_writer_mode_contract_missing"
    assert DEFAULT_CONFIG["kanban"]["repo_writer_mode"] == "off", marker
    assert not [
        issue
        for issue in validate_config_structure(
            {"kanban": {"repo_writer_mode": "single_writer"}}
        )
        if "repo_writer_mode" in issue.message
    ], marker
    issues = validate_config_structure(
        {"kanban": {"repo_writer_mode": "many_writers"}}
    )
    assert any(
        issue.severity == "error"
        and "off" in issue.message
        and "single_writer" in issue.message
        for issue in issues
    ), marker


def test_red_repo_writer_mode_off_preserves_omitted_project_worktree(
    kanban_conn, tmp_path
):
    """RED characterization: explicit/default off keeps today's worktree default."""
    marker = "repo_writer_mode_contract_missing"
    _set_repo_writer_mode(tmp_path, "off")
    proj = _make_project(tmp_path / "off-repo")

    tid = kb.create_task(kanban_conn, title="Off parity", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.workspace_kind == "worktree", marker
    assert task.workspace_path == os.path.join(
        proj.primary_path, ".worktrees", tid
    ), marker
    assert task.branch_name == f"{proj.slug}/{tid}-off-parity", marker


@pytest.mark.parametrize("explicit_none", [False, True])
def test_red_single_writer_project_omitted_or_none_uses_primary_checkout(
    kanban_conn, tmp_path, explicit_none
):
    """RED: omitted (including legacy explicit None) resolves to project dir."""
    marker = "repo_writer_mode_contract_missing"
    _set_repo_writer_mode(tmp_path, "single_writer")
    proj = _make_project(tmp_path / f"single-repo-{explicit_none}")
    kwargs = {"workspace_kind": None} if explicit_none else {}

    tid = kb.create_task(
        kanban_conn,
        title="Single writer",
        project_id=proj.slug,
        **kwargs,
    )
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id, marker
    assert task.workspace_kind == "dir", marker
    assert task.workspace_path == proj.primary_path, marker
    assert task.branch_name is None, marker
    assert not (tmp_path / ".hermes" / "kanban" / "repo-locks").exists(), marker


@pytest.mark.parametrize("explicit_none", [False, True])
def test_red_single_writer_omitted_kind_ignores_caller_path(
    kanban_conn, tmp_path, explicit_none
):
    """RED: omitted kind always binds project work to its primary checkout."""
    marker = "repo_writer_mode_call_path_missing"
    _set_repo_writer_mode(tmp_path, "single_writer")
    proj = _make_project(tmp_path / f"primary-{explicit_none}")
    assert proj is not None
    unrelated = tmp_path / f"unrelated-{explicit_none}"
    unrelated.mkdir()

    if explicit_none:
        tid = kb.create_task(
            kanban_conn,
            title="Reject unrelated checkout",
            project_id=proj.slug,
            workspace_kind=None,
            workspace_path=str(unrelated),
        )
    else:
        tid = kb.create_task(
            kanban_conn,
            title="Reject unrelated checkout",
            project_id=proj.slug,
            workspace_path=str(unrelated),
        )
    task = kb.get_task(kanban_conn, tid)
    assert task is not None

    assert task.project_id == proj.id, marker
    assert task.workspace_kind == "dir", marker
    assert task.workspace_path == proj.primary_path, marker
    assert task.workspace_path != str(unrelated), marker
    assert task.branch_name is None, marker


@pytest.mark.parametrize(
    ("workspace_kind", "path_kind"),
    [("scratch", "none"), ("dir", "caller"), ("worktree", "derived")],
)
def test_red_single_writer_explicit_workspace_kinds_remain_explicit(
    kanban_conn, tmp_path, workspace_kind, path_kind
):
    """RED: mode changes only omitted workspace, never the public enum choices."""
    marker = "repo_writer_mode_contract_missing"
    _set_repo_writer_mode(tmp_path, "single_writer")
    proj = _make_project(tmp_path / f"explicit-{workspace_kind}")
    assert proj is not None
    caller_path = tmp_path / "caller-selected-dir"
    workspace_path = str(caller_path) if path_kind == "caller" else None

    tid = kb.create_task(
        kanban_conn,
        title=f"Explicit {workspace_kind}",
        project_id=proj.slug,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
    )
    task = kb.get_task(kanban_conn, tid)
    assert task is not None

    assert task.workspace_kind == workspace_kind, marker
    if path_kind == "none":
        assert task.workspace_path is None, marker
        assert task.branch_name is None, marker
    elif path_kind == "caller":
        assert task.workspace_path == str(caller_path), marker
        assert task.branch_name is None, marker
    else:
        assert task.workspace_path == os.path.join(
            proj.primary_path, ".worktrees", tid
        ), marker
        assert task.branch_name == f"{proj.slug}/{tid}-explicit-worktree", marker


def test_red_single_writer_unknown_project_falls_back_to_scratch(
    kanban_conn, tmp_path
):
    marker = "repo_writer_mode_contract_missing"
    _set_repo_writer_mode(tmp_path, "single_writer")

    tid = kb.create_task(
        kanban_conn,
        title="Unknown project",
        project_id="does-not-exist",
    )
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None, marker
    assert task.workspace_kind == "scratch", marker
    assert task.workspace_path is None, marker
