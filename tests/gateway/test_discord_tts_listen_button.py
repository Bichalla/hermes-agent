import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    DiscordTTSListenView,
    _cleanup_discord_tts_listen_cache,
)


@pytest.mark.asyncio
async def test_send_attaches_lazy_tts_button_when_enabled():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True, "label": "듣기"}},
        )
    )

    send_calls = []

    async def fake_send(**kwargs):
        send_calls.append(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send("555", "안녕하세요. 음성 버튼 테스트입니다.")

    assert result.success is True
    assert channel.send.await_count == 1
    view = send_calls[0].get("view")
    assert isinstance(view, DiscordTTSListenView)
    assert view.text == "안녕하세요. 음성 버튼 테스트입니다."
    assert view.children
    first_child = view.children[0]
    assert getattr(first_child, "label") == "듣기"
    assert str(getattr(first_child, "emoji")) == "🔊"


@pytest.mark.asyncio
async def test_listen_button_generates_mp3_only_after_click_and_reuses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_platform, "_discord_tts_listen_cache_dir", lambda: tmp_path)

    calls = []

    def fake_tts(text, output_path=None):
        assert output_path is not None
        calls.append({"text": text, "output_path": output_path})
        with open(output_path, "wb") as fh:
            fh.write(b"mp3-data")
        return json.dumps({"success": True, "file_path": output_path})

    import tools.tts_tool as tts_tool

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    view = DiscordTTSListenView(
        text="클릭 전에는 TTS를 만들지 않는다.",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )
    assert calls == []

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, roles=[], display_name="tester"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await view._handle_click(interaction)
    await view._handle_click(interaction)

    assert len(calls) == 1
    assert calls[0]["text"] == "클릭 전에는 TTS를 만들지 않는다."
    assert calls[0]["output_path"].endswith(".mp3")
    assert os.path.exists(calls[0]["output_path"])
    assert interaction.response.defer.await_count == 2
    assert interaction.followup.send.await_count == 2
    sent_kwargs = interaction.followup.send.await_args.kwargs
    assert sent_kwargs["ephemeral"] is True
    assert sent_kwargs["content"] == "🔊 음성으로 듣기"
    assert sent_kwargs["file"] is not None


@pytest.mark.asyncio
async def test_listen_button_reuses_cache_when_provider_rewrites_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_platform, "_discord_tts_listen_cache_dir", lambda: tmp_path)

    calls = []

    def fake_tts(text, output_path=None):
        assert output_path is not None
        calls.append({"text": text, "output_path": output_path})
        ogg_path = os.path.splitext(output_path)[0] + ".ogg"
        with open(ogg_path, "wb") as fh:
            fh.write(b"ogg-data")
        return json.dumps({"success": True, "file_path": ogg_path})

    import tools.tts_tool as tts_tool

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    view = DiscordTTSListenView(
        text="현재 provider가 ogg로 확장자를 바꿔도 캐시를 재사용한다.",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, roles=[], display_name="tester"),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await view._handle_click(interaction)
    await view._handle_click(interaction)

    assert len(calls) == 1
    sent_kwargs = interaction.followup.send.await_args.kwargs
    assert sent_kwargs["file"].filename.endswith(".ogg")


def test_cleanup_discord_tts_listen_cache_deletes_only_stale_listen_files(tmp_path):
    now = 1_700_000_000.0
    old_listen = tmp_path / "tts_listen_old.mp3"
    fresh_listen = tmp_path / "tts_listen_fresh.mp3"
    old_other = tmp_path / "manual_old.mp3"
    old_txt = tmp_path / "tts_listen_old.txt"

    for path in (old_listen, fresh_listen, old_other, old_txt):
        path.write_bytes(b"x")

    os.utime(old_listen, (now - 4 * 86400, now - 4 * 86400))
    os.utime(fresh_listen, (now - 1 * 86400, now - 1 * 86400))
    os.utime(old_other, (now - 4 * 86400, now - 4 * 86400))
    os.utime(old_txt, (now - 4 * 86400, now - 4 * 86400))

    removed = _cleanup_discord_tts_listen_cache(tmp_path, ttl_days=3, now=now)

    assert removed == 1
    assert not old_listen.exists()
    assert fresh_listen.exists()
    assert old_other.exists()
    assert old_txt.exists()
