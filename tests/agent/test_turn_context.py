"""Unit tests for the extracted turn prologue (``agent/turn_context.py``).

These exercise ``build_turn_context`` against a lightweight fake agent to
confirm the prologue produces the right ``TurnContext`` and applies the
``agent`` side effects the loop relies on — without spinning up a real
``AIAgent`` or hitting any provider.
"""

from __future__ import annotations

import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.turn_context import TurnContext, build_turn_context
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
from tools.workflow_authority import (
    clear_current_turn_user_authority,
    get_current_turn_user_authority,
)


class _FakeTodoStore:
    def has_items(self):
        return True

    def _hydrate(self, *_a, **_k):
        pass


class _FakeGuardrails:
    def __init__(self):
        self.reset_called = False

    def reset_for_turn(self):
        self.reset_called = True


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches."""

    def __init__(self):
        self.session_id = "sess-1"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-x"
        self.api_mode = "chat_completions"
        self.allowed_subject_scope = ("self", "family")
        self.allowed_domains = ("health-illness",)
        self.platform = "cli"
        self._user_id = "legacy-sender"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self.enabled_toolsets = None
        self.disabled_toolsets = None
        self._skip_mcp_refresh = False
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        # Attributes the prologue assigns; recorded for assertions.
        self._invalid_tool_retries = -1
        self._vision_supported = None
        self._persist_calls = 0
        self._session_messages = []
        self._pending_cli_user_message = None
        self._session_persist_lock = threading.RLock()
        # Records _cached_system_prompt at the moment _ensure_db_session()
        # is called (regression guard for #45499 turn-setup ordering).
        self._ensure_db_prompt_at_call = "<unset>"

    # --- methods the prologue calls ---
    def _ensure_db_session(self):
        self._ensure_db_prompt_at_call = self._cached_system_prompt

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, *_a, **_k):
        self._persist_calls += 1


def _make_agent_with_cooldown(db_path, session_id, *, cooldown_until=None):
    agent = _FakeAgent()
    agent.compression_enabled = True
    agent._emit_status = MagicMock()
    agent._compress_context = MagicMock(
        side_effect=lambda messages, *_a, **_k: (messages, "SYSTEM")
    )

    db = SessionDB(db_path=db_path)
    db.create_session(session_id, source="cli")
    if cooldown_until is not None:
        db.record_compression_failure_cooldown(session_id, cooldown_until, "timeout")

    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
    compressor.bind_session_state(db, session_id)
    agent.context_compressor = compressor
    agent._session_db = db
    return agent


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    """``build_turn_context`` calls ``auxiliary_client.set_runtime_main`` as a
    production side effect (telling aux tools the live main provider/model).
    That writes a module-level global these unit tests don't care about and
    which would otherwise leak into sibling tests (e.g. provider-parity
    resolution) when the per-test process isolation plugin is disabled. Stub
    it out so the prologue tests stay hermetic.
    """
    clear_current_turn_user_authority()
    clear_session_vars([])
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield
    clear_current_turn_user_authority()
    clear_session_vars([])


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


def test_returns_turn_context_with_user_message_appended():
    agent = _FakeAgent()
    ctx = _build(agent)
    assert isinstance(ctx, TurnContext)
    assert ctx.user_message == "hello"
    # The user turn was appended and indexed.
    assert ctx.messages[-1] == {"role": "user", "content": "hello"}
    assert ctx.current_turn_user_idx == len(ctx.messages) - 1
    assert ctx.active_system_prompt == "SYSTEM"


def test_applies_agent_side_effects():
    agent = _FakeAgent()
    _build(agent)
    # Retry counters reset, guardrails reset, vision re-armed, turn counted.
    assert agent._invalid_tool_retries == 0
    assert agent._tool_guardrails.reset_called is True
    assert agent._vision_supported is True
    assert agent._user_turn_count == 1
    # Crash-resilience persistence fired once.
    assert agent._persist_calls == 1
    # task/turn ids assigned on the agent.
    assert agent._current_task_id
    assert agent._current_turn_id


def test_task_id_passthrough():
    agent = _FakeAgent()
    ctx = _build(agent, task_id="fixed-task")
    assert ctx.effective_task_id == "fixed-task"
    assert agent._current_task_id == "fixed-task"


def test_foreground_user_turn_binds_hidden_authority_without_message_text():
    agent = _FakeAgent()
    ctx = _build(agent, user_message="synthetic private content")
    authority = get_current_turn_user_authority()
    assert authority is not None
    assert authority.turn_id == ctx.turn_id
    assert authority.source_role == "user"
    assert authority.session_scope == agent.session_id
    assert authority.platform_scope == agent.platform
    assert authority.user_message_index == ctx.current_turn_user_idx
    assert "synthetic private content" not in repr(authority)


def test_foreground_card_imperative_with_trailing_context_binds_create_authority(
    tmp_path, monkeypatch
):
    import json

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-profile")
    agent = _FakeAgent()
    agent.platform = "discord"
    tokens = set_session_vars(
        platform="discord",
        chat_id="chat-42",
        thread_id="thread-7",
        user_id="user-9",
        session_id=agent.session_id,
        message_id="message-123",
    )
    try:
        _build(agent, user_message="다시 1번 카드 만들어라. 이제 될 거야.")
        authority = get_current_turn_user_authority()
        assert authority is not None
        assert authority.allows("explicit_blocked_card_create") is True
        assert authority.source_event_fingerprint

        from hermes_cli import kanban_db as kb
        from tools import kanban_tools as kt

        kb._INITIALIZED_PATHS.clear()
        result = json.loads(
            kt._handle_create(
                {
                    "title": "Writing Plan first improvement",
                    "assignee": "default",
                    "initial_status": "blocked",
                }
            )
        )
        assert result["ok"] is True
        assert result["status"] == "blocked"
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, result["task_id"])
        assert task is not None
        assert task.status == "blocked"

        set_session_vars(
            platform="discord",
            chat_id="chat-42",
            thread_id="thread-7",
            user_id="user-9",
            session_id="different-cached-session",
            message_id="message-123",
        )
        mismatched = json.loads(
            kt._handle_create(
                {
                    "title": "Writing Plan first improvement",
                    "assignee": "default",
                    "initial_status": "blocked",
                }
            )
        )
        assert mismatched.get("ok") is not True
        assert "did not authorize" in mismatched["error"]
    finally:
        clear_session_vars(tokens)


def test_background_review_turn_clears_and_does_not_bind_authority():
    foreground = _FakeAgent()
    _build(foreground)
    assert get_current_turn_user_authority() is not None

    background = _FakeAgent()
    background._skip_mcp_refresh = True
    _build(background)
    assert get_current_turn_user_authority() is None


def test_automation_platform_does_not_bind_foreground_authority():
    agent = _FakeAgent()
    agent.platform = "cron"
    _build(agent)
    assert get_current_turn_user_authority() is None


def test_persist_user_message_becomes_original():
    agent = _FakeAgent()
    ctx = _build(agent, user_message="api-prefixed", persist_user_message="clean")
    # original_user_message tracks the clean persist override.
    assert ctx.original_user_message == "clean"
    # but the appended user turn carries the full (sanitized) message.
    assert ctx.messages[-1]["content"] == "api-prefixed"


def test_pre_llm_hook_receives_profile_and_real_session_authority_contextvars():
    agent = _FakeAgent()
    captured = {}
    tokens = set_session_vars(
        platform="discord",
        chat_id="chat-42",
        thread_id="thread-7",
        user_id="authenticated-user",
    )

    def _capture(_hook_name, **kwargs):
        captured.update(kwargs)
        return [{"context": "synthetic plugin context"}]

    try:
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="work"), \
             patch("hermes_cli.plugins.invoke_hook", side_effect=_capture):
            ctx = _build(agent)
    finally:
        clear_session_vars(tokens)

    assert captured["active_profile"] == "work"
    assert captured["authenticated_sender_id"] == "authenticated-user"
    assert captured["platform"] == "discord"
    assert captured["chat_id"] == "chat-42"
    assert captured["thread_id"] == "thread-7"
    assert captured["tenant_scope"] == "local-profile:work"
    assert captured["current_turn_user_idx"] == ctx.current_turn_user_idx
    assert captured["runtime_mode"] == "chat_completions"
    assert captured["allowed_subject_scope"] == ("self", "family")
    assert captured["allowed_domains"] == ("health-illness",)
    assert captured["sender_id"] == "legacy-sender"
    assert ctx.plugin_user_context == "synthetic plugin context"


def test_pre_llm_hook_history_is_detached_from_persisted_messages():
    agent = _FakeAgent()
    observed = {}

    def _mutate_history(_hook_name, **kwargs):
        history = kwargs["conversation_history"]
        observed["history"] = history
        history[kwargs["current_turn_user_idx"]]["content"] = "mutated"
        history.append({"role": "user", "content": "injected"})
        return []

    with patch("hermes_cli.plugins.invoke_hook", side_effect=_mutate_history):
        ctx = _build(agent, user_message="original user message")

    assert observed["history"][-1]["content"] == "injected"
    assert len(ctx.messages) == 1
    assert ctx.messages[0] == {"role": "user", "content": "original user message"}


def test_hostile_runtime_subclass_is_rejected_without_private_log(caplog):
    private_marker = "PRIVATE_RUNTIME_BOOL_MUST_NOT_LEAK"

    class HostileRuntime(str):
        def __bool__(self):
            raise RuntimeError(private_marker)

        def __repr__(self):
            return private_marker

        def __str__(self):
            raise RuntimeError(private_marker)

    agent = _FakeAgent()
    agent.api_mode = HostileRuntime("chat_completions")
    captured = {}

    def _capture(_hook_name, **kwargs):
        captured.update(kwargs)
        return []

    with patch("hermes_cli.plugins.invoke_hook", side_effect=_capture):
        _build(agent)

    assert captured["runtime_mode"] == ""
    assert private_marker not in caplog.text


def test_pre_llm_authority_contextvars_remain_isolated_across_contexts():
    from contextvars import Context

    first_context = Context()
    second_context = Context()
    first_context.run(
        set_session_vars,
        platform="discord",
        chat_id="chat-a",
        thread_id="thread-a",
        user_id="user-a",
    )
    second_context.run(
        set_session_vars,
        platform="telegram",
        chat_id="chat-b",
        thread_id="thread-b",
        user_id="user-b",
    )

    def _capture(context, profile):
        captured = {}
        with patch(
            "hermes_cli.profiles.get_active_profile_name",
            return_value=profile,
        ), patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=lambda _name, **kwargs: captured.update(kwargs) or [],
        ):
            context.run(_build, _FakeAgent())
        return captured

    first = _capture(first_context, "first")
    second = _capture(second_context, "second")
    first_again = _capture(first_context, "first")

    assert (
        first["active_profile"],
        first["platform"],
        first["chat_id"],
        first["thread_id"],
        first["authenticated_sender_id"],
        first["tenant_scope"],
    ) == (
        "first",
        "discord",
        "chat-a",
        "thread-a",
        "user-a",
        "local-profile:first",
    )
    assert (
        second["active_profile"],
        second["platform"],
        second["chat_id"],
        second["thread_id"],
        second["authenticated_sender_id"],
        second["tenant_scope"],
    ) == (
        "second",
        "telegram",
        "chat-b",
        "thread-b",
        "user-b",
        "local-profile:second",
    )
    authority_keys = (
        "active_profile",
        "platform",
        "chat_id",
        "thread_id",
        "authenticated_sender_id",
        "tenant_scope",
        "allowed_subject_scope",
        "allowed_domains",
    )
    assert {key: first_again[key] for key in authority_keys} == {
        key: first[key] for key in authority_keys
    }
    first_context.run(clear_session_vars, [])
    second_context.run(clear_session_vars, [])


def test_default_chat_completions_plugin_context_is_ephemeral_and_appended_exactly():
    from agent import conversation_loop

    agent = _FakeAgent()
    tokens = set_session_vars(
        platform="discord",
        chat_id="chat-42",
        thread_id="thread-7",
        user_id="authenticated-user",
    )
    try:
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="work"), \
             patch(
                 "hermes_cli.plugins.invoke_hook",
                 return_value=[{"context": "synthetic plugin context"}],
             ):
            ctx = _build(agent, user_message="original user message")
    finally:
        clear_session_vars(tokens)

    persisted_snapshot = [message.copy() for message in ctx.messages]

    class CapturedApiMessages(BaseException):
        def __init__(self, messages):
            self.messages = messages

    agent.iteration_budget = types.SimpleNamespace(
        remaining=1,
        consume=lambda: True,
        refund=lambda: None,
    )
    agent._budget_grace_call = False
    agent._checkpoint_mgr = types.SimpleNamespace(new_turn=lambda: None)
    agent._touch_activity = lambda *_a, **_k: None
    agent.step_callback = None
    agent._skill_nudge_interval = 0
    agent._drain_pending_steer = lambda: None
    agent._sanitize_tool_call_arguments = lambda *_a, **_k: 0
    agent._copy_reasoning_content_for_api = lambda *_a, **_k: None
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    agent.prefill_messages = []
    agent._use_prompt_caching = False
    agent._sanitize_api_messages = lambda messages: messages
    agent._drop_thinking_only_and_merge_users = lambda messages, **_k: messages
    agent._has_stream_consumers = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent.thinking_callback = None
    agent.verbose_logging = False
    agent._api_max_retries = 1
    agent._force_ascii_payload = False
    agent._reset_stream_delivery_tracking = lambda: None
    agent._reapply_reasoning_echo_for_provider = lambda _messages: None

    def _capture_api(messages):
        raise CapturedApiMessages([message.copy() for message in messages])

    agent._build_api_kwargs = _capture_api

    with patch.object(conversation_loop, "build_turn_context", return_value=ctx):
        with pytest.raises(CapturedApiMessages) as captured:
            conversation_loop.run_conversation(agent, "ignored by patched prologue")

    api_user = next(
        message
        for message in captured.value.messages
        if message.get("role") == "user"
    )
    assert api_user["content"] == (
        "original user message\n\nsynthetic plugin context"
    )
    assert ctx.messages == persisted_snapshot
    assert ctx.messages[ctx.current_turn_user_idx]["content"] == "original user message"


def test_pending_cli_message_carries_durable_marker_to_new_turn_dict():
    """A close-persisted CLI input must not be written again by turn start."""
    agent = _FakeAgent()
    staged = {"role": "user", "content": "already durable", "_db_persisted": True}
    agent._pending_cli_user_message = staged

    ctx = _build(agent, user_message="already durable")

    assert ctx.messages[-1] is staged
    assert ctx.messages[-1]["content"] == "already durable"
    assert ctx.messages[-1]["_db_persisted"] is True
    assert agent._pending_cli_user_message is None


def test_stale_pending_cli_message_does_not_replace_new_turn_input():
    """A failed prior persistence handoff cannot substitute later user input."""
    agent = _FakeAgent()
    agent._pending_cli_user_message = {"role": "user", "content": "old prompt"}

    stale = agent._pending_cli_user_message
    ctx = _build(
        agent,
        user_message="new prompt",
        conversation_history=[{"role": "assistant", "content": "old answer"}],
    )

    assert ctx.messages[-1]["content"] == "new prompt"
    assert ctx.messages[-1] is not stale
    assert agent._pending_cli_user_message is None


def test_pending_cli_message_uses_clean_override_for_api_local_note():
    """A noted API message reuses the clean staged dict and its DB marker."""
    agent = _FakeAgent()
    staged = {"role": "user", "content": "clean prompt", "_db_persisted": True}
    agent._pending_cli_user_message = staged

    ctx = _build(
        agent,
        user_message="[MODEL NOTE]\n\nclean prompt",
        persist_user_message="clean prompt",
    )

    assert ctx.messages[-1] is staged
    assert ctx.messages[-1]["content"] == "[MODEL NOTE]\n\nclean prompt"
    assert ctx.messages[-1]["_db_persisted"] is True
    assert agent._pending_cli_user_message is None


def test_memory_nudge_fires_at_interval():
    agent = _FakeAgent()
    agent._memory_nudge_interval = 1
    agent.valid_tool_names = {"memory"}
    agent._memory_store = object()
    ctx = _build(agent)
    assert ctx.should_review_memory is True
    assert agent._turns_since_memory == 0  # reset after firing


def test_no_review_when_memory_disabled():
    agent = _FakeAgent()
    ctx = _build(agent)
    assert ctx.should_review_memory is False


def test_ensure_db_session_runs_after_system_prompt_restore():
    """Regression for #45499.

    On a fresh API/gateway agent (``_cached_system_prompt is None``) the DB
    session row must be created AFTER the system prompt is restored/built, so
    the persisted snapshot is written non-NULL. If ``_ensure_db_session()``
    ran first it would insert ``system_prompt=NULL`` and trip the misleading
    "stored system prompt is null; rebuilding" warning plus a first-turn
    prefix cache miss.
    """
    agent = _FakeAgent()
    agent._cached_system_prompt = None  # fresh agent, no cached prompt yet

    def _restore(_agent, _system_message, _history):
        _agent._cached_system_prompt = "REBUILT-SYSTEM"

    _build(agent, restore_or_build_system_prompt=_restore)

    # The prompt was populated before the DB row was created.
    assert agent._ensure_db_prompt_at_call == "REBUILT-SYSTEM"
    assert agent._cached_system_prompt == "REBUILT-SYSTEM"


# ── Between-turns MCP refresh (cache-safe late-binding) ──────────────────────
#
# A slow MCP server that connects after the agent's build-time tool snapshot
# must become callable by the user's NEXT turn — without mutating an in-flight
# turn's cached request prefix. The prologue is exactly that boundary, so the
# refresh hook lives here. These assert the contract (R1/R2/R6 in the spec),
# not timing permutations.


def test_between_turns_refresh_adds_late_tool_when_servers_registered():
    """R1: a tool that registered since build lands in this turn's snapshot."""
    agent = _FakeAgent()

    new_def = {"type": "function", "function": {"name": "mcp_x_tool", "description": "", "parameters": {}}}

    import model_tools
    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(model_tools, "get_tool_definitions", return_value=[new_def]):
        _build(agent)

    assert "mcp_x_tool" in agent.valid_tool_names
    assert any(t["function"]["name"] == "mcp_x_tool" for t in agent.tools)


def test_between_turns_refresh_skipped_when_no_servers():
    """R6: the common case (no MCP servers) never walks the registry."""
    agent = _FakeAgent()
    import model_tools

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=False), \
         patch.object(model_tools, "get_tool_definitions") as gtd:
        _build(agent)

    gtd.assert_not_called()


def test_between_turns_refresh_skipped_when_skip_flag_set():
    """Internal forks (background_review) set _skip_mcp_refresh to keep tools[]
    byte-identical to the parent for cache parity — the hook must honor it even
    when MCP servers are registered."""
    agent = _FakeAgent()
    agent._skip_mcp_refresh = True
    import model_tools

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(model_tools, "get_tool_definitions") as gtd:
        _build(agent)

    gtd.assert_not_called()


def test_between_turns_refresh_no_churn_when_unchanged():
    """R2: an unchanged tool set leaves the snapshot object identity intact
    (no needless swap → nothing for the next request prefix to diff against)."""
    agent = _FakeAgent()
    same = [{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}]
    agent.tools = same
    agent.valid_tool_names = {"a"}

    import model_tools
    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(
             model_tools, "get_tool_definitions",
             return_value=[{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}],
         ):
        _build(agent)

    assert agent.tools is same  # not replaced → no churn


def test_preflight_skips_when_persisted_cooldown_survives_restart(tmp_path):
    agent = _make_agent_with_cooldown(
        tmp_path / "state.db",
        "sess-1",
        cooldown_until=4_000_000_000.0,
    )

    with patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
         patch("agent.turn_context.estimate_request_tokens_rough", return_value=999_999):
        ctx = _build(agent)

    assert isinstance(ctx, TurnContext)
    agent._emit_status.assert_not_called()
    agent._compress_context.assert_not_called()


def test_preflight_still_runs_for_other_session_with_same_db(tmp_path):
    db_path = tmp_path / "state.db"
    _make_agent_with_cooldown(
        db_path,
        "sess-1",
        cooldown_until=4_000_000_000.0,
    )
    agent = _make_agent_with_cooldown(db_path, "sess-2")

    with patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
         patch("agent.turn_context.estimate_request_tokens_rough", return_value=999_999):
        ctx = _build(agent)

    assert isinstance(ctx, TurnContext)
    agent._emit_status.assert_called_once()
    agent._compress_context.assert_called()


def test_expired_cooldown_allows_preflight(tmp_path):
    agent = _make_agent_with_cooldown(
        tmp_path / "state.db",
        "sess-1",
        cooldown_until=1.0,
    )

    with patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
         patch("agent.turn_context.estimate_request_tokens_rough", return_value=999_999):
        ctx = _build(agent)

    assert isinstance(ctx, TurnContext)
    agent._emit_status.assert_called_once()
    agent._compress_context.assert_called()
