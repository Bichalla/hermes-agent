import json

from gateway.kanban_intake import (
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanApproval,
    SourceBinding,
    execute_pending_approval,
)
from hermes_cli import kanban_db as kb


def clear_kanban_env(monkeypatch):
    for name in [
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_execute_creates_verified_triage_card_in_temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    clear_kanban_env(monkeypatch)
    board = "lifelog-control"
    kb.create_board(board, name="Lifelog Control")
    cfg = KanbanIntakeConfig(enabled=True, default_board=board)
    proposal = KanbanCardProposal(
        board=board,
        title="Local guardrail task",
        body={"source_ref": "kp_safe", "acceptance_criteria": ["pass"]},
        source_ref="kp_safe",
        user_id="u1",
        tenant="lifelog",
    ).normalized(cfg)
    pending = PendingKanbanApproval(
        pending_id="kp_pending",
        binding=SourceBinding("discord", "raw_chat_123456789", "raw_thread_123456789", "u1", "s1"),
        proposal=proposal,
        created_at=1,
        expires_at=999,
    )
    result = execute_pending_approval(pending, cfg)
    assert result.verified is True
    conn = kb.connect(board=board)
    try:
        tasks = kb.list_tasks(conn, include_archived=True)
    finally:
        conn.close()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == proposal.title
    assert task.status == "triage"
    assert task.status not in {"ready", "running"}
    assert task.tenant == "lifelog"
    body = json.loads(task.body)
    assert body["source_ref"] == "kp_safe"
    assert "raw_chat_123456789" not in task.body
    assert "raw_thread_123456789" not in task.body
