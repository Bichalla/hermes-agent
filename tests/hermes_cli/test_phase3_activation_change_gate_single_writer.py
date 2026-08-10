from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


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
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def _task(conn, title="candidate"):
    task_id = kb.create_task(
        conn,
        title=title,
        assignee="worker",
        workspace_kind="scratch",
    )
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    conn.commit()
    return task_id


def _run_case(board, monkeypatch, *, gate_valid, repo_busy, dry_run):
    task_id = _task(board, f"case-{gate_valid}-{repo_busy}-{dry_run}")
    identity = "a" * 64
    gate_calls = []
    repo_calls = []
    spawn_calls = []

    def gate(_conn, _task_id, *, board=None):
        gate_calls.append(_task_id)
        if not gate_valid:
            raise kb.ChangeGateBlocked(("CHANGE_GATE_SCHEMA_INVALID",))

    def repo_probe(*_args, **_kwargs):
        repo_calls.append(task_id)
        return identity, repo_busy

    def spawn(task, workspace, board=None):
        spawn_calls.append(task.id)
        return 4321

    monkeypatch.setattr(kb, "_check_change_gate_before_claim", gate)
    monkeypatch.setattr(kb, "_repo_busy_for_task", repo_probe)
    before = board.execute(
        "SELECT status, claim_lock, repo_identity FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    result = kb.dispatch_once(board, dry_run=dry_run, spawn_fn=spawn)
    after = board.execute(
        "SELECT status, claim_lock, repo_identity FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    events = board.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    return task_id, identity, gate_calls, repo_calls, spawn_calls, result, before, after, events


@pytest.mark.parametrize(
    "gate_valid,repo_busy,dry_run",
    [
        (False, False, False),
        (False, True, False),
        (True, True, False),
        (True, False, False),
        (False, False, True),
        (False, True, True),
        (True, True, True),
        (True, False, True),
    ],
)
def test_single_writer_gate_precedence_matrix(
    board, monkeypatch, gate_valid, repo_busy, dry_run
):
    (
        task_id,
        identity,
        gate_calls,
        repo_calls,
        spawn_calls,
        result,
        before,
        after,
        events,
    ) = _run_case(
        board,
        monkeypatch,
        gate_valid=gate_valid,
        repo_busy=repo_busy,
        dry_run=dry_run,
    )

    if not gate_valid:
        assert result.skipped_change_gate == [
            {"task_id": task_id, "reason_codes": ["CHANGE_GATE_SCHEMA_INVALID"]}
        ]
        assert result.spawned == []
        assert result.skipped_repo_busy == []
        assert repo_calls == []
        assert spawn_calls == []
        assert "a" * 64 not in repr(result.skipped_change_gate)
        assert before == after
        assert not [row for row in events if row["kind"] == "repo_busy"]
        return

    assert result.skipped_change_gate == []
    assert len(repo_calls) >= 1
    if repo_busy:
        assert result.skipped_repo_busy == [(task_id, identity)]
        assert result.spawned == []
        assert spawn_calls == []
        if dry_run:
            assert not [row for row in events if row["kind"] == "repo_busy"]
        else:
            assert [row["kind"] for row in events].count("repo_busy") == 1
    else:
        assert result.skipped_repo_busy == []
        if dry_run:
            assert spawn_calls == []
        else:
            assert spawn_calls == [task_id]
        assert result.spawned and result.spawned[0][0] == task_id
        if dry_run:
            assert before == after
        else:
            assert after[0] == "running"


def test_single_writer_claim_time_gate_race_is_skipped_without_spawn(
    board, monkeypatch
):
    task_id = _task(board, "claim-time-race")
    calls = []
    spawn_calls = []

    def gate(_conn, _task_id, *, board=None):
        calls.append(_task_id)
        if len(calls) == 2:
            raise kb.ChangeGateBlocked(("claim_time_gate",))

    monkeypatch.setattr(kb, "_check_change_gate_before_claim", gate)
    monkeypatch.setattr(kb, "_repo_busy_for_task", lambda *_a, **_k: (None, False))
    result = kb.dispatch_once(
        board,
        spawn_fn=lambda task, workspace, board=None: spawn_calls.append(task.id),
    )

    assert len(calls) == 2
    assert result.skipped_change_gate == [
        {"task_id": task_id, "reason_codes": ["CHANGE_GATE_SCHEMA_INVALID"]}
    ]
    assert result.spawned == []
    assert spawn_calls == []
    task = kb.get_task(board, task_id)
    assert task is not None
    assert task.status == "ready"
    assert "claim-time-race" not in json.dumps(result.skipped_change_gate)
