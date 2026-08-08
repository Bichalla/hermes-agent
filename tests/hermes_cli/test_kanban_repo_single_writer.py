"""Single-writer and legacy dispatcher Change Gate choke-point regressions."""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home / "kanban"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(home / "attachments"))
    kb.init_db()
    return home


def test_claim_gate_refuses_before_write_transaction(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="pre-lock gate refusal",
            body=json.dumps({"contract": {"lane": "implementation"}}),
            assignee="default",
        )

        def fail_if_locked(*_args, **_kwargs):
            raise AssertionError("Change Gate must run before write_txn")

        monkeypatch.setattr(kb, "write_txn", fail_if_locked)
        with pytest.raises(kb.ChangeGateBlocked) as exc_info:
            kb.claim_task(conn, task_id)

    assert exc_info.value.reason_codes == ["CHANGE_GATE_METADATA_MISSING"]


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
