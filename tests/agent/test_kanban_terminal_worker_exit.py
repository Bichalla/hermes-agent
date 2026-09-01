"""Trusted terminal-loop boundaries for dispatcher-owned Kanban workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import model_tools
import run_agent
from agent import conversation_loop
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB
from run_agent import AIAgent


TERMINAL_RESPONSE = (
    "Kanban worker run ended after its terminal lifecycle transition."
)


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str, arguments: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _response(
    *,
    content: str = "",
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str = "tool_calls",
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _make_agent(home: Path, *tool_names: str) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _bind_worker(
    monkeypatch: pytest.MonkeyPatch,
    task,
) -> None:
    assert task.current_run_id is not None
    assert task.claim_lock
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    monkeypatch.setenv("HERMES_SESSION_ID", "kanban-worker-session")


def _clear_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _claimed_task(
    *,
    title: str = "worker task",
    assignee: str = "executor",
    claimer: str = "test-claim",
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title=title, assignee=assignee)
        task = kb.claim_task(conn, task_id, claimer=claimer)
        assert task is not None
        return task


def _reviewer_task():
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review worker", assignee="executor")
        executor = kb.claim_task(conn, task_id, claimer="executor-claim")
        assert executor is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready for review",
            reviewer="reviewer",
            expected_run_id=executor.current_run_id,
        )
        reviewer = kb.claim_review_task(
            conn,
            task_id,
            claimer="reviewer-claim",
        )
        assert reviewer is not None
        return reviewer


def _terminal_args(tool_name: str) -> dict:
    return {
        "kanban_complete": {"summary": "complete"},
        "kanban_block": {"reason": "needs operator input", "kind": "needs_input"},
        "kanban_request_review": {
            "summary": "implementation complete",
            "reviewer": "reviewer",
        },
        "kanban_request_changes": {"reason": "correct the synthetic result"},
    }[tool_name]


@pytest.mark.parametrize(
    ("tool_name", "expected_outcome"),
    (
        ("kanban_complete", "completed"),
        ("kanban_block", "blocked"),
        ("kanban_request_review", "review_requested"),
        ("kanban_request_changes", "changes_requested"),
    ),
)
def test_terminal_lifecycle_success_stops_before_second_provider_call(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    expected_outcome: str,
) -> None:
    task = _reviewer_task() if tool_name == "kanban_request_changes" else _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, tool_name)
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call(tool_name, _terminal_args(tool_name), "terminal")]
        ),
        _response(content="worker re-entered", finish_reason="stop"),
    ]

    result = agent.run_conversation("perform the worker handoff")

    assert agent.client.chat.completions.create.call_count == 1
    assert result["final_response"] == TERMINAL_RESPONSE
    assert result["turn_exit_reason"] == "dispatcher_worker_run_terminal"
    assert result["completed"] is True
    assert agent._executing_tools is False
    with kb.connect() as conn:
        run = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?",
            (task.current_run_id,),
        ).fetchone()
        assert run["outcome"] == expected_outcome
        assert run["ended_at"] is not None


def test_request_review_persists_transition_tool_result_and_finalizes_once(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "kanban_request_review")
    state_db_path = kanban_home / "state.db"
    state_db = SessionDB(db_path=state_db_path)
    state_db.create_session(
        session_id="terminal-session",
        source="kanban",
        model="test/model",
    )
    state_db.set_session_title("terminal-session", "Terminal lifecycle test")
    agent._session_db = state_db
    agent._session_db_created = True
    agent.session_id = "terminal-session"
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._persist_disabled = False
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call(
                    "kanban_request_review",
                    _terminal_args("kanban_request_review"),
                    "review",
                )
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    cleanup = MagicMock(wraps=agent._cleanup_task_resources)
    save = MagicMock(wraps=agent._save_trajectory)
    persist = MagicMock(wraps=agent._persist_session)
    memory_sync = MagicMock(wraps=agent._sync_external_memory_for_turn)
    background_review = MagicMock()
    finalizer = MagicMock(wraps=conversation_loop.finalize_turn)
    micro_compact = MagicMock(wraps=agent.context_compressor._micro_compact)
    agent.context_compressor._micro_compact_enabled = True

    try:
        with (
            patch.object(agent, "_cleanup_task_resources", cleanup),
            patch.object(agent, "_save_trajectory", save),
            patch.object(agent, "_persist_session", persist),
            patch.object(agent, "_sync_external_memory_for_turn", memory_sync),
            patch.object(agent, "_spawn_background_review", background_review),
            patch.object(agent.context_compressor, "_micro_compact", micro_compact),
            patch("agent.conversation_loop.finalize_turn", finalizer),
        ):
            result = agent.run_conversation("request review")

        durable = state_db.get_messages_as_conversation("terminal-session")
    finally:
        state_db.close()

    assert result["final_response"] == TERMINAL_RESPONSE
    assert agent.client.chat.completions.create.call_count == 1
    assert cleanup.call_count == 1
    assert save.call_count == 1
    assert persist.call_count >= 1
    assert memory_sync.call_count == 1
    assert finalizer.call_count == 1
    background_review.assert_not_called()
    micro_compact.assert_not_called()
    assert [message["role"] for message in durable][-3:] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert durable[-2]["tool_call_id"] == "review"
    persisted_result = json.loads(durable[-2]["content"])
    assert persisted_result["ok"] is True
    assert persisted_result["task_id"] == task.id
    assert durable[-1]["content"] == TERMINAL_RESPONSE


def test_failed_stale_run_lifecycle_does_not_false_stop(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id + 1000))
    agent = _make_agent(kanban_home, "kanban_request_review")
    message = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "kanban_request_review",
                _terminal_args("kanban_request_review"),
                "stale",
            )
        ]
    )

    agent._execute_tool_calls(message, [], "worker-session")

    assert agent._dispatcher_worker_terminal_exit is None
    with kb.connect() as conn:
        current = kb.get_task(conn, task.id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == task.current_run_id


def test_foreign_task_lifecycle_does_not_end_current_worker(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    with kb.connect() as conn:
        foreign_id = kb.create_task(conn, title="foreign", assignee="other")
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "kanban_complete")
    message = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "kanban_complete",
                {"task_id": foreign_id, "summary": "not mine"},
                "foreign",
            )
        ]
    )

    agent._execute_tool_calls(message, [], "worker-session")

    assert agent._dispatcher_worker_terminal_exit is None
    with kb.connect() as conn:
        current = kb.get_task(conn, task.id)
        foreign = kb.get_task(conn, foreign_id)
        assert current is not None and current.status == "running"
        assert foreign is not None and foreign.status == "ready"


def test_ordinary_worker_tool_continues_until_lifecycle_handoff(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "terminal", "kanban_complete")
    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[_tool_call("terminal", {"command": "noop"}, "ordinary")]),
        _response(
            tool_calls=[
                _tool_call(
                    "kanban_complete",
                    _terminal_args("kanban_complete"),
                    "complete",
                )
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    real_handle = model_tools.handle_function_call

    def dispatch(name, args, task_id=None, **kwargs):
        if name == "terminal":
            return json.dumps({"ok": True})
        return real_handle(name, args, task_id, **kwargs)

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = agent.run_conversation("use one ordinary tool, then finish")

    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == TERMINAL_RESPONSE


def test_orchestrator_lifecycle_call_retains_next_provider_turn(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker(monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="orchestrated", assignee="executor")
    agent = _make_agent(kanban_home, "kanban_request_review")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call(
                    "kanban_request_review",
                    {
                        "task_id": task_id,
                        "summary": "operator-directed review",
                    },
                    "review",
                )
            ]
        ),
        _response(content="continued", finish_reason="stop"),
    ]

    result = agent.run_conversation("route the task")

    assert agent.client.chat.completions.create.call_count == 2
    assert result["final_response"] == "continued"
    assert agent._dispatcher_worker_terminal_exit is None
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "review"


def test_mixed_batch_skips_model_facing_release_after_terminal_success(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(
        kanban_home,
        "kanban_request_review",
        "change_gate_release",
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call(
                    "kanban_request_review",
                    _terminal_args("kanban_request_review"),
                    "review",
                ),
                _tool_call(
                    "change_gate_release",
                    {"task_id": task.id, "purpose": "G4"},
                    "release",
                ),
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    release_calls: list[dict] = []
    real_handle = model_tools.handle_function_call

    def dispatch(name, args, task_id=None, **kwargs):
        if name == "change_gate_release":
            release_calls.append(dict(args))
            return json.dumps({"success": True})
        return real_handle(name, args, task_id, **kwargs)

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = agent.run_conversation("handoff and then release")

    assert result["final_response"] == TERMINAL_RESPONSE
    assert agent.client.chat.completions.create.call_count == 1
    assert release_calls == []
    tool_results = [
        message for message in result["messages"] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_results[-2:]] == [
        "review",
        "release",
    ]
    assert "Tool skipped" in tool_results[-1]["content"]
    assert tool_results[-1]["effect_disposition"] == "none"


def test_mixed_batch_preserves_calls_before_terminal_and_skips_calls_after(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(
        kanban_home,
        "terminal",
        "kanban_request_review",
        "change_gate_release",
    )
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("terminal", {"command": "before"}, "before"),
                _tool_call(
                    "kanban_request_review",
                    _terminal_args("kanban_request_review"),
                    "review",
                ),
                _tool_call(
                    "change_gate_release",
                    {"task_id": task.id, "purpose": "G4"},
                    "after",
                ),
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    observed_calls: list[str] = []
    real_handle = model_tools.handle_function_call

    def dispatch(name, args, task_id=None, **kwargs):
        observed_calls.append(name)
        if name == "terminal":
            return json.dumps({"ok": True})
        if name == "change_gate_release":
            return json.dumps({"ok": True})
        return real_handle(name, args, task_id, **kwargs)

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = agent.run_conversation("run ordered batch")

    assert result["final_response"] == TERMINAL_RESPONSE
    assert agent.client.chat.completions.create.call_count == 1
    assert observed_calls == ["terminal", "kanban_request_review"]
    tool_results = [
        message for message in result["messages"] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_results[-3:]] == [
        "before",
        "review",
        "after",
    ]
    assert "Tool skipped" in tool_results[-1]["content"]


def test_failed_lifecycle_call_does_not_skip_later_batch_call(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "kanban_request_review", "terminal")
    message = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "kanban_request_review",
                {"summary": ""},
                "failed-review",
            ),
            _tool_call("terminal", {"command": "after"}, "after"),
        ]
    )
    observed_calls: list[str] = []
    real_handle = model_tools.handle_function_call

    def dispatch(name, args, task_id=None, **kwargs):
        observed_calls.append(name)
        if name == "terminal":
            return json.dumps({"ok": True})
        return real_handle(name, args, task_id, **kwargs)

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        agent._execute_tool_calls(message, [], "worker-session")

    assert observed_calls == ["kanban_request_review", "terminal"]
    assert agent._dispatcher_worker_terminal_exit is None
    with kb.connect() as conn:
        current = kb.get_task(conn, task.id)
        assert current is not None
        assert current.status == "running"
        assert current.current_run_id == task.current_run_id


def test_dispatcher_projects_one_reviewer_after_worker_terminal_exit(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "kanban_request_review")
    message = SimpleNamespace(
        tool_calls=[
            _tool_call(
                "kanban_request_review",
                _terminal_args("kanban_request_review"),
                "review",
            )
        ]
    )
    agent._execute_tool_calls(message, [], "worker-session")
    assert agent._dispatcher_worker_terminal_exit["outcome"] == "review_requested"

    import hermes_cli.config as config
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(
        config,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    spawn_calls: list[str] = []

    def spawn(next_task, *_args, **_kwargs):
        spawn_calls.append(next_task.id)
        return os.getpid()

    with kb.connect() as conn:
        first = kb.dispatch_once(conn, spawn_fn=spawn)
        second = kb.dispatch_once(conn, spawn_fn=spawn)
        current = kb.get_task(conn, task.id)

    assert [item[0] for item in first.spawned] == [task.id]
    assert second.spawned == []
    assert spawn_calls == [task.id]
    assert current is not None and current.status == "running"


def test_g2_terminal_outcome_stops_before_post_g2_provider_or_tool_calls(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task(assignee="planner")
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "kanban_g2_handoff", "terminal")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("kanban_g2_handoff", {}, "g2"),
                _tool_call("terminal", {"command": "must-not-run"}, "after"),
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    observed_calls: list[str] = []

    def dispatch(name, args, task_id=None, **kwargs):
        observed_calls.append(name)
        if name != "kanban_g2_handoff":
            return json.dumps({"ok": True})
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL, "
                "claim_expires = NULL WHERE id = ?",
                (task.id,),
            )
            closed = kb._end_run(
                conn,
                task.id,
                outcome="g2_handoff",
                status="done",
            )
            assert closed == task.current_run_id
        return json.dumps({"ok": True, "parent_status": "done"})

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = agent.run_conversation("freeze the planner handoff")

    assert agent.client.chat.completions.create.call_count == 1
    assert observed_calls == ["kanban_g2_handoff"]
    assert result["final_response"] == TERMINAL_RESPONSE
    assert result["turn_exit_reason"] == "dispatcher_worker_run_terminal"
    tool_results = [
        message for message in result["messages"] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_results[-2:]] == [
        "g2",
        "after",
    ]
    assert "Tool skipped" in tool_results[-1]["content"]
    assert tool_results[-1]["effect_disposition"] == "none"


def test_terminal_outcome_drift_stops_fail_closed_before_g2(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _claimed_task(assignee="planner")
    _bind_worker(monkeypatch, task)
    agent = _make_agent(kanban_home, "terminal", "kanban_g2_handoff")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("terminal", {"command": "unexpected-close"}, "ordinary"),
                _tool_call("kanban_g2_handoff", {}, "g2"),
            ]
        ),
        _response(content="must not run", finish_reason="stop"),
    ]
    observed_calls: list[str] = []

    def dispatch(name, args, task_id=None, **kwargs):
        observed_calls.append(name)
        if name != "terminal":
            raise AssertionError("G2 must not execute after terminal outcome drift")
        with kb.connect() as conn:
            assert kb.complete_task(
                conn,
                task.id,
                summary="unexpected non-G2 close",
                expected_run_id=task.current_run_id,
                fire_lifecycle_hook=False,
            )
        return json.dumps({"ok": True})

    with patch("run_agent.handle_function_call", side_effect=dispatch):
        result = agent.run_conversation("attempt an outcome-confused G2 batch")

    assert agent.client.chat.completions.create.call_count == 1
    assert observed_calls == ["terminal"]
    assert result["completed"] is False
    assert result["turn_exit_reason"] == (
        "dispatcher_worker_run_terminal_outcome_mismatch"
    )
    assert "unexpected terminal lifecycle outcome" in result["final_response"]
    assert agent._dispatcher_worker_terminal_exit == {
        "task_id": task.id,
        "run_id": task.current_run_id,
        "outcome": "completed",
        "tool_name": "terminal",
        "matched_registry_outcome": False,
        "allowed_outcomes": [],
    }
    tool_results = [
        message for message in result["messages"] if message.get("role") == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_results[-2:]] == [
        "ordinary",
        "g2",
    ]
    assert "Tool skipped" in tool_results[-1]["content"]
    assert tool_results[-1]["effect_disposition"] == "none"


def test_registry_terminal_metadata_is_model_invisible_and_outcome_specific() -> None:
    from tools.registry import registry

    expected = {
        "kanban_complete": {"completed"},
        "kanban_block": {"blocked"},
        "kanban_request_review": {"review_requested"},
        "kanban_request_changes": {"changes_requested"},
        "kanban_g2_handoff": {"g2_handoff"},
    }
    for name, outcomes in expected.items():
        entry = registry.get_entry(name)
        assert entry is not None
        assert entry.dispatcher_worker_terminal_outcomes == outcomes
        assert "dispatcher_worker_terminal_outcomes" not in entry.schema


def test_snapshot_requires_exact_task_run_and_claim(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.kanban_tools import snapshot_dispatcher_worker_run

    task = _claimed_task()
    _bind_worker(monkeypatch, task)
    identity = snapshot_dispatcher_worker_run()
    assert identity is not None
    assert identity.task_id == task.id
    assert identity.run_id == task.current_run_id

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "foreign-claim")
    assert snapshot_dispatcher_worker_run() is None
