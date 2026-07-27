from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars


def test_session_scope_id_is_task_local_bound_and_cleared() -> None:
    tokens = set_session_vars(
        platform="discord",
        scope_id="guild-1",
        chat_id="channel-1",
        thread_id="thread-1",
        message_id="message-1",
    )
    assert get_session_env("HERMES_SESSION_SCOPE_ID") == "guild-1"
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_SCOPE_ID") == ""


def test_gateway_binds_source_scope_id_to_session_context() -> None:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    source = SessionSource(
        platform=Platform.DISCORD,
        scope_id="guild-1",
        chat_id="channel-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="user-1",
        message_id="message-1",
    )
    context = SimpleNamespace(
        source=source,
        session_key="session-key-1",
        session_id="session-1",
    )
    tokens = GatewayRunner._set_session_env(
        runner, cast(Any, context), user_text="record this"
    )
    try:
        assert get_session_env("HERMES_SESSION_SCOPE_ID") == "guild-1"
        assert get_session_env("HERMES_SESSION_MESSAGE_ID") == "message-1"
    finally:
        GatewayRunner._clear_session_env(runner, tokens)
