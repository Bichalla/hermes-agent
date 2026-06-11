import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
import plugins.platforms.discord.adapter as discord_platform
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    DiscordTTSListenView,
    _apply_yaml_config,
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
async def test_send_omits_listen_button_for_streaming_interim_metadata():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True}},
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

    result = await adapter.send(
        "555",
        "초기 스트리밍 프리뷰 ▉",
        metadata={"streaming": True, "final": False},
    )

    assert result.success is True
    assert "view" not in send_calls[0]


@pytest.mark.asyncio
async def test_edit_message_finalize_adds_listen_button_for_final_content():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True, "label": "듣기"}},
        )
    )

    edit_calls = []

    async def fake_edit(**kwargs):
        edit_calls.append(kwargs)

    message = SimpleNamespace(edit=AsyncMock(side_effect=fake_edit))
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.edit_message("555", "1234", "최종 답변입니다.", finalize=True)

    assert result.success is True
    view = edit_calls[0].get("view")
    assert isinstance(view, DiscordTTSListenView)
    assert view.text == "최종 답변입니다."


@pytest.mark.asyncio
async def test_edit_message_releases_unattached_tts_view_on_edit_failure():
    discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.clear()
    discord_platform._DISCORD_TTS_LISTEN_LOCKS.clear()
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True}},
        )
    )

    async def fail_edit(**_kwargs):
        raise RuntimeError("discord edit failed")

    message = SimpleNamespace(edit=AsyncMock(side_effect=fail_edit))
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.edit_message(
        "555",
        "1234",
        "실패 시 남으면 안 되는 최종 답변",
        finalize=True,
        metadata={"streaming": True, "final": True},
    )

    assert result.success is False
    assert discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE == {}
    assert discord_platform._DISCORD_TTS_LISTEN_LOCKS == {}


@pytest.mark.asyncio
async def test_edit_message_nonfinal_success_does_not_create_unattached_tts_view():
    discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.clear()
    discord_platform._DISCORD_TTS_LISTEN_LOCKS.clear()
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True}},
        )
    )

    edit_calls = []

    async def fake_edit(**kwargs):
        edit_calls.append(kwargs)

    message = SimpleNamespace(edit=AsyncMock(side_effect=fake_edit))
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.edit_message("555", "1234", "아직 최종 아님", finalize=False)

    assert result.success is True
    assert edit_calls == [{"content": "아직 최종 아님"}]
    assert discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE == {}
    assert discord_platform._DISCORD_TTS_LISTEN_LOCKS == {}


@pytest.mark.asyncio
async def test_edit_message_segment_finalize_omits_listen_button_for_nonfinal_streaming_content():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"tts_listen_button": {"enabled": True, "label": "듣기"}},
        )
    )

    edit_calls = []

    async def fake_edit(**kwargs):
        edit_calls.append(kwargs)

    message = SimpleNamespace(edit=AsyncMock(side_effect=fake_edit))
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.edit_message(
        "555",
        "1234",
        "도구 호출 전 중간 프리앰블",
        finalize=True,
        metadata={"streaming": True, "final": False},
    )

    assert result.success is True
    assert "view" not in edit_calls[0]


def test_listen_view_uses_bounded_timeout_and_expiring_text_cache():
    view = DiscordTTSListenView(
        text="장기 보관하면 안 되는 답변",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )

    assert view.timeout is not None
    assert 0 < view.timeout <= 6 * 3600
    assert view.text == "장기 보관하면 안 되는 답변"


def test_tts_listen_cleanup_purges_expired_orphan_text_entries():
    discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.clear()
    discord_platform._DISCORD_TTS_LISTEN_LOCKS.clear()
    view = DiscordTTSListenView(
        text="send 실패 후 남은 orphan text",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )
    try:
        discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE[view.entry_id] = (
            "send 실패 후 남은 orphan text",
            time.time() - 1,
        )

        adapter = DiscordAdapter(
            PlatformConfig(
                enabled=True,
                token="***",
                extra={"tts_listen_button": {"enabled": True}},
            )
        )
        adapter._tts_listen_last_cleanup = -10_000
        adapter._maybe_cleanup_tts_listen_cache()

        assert view.entry_id not in discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE
        assert view.entry_id not in discord_platform._DISCORD_TTS_LISTEN_LOCKS
    finally:
        discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.pop(view.entry_id, None)
        discord_platform._DISCORD_TTS_LISTEN_LOCKS.pop(view.entry_id, None)


def test_tts_listen_cleanup_enforces_memory_entry_max_oldest_first():
    discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.clear()
    discord_platform._DISCORD_TTS_LISTEN_LOCKS.clear()
    views = [
        DiscordTTSListenView(
            text=f"orphan {idx}",
            allowed_user_ids={"111"},
            ttl_days=3,
            ephemeral=True,
        )
        for idx in range(3)
    ]
    try:
        now = time.time()
        for idx, view in enumerate(views):
            discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE[view.entry_id] = (
                f"orphan {idx}",
                now + 100 + idx,
            )

        adapter = DiscordAdapter(
            PlatformConfig(
                enabled=True,
                token="***",
                extra={"tts_listen_button": {"enabled": True, "max_files": 1}},
            )
        )
        adapter._tts_listen_last_cleanup = -10_000
        adapter._maybe_cleanup_tts_listen_cache()

        assert views[0].entry_id not in discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE
        assert views[1].entry_id not in discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE
        assert views[2].entry_id in discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE
    finally:
        for view in views:
            discord_platform._DISCORD_TTS_LISTEN_TEXT_CACHE.pop(view.entry_id, None)
            discord_platform._DISCORD_TTS_LISTEN_LOCKS.pop(view.entry_id, None)


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
    assert (tmp_path / f"{view.cache_stem}.mp3").exists()
    assert interaction.response.defer.await_count == 2
    assert interaction.followup.send.await_count == 2
    sent_kwargs = interaction.followup.send.await_args.kwargs
    assert sent_kwargs["ephemeral"] is True
    assert sent_kwargs["content"] == "🔊 음성으로 듣기"
    assert sent_kwargs["file"] is not None
    assert sent_kwargs["file"].filename == "hermes-tts.mp3"


@pytest.mark.asyncio
async def test_listen_button_concurrent_clicks_share_one_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_platform, "_discord_tts_listen_cache_dir", lambda: tmp_path)

    calls = []
    first_call_entered = threading.Event()
    release_provider = threading.Event()

    def fake_tts(text, output_path=None):
        assert output_path is not None
        calls.append({"text": text, "output_path": output_path})
        first_call_entered.set()
        assert release_provider.wait(timeout=5)
        with open(output_path, "wb") as fh:
            fh.write(b"mp3-data")
        return json.dumps({"success": True, "file_path": output_path})

    import tools.tts_tool as tts_tool

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    view = DiscordTTSListenView(
        text="동시 클릭은 하나의 생성만 수행한다.",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )

    def interaction():
        return SimpleNamespace(
            user=SimpleNamespace(id=111, roles=[], display_name="tester"),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    first = interaction()
    second = interaction()
    first_task = asyncio.create_task(view._handle_click(first))
    assert await asyncio.to_thread(first_call_entered.wait, 5)
    second_task = asyncio.create_task(view._handle_click(second))
    await asyncio.sleep(0.05)
    release_provider.set()
    await asyncio.gather(first_task, second_task)

    assert len(calls) == 1
    assert first.followup.send.await_count == 1
    assert second.followup.send.await_count == 1


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
    assert sent_kwargs["file"].filename == "hermes-tts.ogg"


@pytest.mark.asyncio
async def test_listen_button_rejects_provider_path_outside_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_platform, "_discord_tts_listen_cache_dir", lambda: tmp_path / "cache")
    outside = tmp_path / "outside.mp3"

    def fake_tts(text, output_path=None):
        outside.write_bytes(b"secret-ish data")
        return json.dumps({"success": True, "file_path": str(outside)})

    import tools.tts_tool as tts_tool

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    view = DiscordTTSListenView(
        text="provider path containment",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )

    with pytest.raises(RuntimeError, match="outside Discord TTS cache"):
        await view._get_or_generate_audio_path()


def test_listen_button_ignores_symlink_cache_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_platform, "_discord_tts_listen_cache_dir", lambda: tmp_path / "cache")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")

    view = DiscordTTSListenView(
        text="symlink cache must not be sent",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )
    link = cache_dir / f"{view.cache_stem}.mp3"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unsupported")

    assert view._find_cached_audio_path() is None


@pytest.mark.asyncio
async def test_listen_button_error_after_defer_uses_followup(monkeypatch):
    def fake_tts(text, output_path=None):
        raise RuntimeError("provider exploded")

    import tools.tts_tool as tts_tool

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    view = DiscordTTSListenView(
        text="defer 이후 에러",
        allowed_user_ids={"111"},
        ttl_days=3,
        ephemeral=True,
    )
    response = SimpleNamespace(
        defer=AsyncMock(),
        send_message=AsyncMock(),
        is_done=lambda: True,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, roles=[], display_name="tester"),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await view._handle_click(interaction)

    response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited()
    assert "TTS 생성 실패" in interaction.followup.send.await_args.args[0]


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


def test_cleanup_discord_tts_listen_cache_enforces_max_files_oldest_first(tmp_path):
    now = 1_700_000_000.0
    files = [tmp_path / f"tts_listen_{idx}.mp3" for idx in range(4)]
    for idx, path in enumerate(files):
        path.write_bytes(b"x")
        os.utime(path, (now + idx, now + idx))

    removed = _cleanup_discord_tts_listen_cache(
        tmp_path,
        ttl_days=30,
        now=now + 10,
        max_files=2,
    )

    assert removed == 2
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
    assert files[3].exists()


def test_cleanup_discord_tts_listen_cache_enforces_max_bytes_oldest_first(tmp_path):
    now = 1_700_000_000.0
    small_old = tmp_path / "tts_listen_old.mp3"
    large_mid = tmp_path / "tts_listen_mid.mp3"
    small_new = tmp_path / "tts_listen_new.mp3"
    for idx, (path, size) in enumerate(((small_old, 3), (large_mid, 5), (small_new, 3))):
        path.write_bytes(b"x" * size)
        os.utime(path, (now + idx, now + idx))

    removed = _cleanup_discord_tts_listen_cache(
        tmp_path,
        ttl_days=30,
        now=now + 10,
        max_bytes=6,
    )

    assert removed == 2
    assert not small_old.exists()
    assert not large_mid.exists()
    assert small_new.exists()


def test_apply_yaml_config_bridges_tts_label_and_ephemeral(monkeypatch):
    for env_name in (
        "HERMES_DISCORD_TTS_LISTEN_BUTTON",
        "HERMES_DISCORD_TTS_LISTEN_TTL_DAYS",
        "HERMES_DISCORD_TTS_LISTEN_EPHEMERAL",
        "HERMES_DISCORD_TTS_LISTEN_LABEL",
        "HERMES_DISCORD_TTS_LISTEN_MAX_FILES",
        "HERMES_DISCORD_TTS_LISTEN_MAX_BYTES",
    ):
        monkeypatch.delenv(env_name, raising=False)

    _apply_yaml_config(
        {},
        {
            "tts_listen_button": {
                "enabled": True,
                "ttl_days": 3,
                "ephemeral": False,
                "label": "Listen",
                "max_files": 7,
                "max_bytes": 12345,
            }
        },
    )

    assert os.environ["HERMES_DISCORD_TTS_LISTEN_BUTTON"] == "true"
    assert os.environ["HERMES_DISCORD_TTS_LISTEN_TTL_DAYS"] == "3"
    assert os.environ["HERMES_DISCORD_TTS_LISTEN_EPHEMERAL"] == "false"
    assert os.environ["HERMES_DISCORD_TTS_LISTEN_LABEL"] == "Listen"
    assert os.environ["HERMES_DISCORD_TTS_LISTEN_MAX_FILES"] == "7"
    assert os.environ["HERMES_DISCORD_TTS_LISTEN_MAX_BYTES"] == "12345"


def test_tts_listen_button_config_parses_string_false_ephemeral(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_TTS_LISTEN_BUTTON", "true")
    monkeypatch.setenv("HERMES_DISCORD_TTS_LISTEN_EPHEMERAL", "false")
    monkeypatch.setenv("HERMES_DISCORD_TTS_LISTEN_LABEL", "Listen")
    monkeypatch.setenv("HERMES_DISCORD_TTS_LISTEN_MAX_FILES", "9")
    monkeypatch.setenv("HERMES_DISCORD_TTS_LISTEN_MAX_BYTES", "54321")
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="***", extra={"tts_listen_button": {}})
    )

    assert adapter._tts_listen_button_enabled is True
    assert adapter._tts_listen_button_ephemeral is False
    assert adapter._tts_listen_button_label == "Listen"
    assert adapter._tts_listen_button_max_files == 9
    assert adapter._tts_listen_button_max_bytes == 54321
