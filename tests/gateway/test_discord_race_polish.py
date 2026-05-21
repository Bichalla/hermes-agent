"""Discord adapter race polish: concurrent join_voice_channel must not
double-invoke channel.connect() on the same guild."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter():
    from gateway.platforms.discord import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter._platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="t")
    adapter._ready_event = asyncio.Event()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._on_voice_disconnect = None
    adapter._client = MagicMock()
    return adapter


def _fake_named_tempfile(tmp_path):
    class FakeTmp:
        name = str(tmp_path / "voice.wav")

        def close(self):
            return None

    return lambda *args, **kwargs: FakeTmp()


@pytest.mark.asyncio
async def test_concurrent_joins_do_not_double_connect():
    """Two concurrent join_voice_channel calls on the same guild must
    serialize through the per-guild lock — only ONE channel.connect()
    actually fires; the second sees the _voice_clients entry the first
    just installed."""
    adapter = _make_adapter()

    connect_count = [0]
    release = asyncio.Event()

    class FakeVC:
        def __init__(self, channel):
            self.channel = channel

        def is_connected(self):
            return True

        async def move_to(self, _channel):
            return None

    async def slow_connect(self):
        connect_count[0] += 1
        await release.wait()
        return FakeVC(self)

    channel = MagicMock()
    channel.id = 111
    channel.guild.id = 42
    channel.connect = lambda: slow_connect(channel)

    from gateway.platforms import discord as discord_mod
    with patch.object(discord_mod, "VoiceReceiver",
                      MagicMock(return_value=MagicMock(start=lambda: None))):
        with patch.object(discord_mod.asyncio, "ensure_future",
                          lambda _c: asyncio.create_task(asyncio.sleep(0))):
            t1 = asyncio.create_task(adapter.join_voice_channel(channel))
            t2 = asyncio.create_task(adapter.join_voice_channel(channel))
            await asyncio.sleep(0.05)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)

    assert connect_count[0] == 1, (
        f"expected 1 channel.connect() call, got {connect_count[0]} — "
        "per-guild lock is not serializing join_voice_channel"
    )
    assert r1 is True and r2 is True
    assert 42 in adapter._voice_clients


def _install_fake_voice_playback(adapter, monkeypatch, tmp_path, calls, sleeps, *, env_value):
    guild_id = 42
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"fake")

    class FakeReceiver:
        def pause(self, clear_buffers=False):
            calls.append(("pause", clear_buffers))

        def resume(self, clear_buffers=False):
            calls.append(("resume", clear_buffers))

        def clear_buffers(self):
            calls.append(("clear", None))

    class FakeVoiceClient:
        def is_connected(self):
            return True

        def is_playing(self):
            return False

        def play(self, _source, after=None):
            calls.append(("play", None))
            if after:
                after(None)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    adapter._voice_clients[guild_id] = FakeVoiceClient()
    adapter._voice_receivers[guild_id] = FakeReceiver()
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))
    monkeypatch.setenv("HERMES_DISCORD_VOICE_POST_TTS_COOLDOWN_SECONDS", env_value)
    monkeypatch.setenv("HERMES_DISCORD_VOICE_CLEAR_BUFFERS_ON_TTS", "true")
    monkeypatch.setattr("gateway.platforms.discord.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("gateway.platforms.discord.discord.FFmpegPCMAudio", lambda *_a, **_k: object())
    monkeypatch.setattr("gateway.platforms.discord.discord.PCMVolumeTransformer", lambda source, volume=1.0: source)
    return guild_id, audio_path


@pytest.mark.asyncio
async def test_play_in_voice_channel_applies_post_tts_cooldown(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    sleeps = []
    guild_id, audio_path = _install_fake_voice_playback(
        adapter, monkeypatch, tmp_path, calls, sleeps, env_value="1.25"
    )

    assert await DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path)) is True

    assert ("pause", True) in calls
    assert sleeps == [1.25]
    assert calls[-1] == ("resume", True)


@pytest.mark.asyncio
async def test_play_in_voice_channel_skips_cooldown_sleep_when_zero(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    sleeps = []
    guild_id, audio_path = _install_fake_voice_playback(
        adapter, monkeypatch, tmp_path, calls, sleeps, env_value="0"
    )

    assert await DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path)) is True

    assert ("pause", True) in calls
    assert sleeps == []
    assert calls[-1] == ("resume", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("env_value", ["not-a-float", "-1"])
async def test_play_in_voice_channel_uses_default_cooldown_for_invalid_or_negative_env(
    monkeypatch, tmp_path, env_value
):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    sleeps = []
    guild_id, audio_path = _install_fake_voice_playback(
        adapter, monkeypatch, tmp_path, calls, sleeps, env_value=env_value
    )

    assert await DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path)) is True

    assert ("pause", True) in calls
    assert sleeps == [1.0]
    assert calls[-1] == ("resume", True)


@pytest.mark.asyncio
async def test_play_in_voice_channel_resumes_receiver_when_play_raises(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    guild_id = 42
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"fake")
    calls = []

    class FakeReceiver:
        def pause(self, clear_buffers=False):
            calls.append(("pause", clear_buffers))

        def resume(self, clear_buffers=False):
            calls.append(("resume", clear_buffers))

    class FakeVoiceClient:
        def is_connected(self):
            return True

        def is_playing(self):
            return False

        def play(self, _source, after=None):
            raise RuntimeError("boom")

    adapter._voice_clients[guild_id] = FakeVoiceClient()
    adapter._voice_receivers[guild_id] = FakeReceiver()
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))
    monkeypatch.setenv("HERMES_DISCORD_VOICE_CLEAR_BUFFERS_ON_TTS", "true")
    monkeypatch.setattr("gateway.platforms.discord.discord.FFmpegPCMAudio", lambda *_a, **_k: object())
    monkeypatch.setattr("gateway.platforms.discord.discord.PCMVolumeTransformer", lambda source, volume=1.0: source)

    assert await DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path)) is False
    assert calls[0] == ("pause", True)
    assert ("reset_timeout", guild_id) in calls
    assert calls[-1] == ("resume", True)


@pytest.mark.asyncio
async def test_process_voice_input_resets_timeout_for_valid_transcript(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    callback_payloads = []
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))

    async def callback(**kwargs):
        callback_payloads.append(kwargs)

    adapter._voice_input_callback = callback

    monkeypatch.setattr("gateway.platforms.discord.tempfile.NamedTemporaryFile", _fake_named_tempfile(tmp_path))
    monkeypatch.setattr("gateway.platforms.discord.VoiceReceiver.pcm_to_wav", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "tools.transcription_tools.transcribe_audio",
        lambda _path: {"success": True, "transcript": "혼불아 안녕"},
    )
    monkeypatch.setattr("tools.voice_mode.is_whisper_hallucination", lambda _text: False)

    await DiscordAdapter._process_voice_input(adapter, guild_id=42, user_id=7, pcm_data=b"pcm")

    assert calls == [("reset_timeout", 42)]
    assert callback_payloads == [{"guild_id": 42, "user_id": 7, "transcript": "혼불아 안녕"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transcription_result,is_hallucination",
    [
        ({"success": False, "transcript": "혼불아"}, False),
        ({"success": True, "transcript": "   "}, False),
        ({"success": True, "transcript": "엔진음,"}, True),
    ],
)
async def test_process_voice_input_does_not_reset_timeout_for_ignored_transcripts(
    monkeypatch, tmp_path, transcription_result, is_hallucination
):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []
    callback_payloads = []
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))

    async def callback(**kwargs):
        callback_payloads.append(kwargs)

    adapter._voice_input_callback = callback
    monkeypatch.setattr("gateway.platforms.discord.tempfile.NamedTemporaryFile", _fake_named_tempfile(tmp_path))
    monkeypatch.setattr("gateway.platforms.discord.VoiceReceiver.pcm_to_wav", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.transcription_tools.transcribe_audio", lambda _path: transcription_result)
    monkeypatch.setattr("tools.voice_mode.is_whisper_hallucination", lambda _text: is_hallucination)

    await DiscordAdapter._process_voice_input(adapter, guild_id=42, user_id=7, pcm_data=b"pcm")

    assert calls == []
    assert callback_payloads == []


@pytest.mark.asyncio
async def test_play_in_voice_channel_resets_timeout_when_playback_starts(monkeypatch, tmp_path):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    guild_id = 42
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"fake")
    calls = []
    playback_started = asyncio.Event()
    release_playback = asyncio.Event()
    after_callback = None

    class FakeReceiver:
        def pause(self, clear_buffers=False):
            calls.append(("pause", clear_buffers))

        def resume(self, clear_buffers=False):
            calls.append(("resume", clear_buffers))

    class FakeVoiceClient:
        def is_connected(self):
            return True

        def is_playing(self):
            return False

        def play(self, _source, after=None):
            nonlocal after_callback
            calls.append(("play", None))
            after_callback = after
            playback_started.set()

    async def fake_wait_for(awaitable, timeout):
        await release_playback.wait()
        if after_callback:
            after_callback(None)
        return await awaitable

    adapter._voice_clients[guild_id] = FakeVoiceClient()
    adapter._voice_receivers[guild_id] = FakeReceiver()
    adapter._reset_voice_timeout = lambda gid: calls.append(("reset_timeout", gid))
    monkeypatch.setenv("HERMES_DISCORD_VOICE_CLEAR_BUFFERS_ON_TTS", "true")
    monkeypatch.setenv("HERMES_DISCORD_VOICE_POST_TTS_COOLDOWN_SECONDS", "0")
    monkeypatch.setattr("gateway.platforms.discord.discord.FFmpegPCMAudio", lambda *_a, **_k: object())
    monkeypatch.setattr("gateway.platforms.discord.discord.PCMVolumeTransformer", lambda source, volume=1.0: source)
    monkeypatch.setattr("gateway.platforms.discord.asyncio.wait_for", fake_wait_for)

    task = asyncio.create_task(
        DiscordAdapter.play_in_voice_channel(adapter, guild_id, str(audio_path))
    )
    await playback_started.wait()
    await asyncio.sleep(0)

    assert calls[:3] == [
        ("pause", True),
        ("play", None),
        ("reset_timeout", guild_id),
    ]

    release_playback.set()
    assert await task is True


@pytest.mark.asyncio
async def test_voice_timeout_handler_logs_before_leaving(monkeypatch, caplog):
    from gateway.platforms.discord import DiscordAdapter

    adapter = _make_adapter()
    calls = []

    async def fake_sleep(_seconds):
        return None

    async def fake_leave(guild_id):
        calls.append(("leave", guild_id))

    monkeypatch.setattr("gateway.platforms.discord.asyncio.sleep", fake_sleep)
    adapter.leave_voice_channel = fake_leave
    caplog.set_level("INFO", logger="gateway.platforms.discord")

    await DiscordAdapter._voice_timeout_handler(adapter, guild_id=42)

    assert "Voice inactivity timeout fired for guild 42 after 300s" in caplog.text
    assert calls == [("leave", 42)]
