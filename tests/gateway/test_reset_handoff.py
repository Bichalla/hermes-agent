from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="thread",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="thread",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="thread",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_handoff_captures_old_transcript_before_reset(mock_invoke_hook, tmp_path):
    runner = _make_runner()
    order: list[str] = []

    runner._session_db.get_messages.side_effect = lambda session_id: order.append(
        f"get_messages:{session_id}"
    ) or [
        {"role": "user", "content": "keep original intent, local only"},
        {"role": "assistant", "content": "working on it"},
    ]
    runner.session_store.reset_session.side_effect = lambda session_key: order.append(
        "reset_session"
    ) or runner.session_store.get_or_create_session.return_value

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/new"))

    assert order == ["get_messages:sess-old", "reset_session"]
    assert "keep original intent" not in str(result)
    handoffs = sorted(p for p in (tmp_path / "handoffs" / "default").glob("*.md") if p.name != "latest.md")
    assert len(handoffs) == 1
    assert "keep original intent, local only" in handoffs[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_handoff_awaits_async_session_db_get_messages(mock_invoke_hook, tmp_path):
    """Gateway uses AsyncSessionDB in production; get_messages returns a coroutine."""
    runner = _make_runner()
    order: list[str] = []

    async def _get_messages(session_id: str):
        order.append(f"get_messages:{session_id}")
        return [
            {"role": "user", "content": "async transcript survives reset"},
        ]

    runner._session_db.get_messages = _get_messages
    runner.session_store.reset_session.side_effect = lambda session_key: order.append(
        "reset_session"
    ) or runner.session_store.get_or_create_session.return_value

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/new"))

    assert "Session reset" in str(result) or "New session" in str(result)
    assert order == ["get_messages:sess-old", "reset_session"]
    handoffs = sorted(p for p in (tmp_path / "handoffs" / "default").glob("*.md") if p.name != "latest.md")
    assert len(handoffs) == 1
    assert "async transcript survives reset" in handoffs[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_handoff_failure_does_not_block_reset_and_logs_safely(
    mock_invoke_hook,
    tmp_path,
    caplog,
):
    runner = _make_runner()
    runner._session_db.get_messages.side_effect = RuntimeError("SECRET transcript excerpt")

    with caplog.at_level(logging.WARNING), patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/reset"))

    runner.session_store.reset_session.assert_called_once()
    assert "Session reset" in str(result) or "New session" in str(result)
    assert "SECRET transcript excerpt" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_reply_surfaces_handoff_path_only(mock_invoke_hook, tmp_path):
    runner = _make_runner()
    runner._session_db.get_messages.return_value = [
        {"role": "user", "content": "sensitive body should stay inside local artifact"},
    ]

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "surface": "body",
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/new"))

    text = str(result)
    assert "Handoff:" in text
    assert str(tmp_path / "handoffs" / "default") in text
    assert "sensitive body should stay inside local artifact" not in text


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_reply_can_include_opt_in_safe_preview(mock_invoke_hook, tmp_path):
    runner = _make_runner()
    runner._session_db.get_messages.return_value = [
        {"role": "user", "content": "private user detail should stay in local artifact"},
        {
            "role": "assistant",
            "content": "record complete. event_id: `diet_v1_abc`. validation ok. Next: use low sodium dinner.",
        },
    ]

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                    "preview": {"enabled": True, "max_chars": 400, "max_items": 4},
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/reset"))

    text = str(result)
    assert "Handoff:" in text
    assert "Preview:" in text
    assert "Last completed:" in text
    assert "Open loop:" in text
    assert "Evidence:" in text
    assert "private user detail should stay in local artifact" not in text


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_preview_surfaces_phase1_quality_contract_without_transcript_body(
    mock_invoke_hook,
    tmp_path,
):
    runner = _make_runner()
    runner._session_db.get_messages.return_value = [
        {
            "role": "user",
            "content": "private approval body should stay local only",
        },
        {
            "role": "assistant",
            "content": "record complete. validation ok. Next: inspect local artifact only.",
        },
        {
            "role": "tool",
            "content": "SECRET tool result must stay out of remote preview",
        },
        {
            "role": "user",
            "content": "[SESSION HANDOFF — REFERENCE ONLY] stale handoff meta",
        },
    ]

    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                    "max_messages": 1,
                    "max_chars": 1000,
                    "preview": {"enabled": True, "max_chars": 600, "max_items": 4},
                }
            }
        },
    ):
        result = await runner._handle_reset_command(_make_event("/reset"))

    text = str(result)
    assert "Preview:" in text
    assert "Evidence:" in text
    assert "1 meta filtered" in text
    assert "1 tool excluded" in text
    assert "max_messages=true" in text
    assert "detectors=bullet_and_newline_aware" in text
    assert "latest_user_fallback_suppressed=true" in text
    assert "private approval body should stay local only" not in text
    assert "record complete. validation ok" not in text
    assert "inspect local artifact only" not in text
    assert "SECRET tool result" not in text
    assert "stale handoff meta" not in text


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_preview_failure_keeps_handoff_path_and_reset_success(mock_invoke_hook, tmp_path, caplog):
    runner = _make_runner()
    runner._session_db.get_messages.return_value = [
        {"role": "assistant", "content": "record complete. validation ok."},
    ]

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET preview body")

    with caplog.at_level(logging.WARNING), patch(
        "hermes_cli.config.load_config",
        return_value={
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "artifact_dir": str(tmp_path / "handoffs" / "default"),
                    "preview": {"enabled": True},
                }
            }
        },
    ), patch("hermes_cli.session_handoff.build_handoff_preview", side_effect=boom):
        result = await runner._handle_reset_command(_make_event("/reset"))

    text = str(result)
    assert "Session reset" in text or "New session" in text
    assert "Handoff:" in text
    assert "Preview:" not in text
    assert "SECRET preview body" not in caplog.text
    assert "RuntimeError" in caplog.text
