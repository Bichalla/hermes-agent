"""The real dispatcher must fail closed even though UI callbacks are best effort."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_failed_pre_dispatch_delivery_cannot_be_swallowed(monkeypatch):
    from agent import tool_executor as te
    dispatched = []
    def reject(*args):
        raise RuntimeError('delivery not confirmed')
    agent = SimpleNamespace(
        before_tool_execution=reject,
        _tool_guardrails=SimpleNamespace(before_call=lambda *_: SimpleNamespace(allows_execution=True)),
        _interrupt_requested=False,
        interrupt=Mock(),
    )
    monkeypatch.setattr(te, '_pre_tool_block', lambda *_: (None, {}))
    monkeypatch.setattr(te, '_begin_tool_execution', lambda *_: None)
    monkeypatch.setattr(te, '_run_with_activity_heartbeat', lambda _a, _n, f: f())
    monkeypatch.setattr(te, '_blocked_tool_result', lambda *_a, **kw: kw['block_message'])
    ref = te._ToolCallRef('terminal', {}, 'session', 'call', [])
    state = te._ManagedToolResult(None, {}, [], False, True)
    result = te._dispatch_authorized_once(
        agent, state, ref, execute=lambda _: dispatched.append(True), scope_block=None,
        display_index=None, begin_execution=None, authorization_gate=None,
    )
    assert not dispatched
    assert state.blocked
    assert 'delivery' in result
    agent.interrupt.assert_called_once()


def test_direct_invocation_also_obeys_pre_dispatch_barrier(monkeypatch):
    from agent.agent_runtime_helpers import invoke_tool
    def reject(*args):
        raise RuntimeError('delivery not confirmed')
    agent = SimpleNamespace(before_tool_execution=reject)
    with pytest.raises(RuntimeError, match='delivery not confirmed'):
        invoke_tool(agent, 'terminal', {}, 'session')


def test_external_codex_executor_is_gated_before_session_start(monkeypatch):
    from agent import codex_runtime as cr
    def reject(*args):
        raise RuntimeError('delivery not confirmed')
    agent = SimpleNamespace(before_tool_execution=reject)
    started = []
    monkeypatch.setattr(cr, '_ensure_codex_session', lambda _: started.append(True))
    with pytest.raises(RuntimeError, match='delivery not confirmed'):
        cr.run_codex_app_server_turn(agent, user_message='work', original_user_message='work', messages=[], effective_task_id='session')
    assert not started
