"""Task 3A: dispatcher-level repository single-writer accounting."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.repo_write_lock import repo_identity
from scripts import smoke_kanban_repo_single_writer_no_live as smoke


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: "single_writer")
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        yield conn


@pytest.fixture
def projectless_board(tmp_path, monkeypatch):
    """Keep the board DB outside HERMES_HOME for a literal full-tree snapshot."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: "single_writer")
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    db_path = tmp_path / "board.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        yield conn, home


def _git(*args: str, cwd: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


@pytest.fixture
def repositories(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    _git("config", "user.name", "Hermes Test", cwd=primary)
    _git("config", "user.email", "hermes@example.invalid", cwd=primary)
    (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "seed.txt", cwd=primary)
    _git("commit", "-qm", "seed", cwd=primary)

    worktree = tmp_path / "linked-worktree"
    _git("worktree", "add", "-qb", "linked-test", str(worktree), cwd=primary)
    alias = tmp_path / "primary-alias"
    alias.symlink_to(primary, target_is_directory=True)

    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", cwd=other)
    _git("config", "user.name", "Hermes Test", cwd=other)
    _git("config", "user.email", "hermes@example.invalid", cwd=other)
    (other / "seed.txt").write_text("other\n", encoding="utf-8")
    _git("add", "seed.txt", cwd=other)
    _git("commit", "-qm", "seed", cwd=other)

    assert repo_identity(primary) == repo_identity(worktree) == repo_identity(alias)
    assert repo_identity(primary) != repo_identity(other)
    return primary, worktree, alias, other


def _task(
    conn,
    title: str,
    path: Path | None,
    *,
    status: str = "ready",
    priority: int = 0,
    created_at: int | None = None,
    workspace_kind: str = "dir",
    stored_identity: str | None = None,
) -> str:
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="worker",
        workspace_kind=workspace_kind,
        workspace_path=str(path) if path is not None else None,
        priority=priority,
        repo_identity=stored_identity,
    )
    updates = ["status = ?"]
    values: list[object] = [status]
    if created_at is not None:
        updates.append("created_at = ?")
        values.append(created_at)
    values.append(task_id)
    conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
    conn.commit()
    return task_id


def _capture_spawn(order: list[tuple[str, list[str] | None]], *, fail_first: bool = False):
    calls = 0

    def spawn(task, _workspace, board=None):
        nonlocal calls
        calls += 1
        order.append((task.id, task.skills))
        if fail_first and calls == 1:
            raise RuntimeError("synthetic spawn failure")
        return 9000 + calls

    return spawn


@contextlib.contextmanager
def _held_repo_lock(repository: Path):
    """Hold the real repo lock in a separate process under the temp Hermes home."""
    code = """
import sys
from hermes_cli.repo_write_lock import RepoWriteLock

with RepoWriteLock(sys.argv[1], blocking=False):
    print("held", flush=True)
    sys.stdin.read(1)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(repository)],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == "held\n"
        yield
    finally:
        if proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr


def _tree_snapshot(root: Path, *, excluded: set[Path] | None = None):
    """Capture names, bytes, type, mode and timestamps without following links."""
    excluded = excluded or set()
    paths = [root, *root.rglob("*")]
    snapshot = {}
    for path in sorted(paths, key=os.fspath):
        if path in excluded:
            continue
        info = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        link = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        snapshot[str(path.relative_to(root))] = (
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            payload,
            link,
        )
    return snapshot


def _sqlite_artifacts(db_path: Path) -> set[Path]:
    return {
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    }


def _link_project_without_store(conn, task_id: str) -> None:
    """Mark a task project-linked without opening the per-profile Project DB."""
    conn.execute(
        "UPDATE tasks SET project_id = ?, repo_identity = NULL WHERE id = ?",
        ("p_no_projects_store", task_id),
    )
    conn.commit()


def _assert_no_projects_store(home: Path) -> None:
    assert list(home.glob("projects.db*")) == [], (
        "repo_single_writer_queue_contract_missing: dry-run created projects.db"
    )


@pytest.mark.parametrize(
    "planned_workspace", [False, True], ids=["existing", "planned"],
)
def test_repo_single_writer_queue_contract_missing_dry_run_project_workspace_is_pure(
    projectless_board, repositories, planned_workspace,
):
    board, home = projectless_board
    primary, _, _, _ = repositories
    workspace = primary / ".worktrees" / "planned" if planned_workspace else primary
    task_id = _task(board, "project workspace projection", workspace)
    _link_project_without_store(board, task_id)
    _assert_no_projects_store(home)
    before = _tree_snapshot(home)

    result = kb.dispatch_once(board, dry_run=True)

    task = kb.get_task(board, task_id)
    assert [item[0] for item in result.spawned] == [task_id]
    assert result.skipped_repo_busy == []
    assert task is not None
    assert task.repo_identity is None
    _assert_no_projects_store(home)
    assert _tree_snapshot(home) == before


@pytest.mark.parametrize(
    "planned_workspace", [False, True], ids=["existing", "planned"],
)
def test_repo_single_writer_queue_contract_missing_dry_run_running_legacy_null_seed_is_pure(
    projectless_board, repositories, planned_workspace,
):
    board, home = projectless_board
    primary, _, _, _ = repositories
    running_workspace = (
        primary / ".worktrees" / "legacy-running"
        if planned_workspace
        else primary
    )
    running = _task(
        board, "project legacy running", running_workspace, status="running",
    )
    candidate = _task(board, "project ready candidate", primary)
    _link_project_without_store(board, running)
    _link_project_without_store(board, candidate)
    _assert_no_projects_store(home)
    before = _tree_snapshot(home)

    result = kb.dispatch_once(board, dry_run=True)

    identity = repo_identity(primary)
    running_after = kb.get_task(board, running)
    candidate_after = kb.get_task(board, candidate)
    assert result.spawned == []
    assert result.skipped_repo_busy == [(candidate, identity)]
    assert running_after is not None
    assert candidate_after is not None
    assert running_after.repo_identity is None
    assert candidate_after.repo_identity is None
    _assert_no_projects_store(home)
    assert _tree_snapshot(home) == before


def test_repo_single_writer_queue_contract_missing_dry_run_project_without_workspace_fails_closed_pure(
    projectless_board, tmp_path,
):
    board, home = projectless_board
    missing_workspace = tmp_path / "not-a-repository" / "planned-worktree"
    task_id = _task(board, "project without usable workspace", missing_workspace)
    _link_project_without_store(board, task_id)
    _assert_no_projects_store(home)
    before = _tree_snapshot(home)
    spawn_calls = []

    result = kb.dispatch_once(
        board,
        dry_run=True,
        spawn_fn=lambda *_args, **_kwargs: spawn_calls.append(True),
    )

    task = kb.get_task(board, task_id)
    assert result.spawned == []
    assert result.skipped_repo_busy == []
    assert spawn_calls == []
    assert task is not None
    assert task.status == "ready"
    assert task.claim_lock is None
    assert task.repo_identity is None
    assert not missing_workspace.exists()
    _assert_no_projects_store(home)
    assert _tree_snapshot(home) == before


def test_repo_single_writer_queue_contract_missing_dry_run_bypasses_board_lock(
    board, repositories, monkeypatch,
):
    primary, _, _, _ = repositories
    task_id = _task(board, "pure dry projection", primary)
    db_path = Path(board.execute("PRAGMA database_list").fetchone()["file"])
    home = Path(os.environ["HERMES_HOME"])
    from hermes_cli import repo_write_lock as rwl

    dispatch_leaf = Path(f"{db_path}.dispatch.lock")
    repo_root = rwl._lock_root()
    assert not dispatch_leaf.exists()
    assert not repo_root.exists()
    before = _tree_snapshot(home, excluded=_sqlite_artifacts(db_path))

    def forbidden_tick_lock(*_args, **_kwargs):
        raise AssertionError("repo_single_writer_queue_contract_missing")

    monkeypatch.setattr(kb, "_dispatch_tick_lock", forbidden_tick_lock)

    result = kb.dispatch_once(board, dry_run=True, board="default")

    assert [item[0] for item in result.spawned] == [task_id]
    assert _tree_snapshot(home, excluded=_sqlite_artifacts(db_path)) == before
    assert not dispatch_leaf.exists()
    assert not repo_root.exists()


def test_repo_single_writer_queue_contract_missing_dry_run_observes_external_holder(
    board, repositories,
):
    primary, _, _, _ = repositories
    task_id = _task(board, "externally held", primary)
    identity = repo_identity(primary)
    home = Path(os.environ["HERMES_HOME"])
    db_path = Path(board.execute("PRAGMA database_list").fetchone()["file"])
    from hermes_cli import repo_write_lock as rwl

    with _held_repo_lock(primary):
        leaf = rwl._lock_root() / f"{identity}.lock"
        assert leaf.is_file()
        before_leaf = (leaf.read_bytes(), leaf.stat())
        before_tree = _tree_snapshot(home, excluded=_sqlite_artifacts(db_path))

        result = kb.dispatch_once(board, dry_run=True)

        assert result.skipped_repo_busy == [(task_id, identity)]
        after_leaf = (leaf.read_bytes(), leaf.stat())
        assert after_leaf == before_leaf
        assert _tree_snapshot(home, excluded=_sqlite_artifacts(db_path)) == before_tree


def test_repo_single_writer_queue_contract_missing_dry_run_missing_leaf_stays_missing(
    board, repositories,
):
    primary, _, _, _ = repositories
    task_id = _task(board, "no existing leaf", primary)
    identity = repo_identity(primary)
    home = Path(os.environ["HERMES_HOME"])
    db_path = Path(board.execute("PRAGMA database_list").fetchone()["file"])
    from hermes_cli import repo_write_lock as rwl

    root = rwl._lock_root()
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    leaf = root / f"{identity}.lock"
    assert not leaf.exists()
    before = _tree_snapshot(home, excluded=_sqlite_artifacts(db_path))

    result = kb.dispatch_once(board, dry_run=True)

    assert [item[0] for item in result.spawned] == [task_id]
    assert result.skipped_repo_busy == []
    assert not leaf.exists()
    assert _tree_snapshot(home, excluded=_sqlite_artifacts(db_path)) == before


@pytest.mark.parametrize("insecure_object", ["root-symlink", "root-mode", "leaf-symlink"])
def test_repo_single_writer_queue_contract_missing_dry_run_insecure_lock_fails_closed(
    board, repositories, tmp_path, insecure_object,
):
    primary, _, _, _ = repositories
    task_id = _task(board, f"insecure {insecure_object}", primary)
    identity = repo_identity(primary)
    from hermes_cli import repo_write_lock as rwl

    root = rwl._lock_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / f"outside-{insecure_object}"
    outside.mkdir(mode=0o700)
    outside_leaf = outside / f"{identity}.lock"
    if insecure_object == "root-symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if insecure_object == "root-mode":
            root.chmod(0o755)
        else:
            target = outside / "target"
            target.write_bytes(b"do not touch\n")
            target.chmod(0o600)
            (root / f"{identity}.lock").symlink_to(target)
    before_root = _tree_snapshot(root.parent)
    before_outside = _tree_snapshot(outside)

    result = kb.dispatch_once(board, dry_run=True)

    assert result.spawned == []
    assert result.skipped_repo_busy == [(task_id, identity)]
    assert _tree_snapshot(root.parent) == before_root
    assert _tree_snapshot(outside) == before_outside
    assert not outside_leaf.exists()


def test_repo_single_writer_queue_contract_missing_ready_ready(board, repositories):
    primary, _, _, _ = repositories
    first = _task(board, "first", primary, priority=2)
    second = _task(board, "second", primary, priority=1)
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    identity = repo_identity(primary)
    assert [item[0] for item in order] == [first]
    assert result.skipped_repo_busy == [(second, identity)]
    assert kb.get_task(board, first).repo_identity == identity
    assert kb.get_task(board, second).repo_identity == identity
    events = board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'repo_busy'",
        (second,),
    ).fetchone()[0]
    assert events == 1


@pytest.mark.parametrize(
    ("candidate_status", "claim_name"),
    [("ready", "claim_task"), ("review", "claim_review_task")],
    ids=["ready", "review"],
)
def test_repo_single_writer_queue_contract_missing_precheck_claim_race(
    board, repositories, monkeypatch, candidate_status, claim_name,
):
    primary, _, _, _ = repositories
    candidate = _task(board, "candidate", primary, status=candidate_status, priority=2)
    competitor = _task(board, "competitor", primary, status="done", priority=1)
    spawn_calls: list[tuple[str, list[str] | None]] = []
    original_claim = getattr(kb, claim_name)
    ordinary_ready_claim = kb.claim_task
    injected = False

    def inject_competing_claim(conn, task_id, **kwargs):
        nonlocal injected
        assert task_id == candidate
        assert injected is False
        injected = True
        # Deterministic boundary injection: the dispatcher's preliminary repo
        # check has completed, but its candidate claim transaction has not yet
        # begun. A second SQLite connection now makes a same-repo writer run.
        with kb.connect() as competing_conn:
            competing_conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'done'",
                (competitor,),
            )
            competing_conn.commit()
            assert ordinary_ready_claim(
                competing_conn, competitor, claimer="race-competitor"
            ) is not None
        return original_claim(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, claim_name, inject_competing_claim)

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(spawn_calls))

    identity = repo_identity(primary)
    untouched = kb.get_task(board, candidate)
    assert injected is True
    assert untouched is not None
    assert untouched.status == candidate_status
    assert untouched.claim_lock is None
    assert untouched.consecutive_failures == 0
    assert spawn_calls == []
    assert result.spawned == []
    assert result.auto_blocked == []
    assert result.skipped_repo_busy == [(candidate, identity)]
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'repo_busy'",
        (candidate,),
    ).fetchone()[0] == 1


def test_repo_single_writer_queue_contract_missing_cli_repo_busy_shape(
    board, monkeypatch, capsys,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import config as hermes_config

    identity = "a" * 64
    dispatch_result = kb.DispatchResult(
        skipped_repo_busy=[("t_busy_one", identity), ("t_busy_two", identity)],
    )
    monkeypatch.setattr(kb, "dispatch_once", lambda *_args, **_kwargs: dispatch_result)
    monkeypatch.setattr(hermes_config, "load_config", lambda: {})

    args = argparse.Namespace(
        dry_run=False,
        max=None,
        failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
        json=True,
    )
    assert kb_cli._cmd_dispatch(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "reclaimed": 0,
        "crashed": [],
        "timed_out": [],
        "stale": [],
        "auto_blocked": [],
        "promoted": 0,
        "spawned": [],
        "skipped_unassigned": [],
        "skipped_nonspawnable": [],
        "skipped_per_profile_capped": [],
        "auto_assigned_default": [],
        "skipped_change_gate": [],
        "skipped_repo_busy": [
            {"task_id": "t_busy_one", "repo_identity": identity},
            {"task_id": "t_busy_two", "repo_identity": identity},
        ],
    }

    args.json = False
    assert kb_cli._cmd_dispatch(args) == 0
    human = capsys.readouterr().out
    assert "Skipped (repo busy):\n  - t_busy_one\n  - t_busy_two\n" in human
    assert identity not in human


def test_repo_single_writer_queue_contract_missing_bounded_repo_busy_events(
    board, repositories,
):
    primary, _, _, _ = repositories
    busy = _task(board, "busy", primary)

    with _held_repo_lock(primary):
        for _ in range(5):
            result = kb.dispatch_once(board, spawn_fn=lambda *_args, **_kwargs: 42)
            assert result.skipped_repo_busy == [(busy, repo_identity(primary))]

        dry = _task(board, "dry-busy", primary, priority=-1)
        before_dry = board.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'repo_busy'"
        ).fetchone()[0]
        for _ in range(5):
            dry_result = kb.dispatch_once(board, dry_run=True)
            assert (dry, repo_identity(primary)) in dry_result.skipped_repo_busy

    events = board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'repo_busy'",
        (busy,),
    ).fetchone()[0]
    assert events == 1
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'repo_busy'"
    ).fetchone()[0] == before_dry


def test_repo_single_writer_queue_contract_missing_health_exclusions_are_precise(
    board,
):
    from gateway.kanban_watchers import _board_has_spawnable_work

    busy_ready = _task(board, "busy-ready", None, workspace_kind="scratch")
    only_busy = kb.DispatchResult(skipped_repo_busy=[(busy_ready, "a" * 64)])
    assert _board_has_spawnable_work(kb, board, only_busy) is False

    nonbusy_review = _task(
        board, "nonbusy-review", None, status="review", workspace_kind="scratch",
    )
    assert _board_has_spawnable_work(kb, board, only_busy) is True
    board.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (nonbusy_review,))
    board.commit()
    assert _board_has_spawnable_work(kb, board, only_busy) is False

    other_db = kb.kanban_db_path(board="other-health")
    kb._INITIALIZED_PATHS.discard(str(other_db.resolve()))
    kb.init_db(db_path=other_db)
    with kb.connect(db_path=other_db) as other_board:
        other_ready = _task(
            other_board, "other-ready", None, workspace_kind="scratch",
        )
        other_result = kb.DispatchResult()
        # Board A's exclusion must not leak into board B's result.
        assert any((
            _board_has_spawnable_work(kb, board, only_busy),
            _board_has_spawnable_work(kb, other_board, other_result),
        )) is True
        assert _board_has_spawnable_work(
            kb,
            other_board,
            kb.DispatchResult(skipped_repo_busy=[(other_ready, "b" * 64)]),
        ) is False

    # Keep the ready+review mixed case explicit: excluding both is normal
    # deferred work, excluding only the busy ready task leaves review spawnable.
    board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (nonbusy_review,))
    board.commit()
    both_busy = kb.DispatchResult(
        skipped_repo_busy=[(busy_ready, "a" * 64), (nonbusy_review, "b" * 64)],
    )
    assert _board_has_spawnable_work(kb, board, both_busy) is False


def test_repo_single_writer_queue_contract_missing_gateway_log_is_count_only():
    from gateway.kanban_watchers import _repo_busy_log_summary

    raw_path = "/private/repositories/never-log-this"
    result = kb.DispatchResult(
        skipped_repo_busy=[("t_busy", raw_path), ("t_busy_2", raw_path)],
    )
    summary = _repo_busy_log_summary("board-a", result)

    assert summary == ("board-a", 2)
    assert raw_path not in repr(summary)
    assert _repo_busy_log_summary("board-a", kb.DispatchResult()) is None


def test_repo_single_writer_queue_contract_missing_manual_claim_and_process_lock(
    board, repositories, monkeypatch, capsys,
):
    primary, _, _, other = repositories
    from hermes_cli import kanban as kb_cli

    _task(board, "running", primary, status="running")
    same_db = _task(board, "same-db", primary)
    scratch = _task(board, "scratch", None, workspace_kind="scratch")

    denied = argparse.Namespace(task_id=same_db, ttl=None)
    assert kb_cli._cmd_claim(denied) != 0
    same_db_error = capsys.readouterr().err
    assert "repo_busy" in same_db_error
    assert str(primary) not in same_db_error
    blocked = kb.get_task(board, same_db)
    assert blocked is not None
    assert blocked.status == "ready"
    assert blocked.claim_lock is None

    allowed_scratch = argparse.Namespace(task_id=scratch, ttl=None)
    assert kb_cli._cmd_claim(allowed_scratch) == 0
    capsys.readouterr()
    claimed_scratch = kb.get_task(board, scratch)
    assert claimed_scratch is not None
    assert claimed_scratch.status == "running"
    assert claimed_scratch.workspace_path is not None
    assert Path(claimed_scratch.workspace_path).is_dir()

    unavailable_path = primary.parent / "private-raw-repo-path"
    uncertain = _task(board, "uncertain", unavailable_path)
    assert kb_cli._cmd_claim(
        argparse.Namespace(task_id=uncertain, ttl=None)
    ) != 0
    uncertainty_error = capsys.readouterr().err
    assert "repo_busy" in uncertainty_error
    assert str(unavailable_path) not in uncertainty_error
    assert not unavailable_path.exists()
    uncertain_after = kb.get_task(board, uncertain)
    assert uncertain_after is not None
    assert uncertain_after.status == "ready"
    assert uncertain_after.claim_lock is None

    other_db = kb.kanban_db_path(board="other")
    kb._INITIALIZED_PATHS.discard(str(other_db.resolve()))
    kb.init_db(db_path=other_db)
    with kb.connect(db_path=other_db) as other_board:
        locked_task = _task(other_board, "cross-board", primary, priority=2)
        independent = _task(other_board, "independent", other, priority=1)

        manual_locked = _task(
            board, "external-manual", primary, workspace_kind="worktree",
        )
        with _held_repo_lock(primary):
            assert kb_cli._cmd_claim(
                argparse.Namespace(task_id=manual_locked, ttl=None)
            ) != 0
            external_error = capsys.readouterr().err
            assert "repo_busy" in external_error
            assert str(primary) not in external_error

            spawned: list[tuple[str, list[str] | None]] = []
            result = kb.dispatch_once(
                other_board,
                board="other",
                spawn_fn=_capture_spawn(spawned),
            )

        identity = repo_identity(primary)
        assert result.skipped_repo_busy == [(locked_task, identity)]
        assert [task_id for task_id, _skills in spawned] == [independent]
        untouched = kb.get_task(other_board, locked_task)
        assert untouched is not None
        assert untouched.status == "ready"
        assert untouched.claim_lock is None
        assert untouched.consecutive_failures == 0

    manual_after = kb.get_task(board, manual_locked)
    assert manual_after is not None
    assert manual_after.status == "ready"
    assert manual_after.claim_lock is None
    assert not (primary / ".worktrees" / manual_locked).exists()

    # Mode-off parity: neither DB accounting nor the external process probe
    # changes the historical manual-claim behavior.
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: "off")
    bypass = _task(board, "mode-off", primary)
    with _held_repo_lock(primary):
        assert kb_cli._cmd_claim(
            argparse.Namespace(task_id=bypass, ttl=None)
        ) == 0
    capsys.readouterr()
    assert kb.get_task(board, bypass).status == "running"


@pytest.mark.parametrize(
    ("ready_priority", "review_priority", "expected_first"),
    [(2, 1, "ready"), (1, 2, "review"), (1, 1, "ready")],
    ids=["ready-higher", "review-higher", "exact-tie-ready-first"],
)
def test_ready_review_share_one_deterministic_queue(
    board, repositories, ready_priority, review_priority, expected_first,
):
    primary, _, _, _ = repositories
    ready = _task(
        board, "ready", primary, priority=ready_priority, created_at=100,
    )
    review = _task(
        board, "review", primary, status="review",
        priority=review_priority, created_at=100,
    )
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    first = ready if expected_first == "ready" else review
    second = review if expected_first == "ready" else ready
    assert [item[0] for item in order] == [first]
    assert result.skipped_repo_busy == [(second, repo_identity(primary))]
    if first == review:
        assert order[0][1] == ["sdlc-review"]


def test_review_review_same_repo_serializes(board, repositories):
    primary, _, _, _ = repositories
    first = _task(board, "review-one", primary, status="review", priority=2)
    second = _task(board, "review-two", primary, status="review", priority=1)
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    assert [item[0] for item in order] == [first]
    assert order[0][1] == ["sdlc-review"]
    assert result.skipped_repo_busy == [(second, repo_identity(primary))]


def test_running_legacy_null_identity_blocks_and_is_persisted(board, repositories):
    primary, _, _, _ = repositories
    running = _task(board, "legacy-running", primary, status="running")
    candidate = _task(board, "candidate", primary)

    result = kb.dispatch_once(board, spawn_fn=lambda *_args, **_kwargs: 42)

    identity = repo_identity(primary)
    assert result.spawned == []
    assert result.skipped_repo_busy == [(candidate, identity)]
    assert kb.get_task(board, running).repo_identity == identity


def test_legacy_project_task_uses_existing_primary_when_worktree_is_absent(
    board, repositories, monkeypatch,
):
    primary, _, _, _ = repositories
    from hermes_cli import projects_db as pdb

    with pdb.connect_closing() as project_conn:
        project_id = pdb.create_project(
            project_conn, name="Primary Resolver", folders=[str(primary)],
        )
    running = kb.create_task(
        board,
        title="legacy project worktree",
        assignee="worker",
        project_id=project_id,
        workspace_kind="worktree",
    )
    task = kb.get_task(board, running)
    assert task is not None
    assert task.workspace_path is not None
    assert not Path(task.workspace_path).exists()
    # A shared dispatcher can run under a different profile and therefore
    # cannot assume access to the creator's projects.db.
    monkeypatch.setattr(kb, "_task_repo_primary_path", lambda _task: None)
    board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (running,))
    board.commit()

    kb.dispatch_once(board, spawn_fn=lambda *_args, **_kwargs: 42)

    resolved = kb.get_task(board, running)
    assert resolved is not None
    assert resolved.repo_identity == repo_identity(primary)


def test_primary_worktree_and_symlink_alias_share_common_dir(
    board, repositories,
):
    primary, worktree, alias, _ = repositories
    _task(board, "running-primary", primary, status="running")
    linked = _task(board, "linked", worktree, priority=2)
    symlinked = _task(board, "symlinked", alias, priority=1)

    result = kb.dispatch_once(board, spawn_fn=lambda *_args, **_kwargs: 42)

    identity = repo_identity(primary)
    assert result.spawned == []
    assert result.skipped_repo_busy == [(linked, identity), (symlinked, identity)]


def test_different_repositories_can_spawn_together(board, repositories):
    primary, _, _, other = repositories
    first = _task(board, "primary", primary, priority=2)
    second = _task(board, "other", other, priority=1)
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    assert [item[0] for item in order] == [first, second]
    assert result.skipped_repo_busy == []


def test_scratch_tasks_are_unaccounted_even_with_repo_path(board, repositories):
    primary, _, _, _ = repositories
    first = _task(board, "scratch-one", primary, priority=2, workspace_kind="scratch")
    second = _task(board, "scratch-two", primary, priority=1, workspace_kind="scratch")
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    assert [item[0] for item in order] == [first, second]
    assert result.skipped_repo_busy == []
    assert kb.get_task(board, first).repo_identity is None
    assert kb.get_task(board, second).repo_identity is None


def test_spawn_failure_does_not_poison_identity_for_later_candidate(
    board, repositories,
):
    primary, _, _, _ = repositories
    failed = _task(board, "fails", primary, priority=2)
    later = _task(board, "later", primary, priority=1)
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(
        board, spawn_fn=_capture_spawn(order, fail_first=True), failure_limit=2,
    )

    assert [item[0] for item in order] == [failed, later]
    assert [item[0] for item in result.spawned] == [later]
    assert result.skipped_repo_busy == []
    assert kb.get_task(board, failed).status == "ready"


@pytest.mark.parametrize("error_type", [TypeError, ValueError], ids=["type", "value"])
@pytest.mark.parametrize("signature", ["board", "legacy"], ids=["board", "legacy"])
def test_spawn_body_signature_errors_invoke_exactly_once(
    board, repositories, error_type, signature,
):
    primary, _, _, _ = repositories
    failed = _task(board, "body-error", primary, priority=2)
    later = _task(board, "later", primary, priority=1)
    repo_single_writer_queue_contract_missing = {
        "failed_calls": 0,
        "successful_calls": 0,
    }

    def invoke(task):
        if task.id == failed:
            repo_single_writer_queue_contract_missing["failed_calls"] += 1
            raise error_type("synthetic spawn body error")
        repo_single_writer_queue_contract_missing["successful_calls"] += 1
        return 4321

    def spawn_with_board(task, _workspace, board=None):
        assert board == "default"
        return invoke(task)

    def spawn_legacy(task, _workspace):
        return invoke(task)

    spawn = spawn_with_board if signature == "board" else spawn_legacy
    result = kb.dispatch_once(
        board, board="default", spawn_fn=spawn, failure_limit=2,
    )

    failed_after = kb.get_task(board, failed)
    later_after = kb.get_task(board, later)
    assert repo_single_writer_queue_contract_missing == {
        "failed_calls": 1,
        "successful_calls": 1,
    }
    assert failed_after is not None
    assert failed_after.status == "ready"
    assert failed_after.claim_lock is None
    assert failed_after.worker_pid is None
    assert failed_after.consecutive_failures == 1
    assert [item[0] for item in result.spawned] == [later]
    assert result.skipped_repo_busy == []
    assert later_after is not None
    assert later_after.worker_pid == 4321
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'spawned'",
        (failed,),
    ).fetchone()[0] == 0


def test_dry_run_projects_same_accounting_without_db_mutation(board, repositories):
    primary, _, _, _ = repositories
    first = _task(board, "first", primary, priority=2)
    second = _task(board, "second", primary, priority=1)
    before_events = board.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    result = kb.dispatch_once(board, dry_run=True)

    assert [item[0] for item in result.spawned] == [first]
    assert result.skipped_repo_busy == [(second, repo_identity(primary))]
    assert kb.get_task(board, first).status == "ready"
    assert kb.get_task(board, second).status == "ready"
    assert kb.get_task(board, first).repo_identity is None
    assert kb.get_task(board, second).repo_identity is None
    assert board.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events


def test_single_writer_dry_run_skips_all_maintenance_mutations(
    board, repositories, monkeypatch,
):
    primary, _, _, _ = repositories
    now = int(time.time())
    host_claim = kb._claimer_id()

    def running_task(
        title: str,
        *,
        claim_lock: str,
        claim_expires: int,
        started_at: int,
        worker_pid: int | None = None,
        last_heartbeat_at: int | None = None,
        max_runtime_seconds: int | None = None,
    ) -> str:
        task_id = _task(
            board, title, None, workspace_kind="scratch",
        )
        claimed = kb.claim_task(board, task_id, claimer=claim_lock)
        assert claimed is not None
        running = kb.get_task(board, task_id)
        assert running is not None
        run_id = running.current_run_id
        assert run_id is not None
        board.execute(
            "UPDATE tasks SET claim_expires = ?, started_at = ?, worker_pid = ?, "
            "last_heartbeat_at = ?, max_runtime_seconds = ? WHERE id = ?",
            (
                claim_expires,
                started_at,
                worker_pid,
                last_heartbeat_at,
                max_runtime_seconds,
                task_id,
            ),
        )
        board.execute(
            "UPDATE task_runs SET claim_expires = ?, started_at = ?, worker_pid = ?, "
            "last_heartbeat_at = ?, max_runtime_seconds = ? WHERE id = ?",
            (
                claim_expires,
                started_at,
                worker_pid,
                last_heartbeat_at,
                max_runtime_seconds,
                run_id,
            ),
        )
        board.commit()
        return task_id

    expired = running_task(
        "expired claim",
        claim_lock="remote-host:expired",
        claim_expires=now - 10,
        started_at=now - 20,
    )
    stale = running_task(
        "stale heartbeat",
        claim_lock="remote-host:stale",
        claim_expires=now + 10_000,
        started_at=now - 10_000,
        last_heartbeat_at=now - 10_000,
    )
    crashed_pid = 2_000_000_001
    crashed = running_task(
        "crashed worker",
        claim_lock=host_claim,
        claim_expires=now + 10_000,
        started_at=now - 1,
        worker_pid=crashed_pid,
        last_heartbeat_at=now,
    )
    timed_out_pid = 2_000_000_002
    timed_out = running_task(
        "max runtime",
        claim_lock=host_claim,
        claim_expires=now + 10_000,
        started_at=now - 100,
        worker_pid=timed_out_pid,
        last_heartbeat_at=now,
        max_runtime_seconds=1,
    )

    parent = _task(board, "done dependency", None, status="done", workspace_kind="scratch")
    promotable = _task(
        board, "promotable todo", None, status="todo", workspace_kind="scratch",
    )
    board.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent, promotable),
    )
    projected = _task(board, "projected ready", primary)
    board.commit()

    tables = ("tasks", "task_runs", "task_events")

    def table_rows() -> dict[str, tuple[tuple[object, ...], ...]]:
        return {
            table: tuple(
                tuple(row)
                for row in board.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in tables
        }

    db_path = Path(board.execute("PRAGMA database_list").fetchone()["file"])
    repo_single_writer_queue_contract_missing = {
        "rows": table_rows(),
        "db_bytes": db_path.read_bytes(),
        "counts": {
            table: board.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        },
        "task_fields": {
            task_id: tuple(kb.get_task(board, task_id).__dict__.items())
            for task_id in (
                expired, stale, crashed, timed_out, promotable, projected,
            )
        },
    }
    reap_calls: list[bool] = []
    monkeypatch.setattr(
        kb, "reap_worker_zombies", lambda: reap_calls.append(True) or [],
    )
    timed_out_checks = 0

    def fake_pid_alive(pid):
        nonlocal timed_out_checks
        if pid == timed_out_pid:
            timed_out_checks += 1
            return timed_out_checks == 1
        return False

    monkeypatch.setattr(kb, "_pid_alive", fake_pid_alive)
    monkeypatch.setattr(kb.os, "kill", lambda _pid, _signal: None)

    result = kb.dispatch_once(
        board,
        dry_run=True,
        stale_timeout_seconds=100,
    )

    assert [item[0] for item in result.spawned] == [projected]
    assert reap_calls == []
    assert table_rows() == repo_single_writer_queue_contract_missing["rows"]
    assert db_path.read_bytes() == repo_single_writer_queue_contract_missing["db_bytes"]
    assert {
        table: board.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    } == repo_single_writer_queue_contract_missing["counts"]
    assert {
        task_id: tuple(kb.get_task(board, task_id).__dict__.items())
        for task_id in (
            expired, stale, crashed, timed_out, promotable, projected,
        )
    } == repo_single_writer_queue_contract_missing["task_fields"]
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'repo_busy'"
    ).fetchone()[0] == 0
    projected_after = kb.get_task(board, projected)
    assert projected_after is not None
    assert projected_after.repo_identity is None


def test_malformed_stored_identity_fails_closed(board, repositories):
    primary, _, _, _ = repositories
    malformed = _task(
        board, "malformed", primary, stored_identity="A" * 64,
    )
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    assert order == []
    assert result.spawned == []
    assert kb.get_task(board, malformed).status == "ready"
    assert kb.get_task(board, malformed).repo_identity == "A" * 64


def test_mode_off_preserves_ready_then_review_and_no_repo_accounting(
    board, repositories, monkeypatch,
):
    primary, _, _, _ = repositories
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: "off")
    ready = _task(board, "ready-low", primary, priority=1, created_at=200)
    review = _task(
        board, "review-high", primary, status="review", priority=9, created_at=100,
    )
    order: list[tuple[str, list[str] | None]] = []

    result = kb.dispatch_once(board, spawn_fn=_capture_spawn(order))

    assert [item[0] for item in order] == [ready, review]
    assert not hasattr(result, "skipped_repo_busy") or result.skipped_repo_busy == []
    assert kb.get_task(board, ready).repo_identity is None
    assert kb.get_task(board, review).repo_identity is None


def test_repo_single_writer_no_live_smoke_contract():
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_kanban_repo_single_writer_no_live.py",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    data = json.loads(proc.stdout)

    assert set(data) == {
        "absent_config_mode_off",
        "canonical_temp_root",
        "config_file_absent_before_enable",
        "explicit_temp_config_single_writer",
        "first_task_fake_spawned",
        "gateway_restarted",
        "git_isolation_enforced",
        "graphify_run",
        "hostile_git_hook_suppressed",
        "imports_created_artifacts",
        "imports_spawned_process",
        "kanban_env_isolated",
        "live_board_mutated",
        "live_config_mutated",
        "lock_authority_free_after_release",
        "lock_contender_acquired_after_release",
        "lock_contender_busy_while_held",
        "lock_leaf_persists_after_release",
        "lock_probe_subprocesses_recorded",
        "network_called",
        "network_used",
        "process_argv_cwd_allowlist_clean",
        "reclaim_signal_calls_empty",
        "reclaimed_tasks_ready",
        "reclaims_returned_true",
        "repo_busy_consecutive_failures_zero",
        "repo_busy_event_payload_readback",
        "repo_writer_marker_cleared",
        "second_task_repo_busy",
        "staged_committed_pushed",
        "temp_board_created_after_enable",
        "temp_only_mutation",
        "worker_process_spawned",
    }
    assert data["absent_config_mode_off"] is True
    assert data["canonical_temp_root"] is True
    assert data["config_file_absent_before_enable"] is True
    assert data["explicit_temp_config_single_writer"] is True
    assert data["first_task_fake_spawned"] is True
    assert data["gateway_restarted"] is False
    assert data["git_isolation_enforced"] is True
    assert data["graphify_run"] is False
    assert data["hostile_git_hook_suppressed"] is True
    assert data["imports_created_artifacts"] is False
    assert data["imports_spawned_process"] is False
    assert data["kanban_env_isolated"] is True
    assert data["live_board_mutated"] is False
    assert data["live_config_mutated"] is False
    assert data["lock_authority_free_after_release"] is True
    assert data["lock_contender_acquired_after_release"] is True
    assert data["lock_contender_busy_while_held"] is True
    assert data["lock_leaf_persists_after_release"] is True
    assert data["lock_probe_subprocesses_recorded"] is True
    assert data["network_called"] is False
    assert data["network_used"] is False
    assert data["process_argv_cwd_allowlist_clean"] is True
    assert data["reclaim_signal_calls_empty"] is True
    assert data["reclaimed_tasks_ready"] is True
    assert data["reclaims_returned_true"] is True
    assert data["repo_busy_consecutive_failures_zero"] is True
    assert data["repo_busy_event_payload_readback"] is True
    assert data["repo_writer_marker_cleared"] is True
    assert data["second_task_repo_busy"] is True
    assert data["staged_committed_pushed"] is False
    assert data["temp_board_created_after_enable"] is True
    assert data["temp_only_mutation"] is True
    assert data["worker_process_spawned"] is False

    required_false_keys = {
        "gateway_restarted",
        "graphify_run",
        "imports_created_artifacts",
        "imports_spawned_process",
        "live_board_mutated",
        "live_config_mutated",
        "network_called",
        "network_used",
        "staged_committed_pushed",
        "worker_process_spawned",
    }
    assert {key for key, value in data.items() if value is False} == required_false_keys


def test_repo_single_writer_no_live_smoke_isolates_hostile_ambient_git_config(
    tmp_path,
):
    source_root = Path(__file__).resolve().parents[2]
    marker = tmp_path / "hostile-post-commit-ran"
    hostile_home = tmp_path / "hostile-home"
    hostile_template = tmp_path / "hostile-template"
    hostile_hooks = hostile_template / "hooks"
    hostile_home.mkdir(mode=0o700)
    hostile_hooks.mkdir(parents=True, mode=0o700)
    hook = hostile_hooks / "post-commit"
    hook.write_text(
        "#!/bin/sh\nprintf '%s\\n' hostile > " + shlex.quote(str(marker)) + "\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    hostile_config = (
        "[init]\n"
        f"\ttemplateDir = {hostile_template}\n"
        "[core]\n"
        f"\thooksPath = {hostile_hooks}\n"
    )
    global_config = tmp_path / "hostile-global.gitconfig"
    system_config = tmp_path / "hostile-system.gitconfig"
    global_config.write_text(hostile_config, encoding="utf-8")
    system_config.write_text(hostile_config, encoding="utf-8")
    (hostile_home / ".gitconfig").write_text(hostile_config, encoding="utf-8")

    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
        if os.environ.get(key)
    }
    env.update(
        {
            "HOME": str(hostile_home),
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_SYSTEM": str(system_config),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_kanban_repo_single_writer_no_live.py",
            "--json",
        ],
        cwd=source_root.resolve(strict=True),
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    data = json.loads(proc.stdout)

    assert marker.exists() is False
    assert data["git_isolation_enforced"] is True
    assert data["hostile_git_hook_suppressed"] is True
    assert data["process_argv_cwd_allowlist_clean"] is True
    assert data["worker_process_spawned"] is False
    assert data["staged_committed_pushed"] is False


def _harmless_fake_executable(tmp_path: Path, name: str) -> Path:
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable.resolve(strict=True)


def test_process_descriptor_binds_explicit_executable_override(tmp_path):
    fake = _harmless_fake_executable(tmp_path, "fake-python")
    env = {"PATH": os.defpath}
    argv = (sys.executable, "-V")
    ordinary = smoke._process_descriptor(
        (argv,), {"cwd": tmp_path, "env": env},
    )
    overridden = smoke._process_descriptor(
        (argv,), {"cwd": tmp_path, "env": env, "executable": str(fake)},
    )

    assert ordinary is not None
    assert overridden is not None
    assert ordinary.executable == Path(sys.executable).resolve(strict=True)
    assert overridden.executable == fake
    assert ordinary != overridden


def test_process_descriptor_resolves_git_from_effective_child_path(tmp_path):
    real_git_raw = shutil.which("git", path=os.environ.get("PATH"))
    assert real_git_raw is not None
    real_git = Path(real_git_raw).resolve(strict=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = _harmless_fake_executable(fake_bin, "git")
    argv = ("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    real = smoke._process_descriptor(
        (argv,), {"cwd": tmp_path, "env": dict(os.environ)},
    )
    shadowed = smoke._process_descriptor(
        (argv,), {"cwd": tmp_path, "env": {"PATH": str(fake_bin)}},
    )

    assert real is not None
    assert shadowed is not None
    assert real.executable == real_git
    assert shadowed.executable == fake_git
    assert real != shadowed


def test_process_environment_digest_is_order_stable_private_and_strict():
    first = smoke._environment_digest({"B": "secret-two", "A": "secret-one"})
    second = smoke._environment_digest({"A": "secret-one", "B": "secret-two"})

    assert first == second
    assert first is not None
    assert len(first) == 64
    assert "secret" not in first
    assert smoke._environment_digest({"A": 1}) is None
    assert smoke._environment_digest([("A", "1")]) is None


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("shell", True),
        ("preexec_fn", lambda: None),
        ("user", 501),
        ("group", 20),
        ("extra_groups", (20,)),
        ("start_new_session", True),
        ("process_group", 0),
    ],
    ids=[
        "shell", "preexec", "user", "group", "extra-groups",
        "new-session", "process-group",
    ],
)
def test_process_descriptor_rejects_unsafe_process_options(tmp_path, option, value):
    kwargs = {"cwd": tmp_path, "env": {"PATH": os.defpath}, option: value}
    assert smoke._process_descriptor(((sys.executable, "-V"),), kwargs) is None


def test_process_descriptor_binds_safe_options(tmp_path):
    common = {"cwd": tmp_path, "env": {"PATH": os.defpath}}
    piped = smoke._process_descriptor(
        ((sys.executable, "-V"),), {**common, "stdout": subprocess.PIPE},
    )
    discarded = smoke._process_descriptor(
        ((sys.executable, "-V"),), {**common, "stdout": subprocess.DEVNULL},
    )

    assert piped is not None
    assert discarded is not None
    assert piped != discarded


@pytest.mark.parametrize(
    "raw_args",
    [("reset", "--hard"), ("push", "origin", "main")],
    ids=["reset", "push"],
)
def test_fixture_git_raw_args_and_label_cannot_exempt_mutation(
    tmp_path, raw_args,
):
    repository = tmp_path / "repository"
    hooks_dir = tmp_path / "hooks"
    template_dir = tmp_path / "template"
    repository.mkdir()
    hooks_dir.mkdir()
    template_dir.mkdir()
    fake_git = _harmless_fake_executable(tmp_path, "git")
    git_env = {"PATH": str(tmp_path)}
    allowed = smoke._allowed_fixture_descriptors(
        repository=repository,
        git_binary=fake_git,
        git_env=git_env,
        hooks_dir=hooks_dir,
        template_dir=template_dir,
    )
    expected = []

    with pytest.raises(ValueError, match="fixture git arguments not allowed"):
        smoke._git(
            repository,
            *raw_args,
            expected=expected,
            git_binary=fake_git,
            git_env=git_env,
            hooks_dir=hooks_dir,
            template_dir=template_dir,
        )
    assert expected == []

    mutant = (
        str(fake_git),
        "-c", f"core.hooksPath={hooks_dir}",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "credential.helper=",
        *raw_args,
    )
    smoke._register_expected(
        expected,
        "fixture-git",
        mutant,
        repository,
        env=git_env,
        executable=str(fake_git),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    descriptor = expected[0][1]

    assert smoke._close_process_audit(
        [descriptor],
        expected,
        git_binary=fake_git,
        allowed_fixture_descriptors=allowed,
    ) == (False, True, True)


def test_process_audit_rejects_extra_duplicate_identity_read(tmp_path):
    fake_git = _harmless_fake_executable(tmp_path, "git")
    env = {"PATH": str(tmp_path)}
    argv = ("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    expected = []
    smoke._register_expected(
        expected,
        "identity-read",
        argv,
        tmp_path,
        env=env,
        executable=str(fake_git),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    descriptor = expected[0][1]

    assert smoke._close_process_audit(
        [descriptor, descriptor],
        expected,
        git_binary=fake_git,
        allowed_fixture_descriptors=frozenset(),
    ) == (False, True, False)

# Preserved reviewed Phase 2 Change Gate regressions. These remain in the
# reconciled R1 suite rather than being replaced by the local single-writer suite.
@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(home / "attachments"))
    kb.init_db()
    return home


def test_claim_gate_authority_runs_inside_write_transaction(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="pre-lock gate refusal",
            body=json.dumps({"contract": {"lane": "implementation"}}),
            assignee="default",
        )

        seen = []
        original = kb._check_change_gate_before_claim

        def observe_transaction(connection, *args, **kwargs):
            seen.append(connection.in_transaction)
            return original(connection, *args, **kwargs)

        monkeypatch.setattr(kb, "_check_change_gate_before_claim", observe_transaction)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)

    assert exc_info.value.reason_codes == ["CHANGE_GATE_METADATA_MISSING"]
    assert seen == [True]


def test_dispatcher_projects_invalid_gate_as_bounded_skip_and_not_spawnable(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="legacy rail gate refusal",
            body=json.dumps({"contract": {"lane": "implementation"}}),
            assignee="default",
        )
        result = kb.dispatch_once(conn, dry_run=True)
        assert kb.has_spawnable_ready(conn) is False

    assert result.spawned == []
    assert result.skipped_change_gate == [
        {"task_id": task_id, "reason_codes": ["CHANGE_GATE_METADATA_MISSING"]}
    ]
