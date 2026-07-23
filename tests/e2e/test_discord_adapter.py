"""Minimal e2e tests for Discord mention stripping + /command detection.

Covers the fix for slash commands not being recognized when sent via
@mention in a channel, especially after auto-threading.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.e2e.conftest import (
    _make_discord_adapter_wired,
    BOT_USER_ID,
    E2E_MESSAGE_SETTLE_DELAY,
    get_response_text,
    make_discord_message,
    make_fake_dm_channel,
    make_fake_thread,
)

pytestmark = pytest.mark.asyncio


async def dispatch(adapter, msg):
    await adapter._handle_message(msg)
    await asyncio.sleep(E2E_MESSAGE_SETTLE_DELAY)


async def test_discord_ingress_reaches_registered_blocked_create_no_live(
    tmp_path, monkeypatch
):
    """Real Discord adapter ingress binds one turn and reaches the registry alias."""
    import run_agent
    from gateway.run import GatewayRunner
    from gateway.session import build_session_context
    from tests.agent.test_turn_context import _FakeAgent, _build
    from tools.workflow_authority import clear_current_turn_user_authority

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "test-profile")
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_CURRENT_USER_ACTION_FINGERPRINT",
        "HERMES_CURRENT_USER_REQUEST_TARGET_FINGERPRINT",
    ):
        monkeypatch.delenv(name, raising=False)

    from hermes_cli import kanban_db as kb
    from tools import registered_local_workflow as registered

    monkeypatch.setattr(registered, "_feature_enabled", lambda: True)
    adapter, runner = _make_discord_adapter_wired()
    cached_agent = _FakeAgent()
    cached_agent.platform = "discord"
    cached_agent.session_id = (
        runner.session_store.get_or_create_session.return_value.session_id
    )
    observed: dict[str, dict] = {}

    async def _run_registered_create(event, source, _session_key, _generation):
        session_entry = runner.session_store.get_or_create_session.return_value
        context = build_session_context(source, runner.config, session_entry)
        session_tokens = GatewayRunner._set_session_env(runner, context)
        try:
            with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
                _build(
                    cached_agent,
                    user_message=event.text,
                    task_id="discord-authority-registered-e2e",
                )
            result = json.loads(
                run_agent.handle_function_call(
                    "kanban_create_blocked",
                    {"title": "Writing Plan first improvement", "assignee": "peer"},
                    "discord-authority-registered-e2e",
                    session_id=session_entry.session_id,
                    enabled_tools=["kanban_create_blocked"],
                )
            )
            observed["result"] = result
            return result
        finally:
            clear_current_turn_user_authority()
            GatewayRunner._clear_session_env(runner, session_tokens)

    runner._handle_message_with_agent = _run_registered_create
    original_message_handler = adapter._message_handler
    assert original_message_handler is not None

    async def _capture_ingress_failure(event):
        try:
            return await original_message_handler(event)
        except Exception as exc:
            observed["ingress_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise

    adapter.set_message_handler(_capture_ingress_failure)
    msg = make_discord_message(
        content=f"<@{BOT_USER_ID}> 다시 1번 카드 만들어라. 이제 될 거야.",
        channel=make_fake_thread(),
        mentions=[adapter._client.user],
        message_id=812345,
    )
    await dispatch(adapter, msg)
    for _ in range(40):
        if "result" in observed or "ingress_error" in observed:
            break
        await asyncio.sleep(0.05)

    assert "result" in observed, observed
    result = observed["result"]
    assert result["ok"] is True, result
    db_path = kb.kanban_db_path().resolve()
    assert db_path.is_relative_to(hermes_home.resolve())
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 1
    assert task is not None
    assert task.title == "Writing Plan first improvement"
    assert task.status == "blocked"


class TestMentionStrippedCommandDispatch:
    async def test_mention_then_command(self, discord_adapter, bot_user):
        """<@BOT> /help → mention stripped, /help dispatched."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_nickname_mention_then_command(self, discord_adapter, bot_user):
        """<@!BOT> /help → nickname mention also stripped, /help works."""
        msg = make_discord_message(
            content=f"<@!{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response

    async def test_text_before_command_not_detected(self, discord_adapter, bot_user):
        """'<@BOT> something else /help' → mention stripped, but 'something else /help'
        doesn't start with / so it's treated as text, not a command."""
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> something else /help",
            mentions=[bot_user],
        )
        await dispatch(discord_adapter, msg)
        # Message is accepted (not dropped by mention gate), but since it doesn't
        # start with / it's routed as text — no command output, and no agent in this
        # mock setup means no send call either.
        response = get_response_text(discord_adapter)
        assert response is None or "/new" not in response

    async def test_no_mention_in_channel_dropped(self, discord_adapter):
        """Message without @mention in server channel → silently dropped."""
        msg = make_discord_message(content="/help", mentions=[])
        await dispatch(discord_adapter, msg)
        assert get_response_text(discord_adapter) is None

    async def test_dm_no_mention_needed(self, discord_adapter):
        """DMs don't require @mention — /help works directly."""
        dm = make_fake_dm_channel()
        msg = make_discord_message(content="/help", channel=dm, mentions=[])
        await dispatch(discord_adapter, msg)
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


class TestAutoThreadingPreservesCommand:
    async def test_command_detected_after_auto_thread(self, discord_adapter, bot_user, monkeypatch):
        """@mention /help in channel with auto-thread → thread created AND command dispatched."""
        monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
        fake_thread = make_fake_thread(thread_id=90001, name="help")
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> /help",
            mentions=[bot_user],
        )

        # Simulate discord.py restoring the original raw content (with mention)
        # after create_thread(), which undoes any prior mention stripping.
        original_content = msg.content

        async def clobber_content(**kwargs):
            msg.content = original_content
            return fake_thread

        msg.create_thread = AsyncMock(side_effect=clobber_content)
        await dispatch(discord_adapter, msg)

        msg.create_thread.assert_awaited_once()
        response = get_response_text(discord_adapter)
        assert response is not None
        assert "/new" in response


class TestRepliedToMediaDispatch:
    async def test_reply_to_image_message_caches_referenced_attachment(
        self, discord_adapter, bot_user, monkeypatch
    ):
        """A text reply to an image-bearing Discord message should give the agent that image."""
        cached_path = "/tmp/replied-discord-image.png"

        async def fake_cache_image_from_url(url, *, ext=".jpg"):
            assert url == "https://cdn.discordapp.com/attachments/image.png"
            assert ext == ".png"
            return cached_path

        monkeypatch.setattr(
            "plugins.platforms.discord.adapter.cache_image_from_url",
            fake_cache_image_from_url,
        )
        discord_adapter.handle_message = AsyncMock()

        attachment = SimpleNamespace(
            content_type="image/png",
            filename="image.png",
            url="https://cdn.discordapp.com/attachments/image.png",
            size=1234,
        )
        referenced_message = SimpleNamespace(
            id=12345,
            content="",
            attachments=[attachment],
        )
        msg = make_discord_message(
            content=f"<@{BOT_USER_ID}> what's in this image?",
            mentions=[bot_user],
        )
        msg.type = 19
        msg.reference = SimpleNamespace(message_id=12345, resolved=referenced_message)

        await discord_adapter._handle_message(msg)

        discord_adapter.handle_message.assert_awaited_once()
        await_args = discord_adapter.handle_message.await_args
        assert await_args is not None
        event = await_args.args[0]
        assert event.reply_to_message_id == "12345"
        assert event.media_urls == [cached_path]
        assert event.media_types == ["image/png"]
        assert event.message_type.value == "photo"
