"""Communication policy tests use real adapter imports and fake Discord REST state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.communication_routes import CommunicationPolicy, CommunicationPolicyError, resolve_discord_delivery
from plugins.platforms.discord.adapter import DiscordAdapter, _apply_yaml_config


def channel(cid, *, overwrites=(), parent_id=None, kind=0):
    return SimpleNamespace(id=cid, guild=SimpleNamespace(id=99), parent_id=parent_id,
                           type=kind, _overwrites=list(overwrites), send=AsyncMock(return_value=SimpleNamespace(id=901)))


def overwrite(oid, kind, allow=0, deny=0):
    return SimpleNamespace(id=oid, type=kind, allow=allow, deny=deny)


def policy(**overrides):
    cfg = dict(enabled=True, primary_channels={'personal': '10', 'work': '20', 'development': '30'},
               routes={'1': '10'}, owner_ids=['42'])
    cfg.update(overrides)
    return CommunicationPolicy.from_config(cfg)


def client(*channels):
    data = {c.id: c for c in channels}
    return SimpleNamespace(fetch_channel=AsyncMock(side_effect=lambda cid: data[cid]),
                           get_channel=lambda cid: data[cid], user=SimpleNamespace(id=7))


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))


@pytest.mark.asyncio
async def test_adapter_maps_delivery_without_rewriting_metadata_or_reply_anchor():
    old, new = channel(1), channel(10)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new)
    metadata = {'notify': True}
    result = await adapter.send('1', 'stored result', reply_to='333', metadata=metadata)
    assert result.success
    old.send.assert_not_awaited()
    assert new.send.await_args.kwargs['reference'] is None
    assert metadata == {'notify': True}
    assert result.raw_response['target_id'] == '10'


@pytest.mark.asyncio
async def test_unknown_route_and_stale_acl_fail_closed():
    old, new, other = channel(1), channel(10, overwrites=[overwrite(99, 0, 1024)]), channel(88)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new, other)
    assert not (await adapter.send('1', 'private result')).success
    assert not (await adapter.send('88', 'private result')).success
    new.send.assert_not_awaited()
    other.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_thread_never_silently_becomes_primary_thread():
    old, thread, new = channel(1), channel(2, parent_id=1, kind=11), channel(10)
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(client(old, thread, new), policy(), '1', {'thread_id': '2'})


@pytest.mark.asyncio
async def test_owner_only_proof_uses_roles_not_member_cache():
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    role = SimpleNamespace(id=99, permissions=SimpleNamespace(administrator=False, view_channel=True), tags=None)
    guild = SimpleNamespace(owner_id=42, fetch_roles=AsyncMock(return_value=[role]), members=[])
    transport = client(target)
    transport.fetch_guild = AsyncMock(return_value=guild)
    assert (await resolve_discord_delivery(transport, policy(), '10')).target_id == '10'
    role.permissions.administrator = True
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '10')


@pytest.mark.asyncio
async def test_send_keeps_confirmed_chunk_receipts_and_does_not_retry_timeout():
    target = channel(10)
    target.send.side_effect = [SimpleNamespace(id=901), TimeoutError('remote receipt unknown')]
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = client(target)
    adapter.truncate_message = lambda *_: ['first', 'second', 'third']
    result = await adapter.send('10', 'long result')
    assert not result.success and not result.retryable
    assert result.message_id == '901'
    assert result.raw_response['message_ids'] == ['901']
    assert result.raw_response['delivery_state'] == 'partial'
    assert result.raw_response['uncertain'] is True
    assert target.send.await_count == 2


def test_yaml_bridge_carries_default_off_policy_per_adapter():
    cfg = {'enabled': False, 'primary_channels': {}}
    assert _apply_yaml_config({}, {'communication': cfg})['communication'] == cfg
    assert not CommunicationPolicy.from_config(None).enabled


@pytest.mark.asyncio
async def test_all_payload_doors_block_unknown_readers(tmp_path):
    target = channel(10)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(target)
    builder = AsyncMock()
    assert not (await adapter.edit_message('10', '123', 'sensitive state')).success
    assert not (await adapter._send_prompt('10', None, builder)).success
    local = tmp_path / 'evidence.txt'
    local.write_text('private file')
    assert not (await adapter.send_document('10', str(local))).success
    builder.assert_not_called()
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_input_and_slash_do_not_dispatch():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=7, bot=True))
    adapter.handle_message = AsyncMock()
    message = legacy_message(adapter)
    old = message.channel
    assert await adapter._handle_message(message) is False
    assert '실행하지 않았습니다' in old.send.await_args.args[0]
    adapter._evaluate_slash_authorization = lambda _: (True, None)
    interaction = SimpleNamespace(channel=old, response=SimpleNamespace(send_message=AsyncMock()))
    assert not await adapter._check_slash_authorization(interaction, '/ask')
    assert interaction.response.send_message.await_args.kwargs['ephemeral']
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reader_change_between_chunks_blocks_the_tail():
    old, new = channel(1), channel(10)
    async def first_send(**kwargs):
        new._overwrites.append(overwrite(99, 0, allow=1024))
        return SimpleNamespace(id=901)
    new.send.side_effect = first_send
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new)
    adapter.truncate_message = lambda *_: ['first', 'second']
    result = await adapter.send('1', 'result')
    assert not result.success
    assert result.raw_response['message_ids'] == ['901']
    assert not result.raw_response['uncertain']
    assert new.send.await_count == 1


@pytest.mark.asyncio
async def test_timeout_without_receipt_is_unknown_and_forbidden_is_not_sent():
    target = channel(10)
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = client(target)
    target.send.side_effect = TimeoutError()
    result = await adapter.send('10', 'result')
    assert result.raw_response['delivery_state'] == 'unknown'
    assert result.raw_response['message_ids'] == []
    class Forbidden(Exception):
        status = 403
    target.send.side_effect = Forbidden('denied')
    result = await adapter.send('10', 'result')
    assert result.raw_response['delivery_state'] == 'not_sent'
    assert not result.raw_response['uncertain']


@pytest.mark.asyncio
async def test_public_thread_is_valid_only_under_its_declared_parent():
    old, primary = channel(1), channel(10)
    thread = channel(11, parent_id=10, kind=11)
    transport = client(old, primary, thread)
    target = await resolve_discord_delivery(transport, policy(), '10',
        {'thread_id': '11', 'communication_source_channel_id': '1'})
    assert target.target_id == '11'
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '20',
            {'thread_id': '11', 'communication_source_channel_id': '1'})
    thread.type = 12
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '10',
            {'thread_id': '11', 'communication_source_channel_id': '1'})


def test_real_yaml_loader_preserves_policy(tmp_path):
    import yaml
    from gateway.config import Platform, load_gateway_config
    cfg = policy().as_config()
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'platforms': {'discord': {'extra': {'communication': cfg}}}}))
    loaded = load_gateway_config()
    assert loaded.platforms[Platform.DISCORD].extra['communication'] == cfg


@pytest.mark.asyncio
async def test_partial_receipt_survives_reconnect_without_reexecuting_work(monkeypatch):
    import json
    from gateway.platforms.base import ProcessingOutcome
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL', 'true')
    old, new = channel(1), channel(10)
    new.send.side_effect = [SimpleNamespace(id=901), TimeoutError()]
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new)
    adapter.truncate_message = lambda *_: ['one', 'two']
    result = await adapter.send('1', 'answer', reply_to='333', metadata={'notify': True})
    assert result.raw_response['delivery_state'] == 'partial'
    message = SimpleNamespace(id=333, channel=old, author=SimpleNamespace(id=42), created_at=None)
    adapter._record_discord_message_seen(message, status='discovered')
    adapter._record_discord_processing_complete(SimpleNamespace(raw_message=message), ProcessingOutcome.SUCCESS)
    row = adapter._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT status,response_message_id,last_error FROM discord_messages WHERE message_id=?', ('333',)).fetchone())
    assert row[0] == 'delivery_partial' and row[1] == '901'
    assert json.loads(row[2])['message_ids'] == ['901']
    assert not await adapter._should_backfill_discord_message(message)


@pytest.mark.asyncio
async def test_prompt_callback_rechecks_its_original_source_boundary():
    old, new = channel(1), channel(10)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new)
    view = SimpleNamespace()
    result = await adapter._send_prompt('1', None, lambda channel: ({'content': 'sensitive question'}, view))
    assert result.success
    await view._publication_guard()
    new._overwrites.append(overwrite(99, 0, allow=1024))
    with pytest.raises(Exception):
        await view._publication_guard()


@pytest.mark.asyncio
async def test_standalone_sender_cannot_bypass_enabled_policy():
    from plugins.platforms.discord.adapter import _standalone_send
    result = await _standalone_send(PlatformConfig(extra={'communication': policy().as_config()}), '1', 'secret')
    assert result['delivery_state'] == 'not_sent'
    assert 'live verified' in result['error']


@pytest.mark.asyncio
async def test_other_bot_role_does_not_prove_owner_only():
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024), overwrite(88, 0, allow=1024)])
    everyone = SimpleNamespace(id=99, permissions=SimpleNamespace(administrator=False, view_channel=True), tags=None)
    other_bot = SimpleNamespace(id=88, permissions=SimpleNamespace(administrator=False, view_channel=True), tags=SimpleNamespace(bot_id=8))
    guild = SimpleNamespace(owner_id=42, fetch_roles=AsyncMock(return_value=[everyone, other_bot]))
    transport = client(target)
    transport.fetch_guild = AsyncMock(return_value=guild)
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '10')


@pytest.mark.asyncio
async def test_forum_partial_timeout_preserves_starter_receipt_without_tail_retry():
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    tail = SimpleNamespace(id=11, send=AsyncMock(side_effect=TimeoutError()))
    forum = SimpleNamespace(id=10, create_thread=AsyncMock(return_value=SimpleNamespace(
        thread=tail, message=SimpleNamespace(id=901))))
    adapter.truncate_message = lambda *_: ['one', 'two', 'three']
    result = await adapter._send_to_forum(forum, 'long result')
    assert not result.success
    assert result.raw_response['message_ids'] == ['901']
    assert result.raw_response['delivery_state'] == 'partial'
    assert result.raw_response['uncertain']
    assert tail.send.await_count == 1


@pytest.mark.asyncio
async def test_outer_timeout_keeps_first_chunk_receipt():
    import asyncio
    target = channel(10)
    attempts = 0
    async def send(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return SimpleNamespace(id=901)
        await asyncio.Future()
    target.send.side_effect = send
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = client(target)
    adapter.truncate_message = lambda *_: ['one', 'two']
    result = await asyncio.wait_for(adapter.send('10', 'answer'), timeout=.03)
    assert not result.success
    assert result.raw_response['message_ids'] == ['901']
    assert result.raw_response['delivery_state'] == 'partial'
    assert result.raw_response['uncertain']
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('threaded', [False, True])
async def test_primary_source_metadata_cannot_self_certify_readers(threaded):
    primary = channel(10)
    thread = channel(11, parent_id=10, kind=11)
    transport = client(primary, thread)
    everyone = SimpleNamespace(id=99, permissions=SimpleNamespace(administrator=False, view_channel=True), tags=None)
    transport.fetch_guild = AsyncMock(return_value=SimpleNamespace(
        owner_id=42, fetch_roles=AsyncMock(return_value=[everyone])))
    metadata = {'communication_source_channel_id': '11' if threaded else '10'}
    if threaded:
        metadata['thread_id'] = '11'
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '10', metadata)


@pytest.mark.asyncio
async def test_image_batch_reader_change_keeps_receipt_and_blocks_fallback(tmp_path):
    old, new = channel(1), channel(10)
    async def first_send(**kwargs):
        new._overwrites.append(overwrite(99, 0, allow=1024))
        return SimpleNamespace(id=901)
    new.send.side_effect = first_send
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(old, new)
    images = []
    for n in range(11):
        image = tmp_path / f'{n}.png'
        image.write_bytes(b'fixture')
        images.append((image.as_uri(), 'private caption'))
    result = await adapter.send_multiple_images('1', images)
    assert not result.success
    assert result.raw_response['message_ids'] == ['901']
    assert result.raw_response['delivery_state'] == 'partial'
    assert not result.raw_response['uncertain']
    assert new.send.await_count == 1


def role(rid, *, view=False, admin=False, bot_id=None, managed=False):
    return SimpleNamespace(id=rid, permissions=SimpleNamespace(administrator=admin, view_channel=view),
                           tags=SimpleNamespace(bot_id=bot_id) if bot_id else None, managed=managed)


def permission_client(target, roles, *sources):
    transport = client(target, *sources)
    transport.fetch_guild = AsyncMock(return_value=SimpleNamespace(
        owner_id=42, fetch_roles=AsyncMock(return_value=roles)))
    return transport


@pytest.mark.asyncio
async def test_role_base_view_without_everyone_deny_is_not_private():
    target = channel(10)
    transport = permission_client(target, [role(99), role(88, view=True, bot_id=8, managed=True)])
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(), '10')


@pytest.mark.asyncio
async def test_role_base_view_is_blocked_by_everyone_deny():
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    transport = permission_client(target, [role(99), role(88, view=True)])
    assert (await resolve_discord_delivery(transport, policy(), '10')).target_id == '10'


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['valid', 'revoked', 'wrong_tag', 'unmanaged', 'missing_tag', 'unknown_role', 'member_only'])
async def test_existing_admin_bot_requires_current_managed_role_evidence(variant):
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024), overwrite(8, 1, allow=1024)])
    bot_role = role(88, view=True, admin=variant != 'revoked', bot_id=9 if variant == 'wrong_tag' else 8,
                    managed=variant != 'unmanaged')
    if variant == 'missing_tag':
        bot_role.tags = None
    roles = [role(99), bot_role] if variant != 'member_only' else [role(99)]
    if variant == 'unknown_role':
        target._overwrites.append(overwrite(404, 0, allow=1024))
    transport = permission_client(target, roles)
    cfg = policy(existing_admin_bot_ids=['8'])
    if variant == 'valid':
        assert (await resolve_discord_delivery(transport, cfg, '10')).target_id == '10'
        bot_role.permissions.administrator = False
        with pytest.raises(CommunicationPolicyError):
            await resolve_discord_delivery(transport, cfg, '10')
    else:
        with pytest.raises(CommunicationPolicyError):
            await resolve_discord_delivery(transport, cfg, '10')


@pytest.mark.asyncio
@pytest.mark.parametrize('admin_bot', [True, False])
async def test_configured_admin_bot_in_target_guild_cannot_read_cross_guild_source(admin_bot):
    old = channel(1)
    old.guild = SimpleNamespace(id=55)
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    roles = [role(99), role(88, admin=True, bot_id=8, managed=True)] if admin_bot else [role(99)]
    transport = permission_client(target, roles, old)
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(existing_admin_bot_ids=['8'] if admin_bot else []), '1')


def legacy_message(adapter, *, mentioned=True):
    import datetime
    from plugins.platforms.discord.adapter import discord
    old = channel(1)
    old.name, old.topic = 'legacy', None
    old.guild.name = 'guild'
    return SimpleNamespace(id=333, channel=old, guild=old.guild, content='<@7> run task' if mentioned else 'casual chat',
        author=SimpleNamespace(id=42, bot=False, name='owner', display_name='owner'),
        mentions=[adapter._client.user] if mentioned else [], attachments=[], message_snapshots=[],
        type=discord.MessageType.default, reference=None, created_at=datetime.datetime.now(datetime.timezone.utc))


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['ignored', 'not_allowed', 'no_mention', 'request'])
async def test_legacy_redirect_preserves_live_silence_gates(monkeypatch, gate):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=7, bot=True))
    adapter._ready_event.set()
    adapter._is_allowed_user = lambda *a, **kw: True
    adapter.handle_message = AsyncMock()
    monkeypatch.setenv('DISCORD_REQUIRE_MENTION', 'true')
    monkeypatch.setenv('DISCORD_FREE_RESPONSE_CHANNELS', '')
    if gate == 'ignored':
        monkeypatch.setenv('DISCORD_IGNORED_CHANNELS', '1')
    if gate == 'not_allowed':
        monkeypatch.setenv('DISCORD_ALLOWED_CHANNELS', '10')
    message = legacy_message(adapter, mentioned=gate != 'no_mention')
    assert not await adapter._dispatch_discord_message(message)
    assert message.channel.send.await_count == (1 if gate == 'request' else 0)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_unproved_vc_falls_back_to_guarded_attachment():
    from gateway.platforms.base import SendResult
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    vc_channel = channel(50, kind=2)
    adapter._client = client(channel(10), vc_channel)
    vc = SimpleNamespace(channel=vc_channel, is_connected=lambda: True, play=AsyncMock())
    adapter._voice_clients[99] = vc
    adapter._voice_text_channels[99] = 10
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id='901'))
    result = await adapter.play_tts('10', '/fixture.ogg', metadata={'communication_source_channel_id': '10'})
    assert result.success
    adapter.send_voice.assert_awaited_once()
    vc.play.assert_not_called()


@pytest.mark.asyncio
async def test_direct_vc_play_without_source_does_not_start_audio():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = client(channel(50, kind=2))
    vc = SimpleNamespace(channel=channel(50, kind=2), is_connected=lambda: True, play=AsyncMock())
    adapter._voice_clients[99] = vc
    assert not await adapter.play_in_voice_channel(99, '/fixture.ogg')
    vc.play.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('granted_by', ['role', 'member'])
async def test_non_admin_reader_grants_do_not_inherit_admin_bot_exception(granted_by):
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    roles = [role(99), role(88, admin=True, bot_id=8, managed=True), role(89, bot_id=9, managed=True)]
    target._overwrites.append(overwrite(89 if granted_by == 'role' else 9, 0 if granted_by == 'role' else 1, allow=1024))
    transport = permission_client(target, roles)
    with pytest.raises(CommunicationPolicyError):
        await resolve_discord_delivery(transport, policy(existing_admin_bot_ids=['8']), '10')


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke_during_decode', [True, False])
async def test_vc_mixer_requires_fresh_restricted_readers_before_start(monkeypatch, revoke_during_decode):
    from unittest.mock import MagicMock
    import plugins.platforms.discord.adapter as discord_platform
    target = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    voice = channel(50, overwrites=[overwrite(99, 0, deny=1024)], kind=2)
    admin = role(88, admin=True, bot_id=8, managed=True)
    transport = permission_client(target, [role(99), admin], voice)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy(existing_admin_bot_ids=['8']).as_config()}))
    adapter._client = transport
    adapter._voice_clients[99] = SimpleNamespace(channel=voice, is_connected=lambda: True)
    mixer = SimpleNamespace(play_speech=MagicMock(), speech_active=False)
    adapter._voice_mixers[99] = mixer
    adapter._cancel_voice_timeout = MagicMock()
    adapter._reset_voice_timeout = MagicMock()
    adapter._playback_timeout_for_audio = AsyncMock(return_value=1)
    adapter._lead_silence_bytes = lambda: b''
    def decode(_):
        if revoke_during_decode:
            admin.permissions.administrator = False
        return b'pcm'
    monkeypatch.setattr(discord_platform, '_voice_mixer_module', lambda: SimpleNamespace(decode_to_pcm=decode))
    assert await adapter.play_in_voice_channel(99, '/fixture.ogg', chat_id='10') is not revoke_during_decode
    assert mixer.play_speech.call_count == (0 if revoke_during_decode else 1)
    assert transport.fetch_guild.await_count >= 2


@pytest.mark.asyncio
async def test_tts_real_attachment_path_preserves_voice_when_vc_is_broader(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import discord
    monkeypatch.setattr(discord.http, 'Route', lambda method, path, **params: SimpleNamespace(channel_id=params['channel_id']))
    primary = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    voice = channel(50, kind=2)
    transport = permission_client(primary, [role(99, view=True)], voice)
    transport.http = SimpleNamespace(request=AsyncMock(return_value={'id': '901'}))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = transport
    vc = SimpleNamespace(channel=voice, is_connected=lambda: True, play=AsyncMock())
    adapter._voice_clients[99] = vc
    adapter._voice_text_channels[99] = 10
    audio = tmp_path / 'voice.ogg'
    audio.write_bytes(b'OggS-fixture')
    result = await adapter.play_tts('10', str(audio), metadata={'communication_source_channel_id': '10'})
    assert result.success and result.message_id == '901'
    vc.play.assert_not_called()
    transport.http.request.assert_awaited_once()
    assert transport.http.request.await_args.args[0].channel_id == 10


@pytest.mark.asyncio
async def test_voice_metadata_private_thread_does_not_inherit_parent_audience():
    primary = channel(10, overwrites=[overwrite(99, 0, deny=1024)])
    voice = channel(50, overwrites=[overwrite(99, 0, deny=1024)], kind=2)
    private_thread = channel(11, parent_id=10, kind=12)
    transport = permission_client(primary, [role(99, view=True)], voice, private_thread)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': policy().as_config()}))
    adapter._client = transport
    adapter._voice_clients[99] = SimpleNamespace(channel=voice)
    assert not await adapter._voice_publication_allowed(99, chat_id='10', metadata={'thread_id': '11'})


def shared_participant_fixture(*, sender_id=7, surface_id=10, participant='valid', primary_type=0):
    """Two conversation bots plus an already global admin bot; PM could not read old source."""
    target = channel(surface_id, kind=primary_type,
                     overwrites=[overwrite(99, 0, deny=1024), overwrite(88, 0, allow=1024)])
    source = channel(1, overwrites=[overwrite(99, 0, deny=1024), overwrite(88, 0, deny=1024)])
    participant_role = role(88, bot_id=8, managed=True)
    if participant == 'wrong_tag':
        participant_role.tags.bot_id = 18
    if participant == 'unmanaged':
        participant_role.managed = False
    if participant == 'nonbot':
        participant_role.tags = None
    roles = [role(99, view=True), role(77, admin=True, bot_id=7, managed=True),
             role(66, admin=True, bot_id=9, managed=True)]
    if participant != 'missing':
        roles.append(participant_role)
    transport = permission_client(target, roles, source)
    transport.user = SimpleNamespace(id=sender_id)
    cfg = policy(shared_participant_bot_ids=['7', '8'], existing_admin_bot_ids=['9'],
                 private_output_channels=['40'])
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': cfg.as_config()}))
    adapter._client = transport
    return adapter, target, source, participant_role


@pytest.mark.asyncio
@pytest.mark.parametrize('sender_id', [7, 8])
@pytest.mark.parametrize('surface_id', [10, 20, 30])
async def test_shared_participant_all_three_direct_surfaces_for_both_bots(sender_id, surface_id):
    adapter, target, _, _ = shared_participant_fixture(sender_id=sender_id, surface_id=surface_id)
    result = await adapter.send(str(surface_id), 'approved shared answer')
    assert result.success
    target.send.assert_awaited_once()
    assert result.raw_response['target_id'] == str(surface_id)


@pytest.mark.asyncio
async def test_shared_participant_legacy_excluded_pm_source_can_publish_authorized_answer():
    adapter, target, source, _ = shared_participant_fixture()
    metadata = {'notify': True}
    result = await adapter.send('1', 'answer based on unchanged old context', reply_to='333', metadata=metadata)
    assert result.success
    source.send.assert_not_awaited()
    assert target.send.await_args.kwargs['reference'] is None
    assert metadata == {'notify': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('sender_id', [7, 8])
async def test_shared_participant_public_child_thread_uses_selected_primary_surface(sender_id):
    adapter, primary, _, _ = shared_participant_fixture(sender_id=sender_id)
    thread = channel(11, parent_id=10, kind=11)
    previous_fetch = adapter._client.fetch_channel.side_effect
    adapter._client.fetch_channel.side_effect = lambda cid: thread if cid == 11 else previous_fetch(cid)
    result = await adapter.send('10', 'thread answer', metadata={'thread_id': '11'})
    assert result.success
    primary.send.assert_not_awaited()
    thread.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['missing', 'wrong_tag', 'unmanaged', 'nonbot'])
async def test_shared_participant_requires_fresh_managed_bot_role(evidence):
    adapter, target, _, _ = shared_participant_fixture(participant=evidence)
    # An optimistic channel/guild cache must not override the REST role evidence.
    target.guild.roles = [role(88, bot_id=8, managed=True)]
    result = await adapter.send('10', 'sensitive answer')
    assert not result.success
    target.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['private_output', 'voice', 'foreign_guild', 'forum'])
async def test_shared_participant_exception_does_not_expand_to_other_surfaces(surface):
    from gateway.communication_routes import verify_discord_voice_audience
    adapter, target, source, _ = shared_participant_fixture(
        surface_id=40 if surface == 'private_output' else 10,
        primary_type=15 if surface == 'forum' else 0)
    if surface == 'voice':
        target.id, target.type = 50, 2
        adapter._client.fetch_channel.side_effect = lambda cid: target if cid == 50 else source
        assert not await verify_discord_voice_audience(adapter._client, adapter._communication_policy(), 50, '1')
    else:
        if surface == 'foreign_guild':
            source.guild = SimpleNamespace(id=55)
        result = await adapter.send('1' if surface == 'foreign_guild' else str(target.id), 'answer')
        assert not result.success
    target.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('new_reader', ['role', 'member', 'everyone', 'unknown_role'])
async def test_shared_participant_reader_change_is_blocked(new_reader):
    adapter, target, _, _ = shared_participant_fixture()
    assert (await adapter.send('10', 'first answer')).success
    if new_reader == 'role':
        guild = adapter._client.fetch_guild.return_value
        guild.fetch_roles.return_value.append(role(89))
        target._overwrites.append(overwrite(89, 0, allow=1024))
    elif new_reader == 'member':
        target._overwrites.append(overwrite(404, 1, allow=1024))
    elif new_reader == 'unknown_role':
        target._overwrites.append(overwrite(404, 0, allow=1024))
    else:
        target._overwrites = [overwrite(99, 0, allow=1024), overwrite(88, 0, allow=1024)]
    assert not (await adapter.send('10', 'second answer')).success
    assert target.send.await_count == 1


@pytest.mark.asyncio
async def test_shared_participant_stale_role_blocks_edit_and_attachment(tmp_path):
    adapter, target, _, participant_role = shared_participant_fixture()
    message = SimpleNamespace(edit=AsyncMock())
    target.get_partial_message = lambda mid: message
    target.send.return_value.attachments = [SimpleNamespace(filename='fixture.txt')]
    attachment = tmp_path / 'fixture.txt'
    attachment.write_text('approved result')
    assert (await adapter.edit_message('10', '901', 'approved status')).success
    assert (await adapter.send_document('10', str(attachment))).success
    participant_role.tags.bot_id = 18
    assert not (await adapter.edit_message('10', '901', 'new status')).success
    assert not (await adapter.send_document('10', str(attachment))).success
    message.edit.assert_awaited_once()
    target.send.assert_awaited_once()


def test_shared_participant_ids_are_validated_and_round_trip():
    cfg = policy(shared_participant_bot_ids=['7', '8'])
    assert cfg.shared_participant_bot_ids == frozenset({'7', '8'})
    assert cfg.as_config()['shared_participant_bot_ids'] == ['7', '8']
    assert not policy().shared_participant_bot_ids
    with pytest.raises(CommunicationPolicyError):
        policy(shared_participant_bot_ids=['not-a-bot-id'])


@pytest.mark.asyncio
async def test_shared_participant_default_off_preserves_original_transport():
    cfg = policy(enabled=False, shared_participant_bot_ids=['7', '8'])
    target = channel(88)
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={'communication': cfg.as_config()}))
    adapter._client = client(target)
    result = await adapter.send('88', 'legacy output')
    assert result.success
    target.send.assert_awaited_once()
    adapter._client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_participant_metadata_cannot_authorize_an_unconfigured_bot():
    adapter, target, _, _ = shared_participant_fixture()
    adapter.config.extra['communication'] = policy(existing_admin_bot_ids=['9']).as_config()
    result = await adapter.send('10', 'answer', metadata={
        'shared_participant_bot_ids': ['8'], 'communication_audience_verified': True,
    })
    assert not result.success
    target.send.assert_not_awaited()


def raw_member(uid, role_ids=(), *, bot=False):
    return {'user': {'id': str(uid), 'bot': bot}, 'roles': [str(rid) for rid in role_ids]}


def common_admin_fixture(*, sender_id=7, surface_id=10):
    adapter, target, source, participant = shared_participant_fixture(sender_id=sender_id, surface_id=surface_id)
    target._overwrites = [overwrite(99, 0, deny=1024), overwrite(42, 1, allow=1024),
                          overwrite(7, 1, allow=1024), overwrite(8, 1, allow=1024)]
    common_admin = role(55, admin=True)
    roles = [role(99, view=True), common_admin, role(77, bot_id=7, managed=True),
             role(66, bot_id=9, managed=True), participant]
    guild = adapter._client.fetch_guild.return_value
    guild.fetch_roles.return_value = roles
    # The owner has no Admin role. Only default and finance hold the shared Admin role.
    members = [raw_member(42), raw_member(7, [77, 55], bot=True),
               raw_member(8, [88], bot=True), raw_member(9, [66, 55], bot=True)]
    adapter._client.http = SimpleNamespace(get_members=AsyncMock(return_value=members))
    adapter.config.extra['communication'] = policy(
        shared_participant_bot_ids=['7', '8'], existing_admin_bot_ids=['7', '9']).as_config()
    return adapter, target, source, roles, members


@pytest.mark.asyncio
@pytest.mark.parametrize('sender_id', [7, 8])
@pytest.mark.parametrize('surface_id', [10, 20, 30])
async def test_common_admin_actual_four_member_fixture_allows_both_bots(sender_id, surface_id):
    adapter, target, _, _, _ = common_admin_fixture(sender_id=sender_id, surface_id=surface_id)
    assert (await adapter.send(str(surface_id), 'approved shared result')).success
    target.send.assert_awaited_once()
    adapter._client.http.get_members.assert_awaited_with(99, 1000, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['unknown_admin', 'unknown_view', 'revoked_admin', 'nonbot_admin',
                                  'missing_own_role', 'unknown_role', 'duplicate', 'missing_owner', 'failed_fetch', 'missing_bot_identity', 'cached_member_roles'])
async def test_common_admin_fresh_membership_failures_hold(case):
    adapter, target, _, roles, members = common_admin_fixture()
    if case == 'unknown_admin':
        members.append(raw_member(404, [55]))
    elif case == 'unknown_view':
        roles.append(role(56))
        target._overwrites.append(overwrite(56, 0, allow=1024))
        members.append(raw_member(404, [56]))
    elif case == 'revoked_admin':
        members[3]['roles'] = ['66']
    elif case == 'nonbot_admin':
        members[3]['user']['bot'] = False
    elif case == 'missing_own_role':
        members[3]['roles'] = ['55']
    elif case == 'missing_bot_identity':
        roles[3].managed = False
    elif case == 'cached_member_roles':
        adapter._client.fetch_guild.return_value.members = [
            SimpleNamespace(id=9, roles=[role(55, admin=True)])]
        members[3]['roles'] = ['66']
    elif case == 'unknown_role':
        members[3]['roles'].append('404')
    elif case == 'duplicate':
        members.append(members[0])
    elif case == 'missing_owner':
        members.pop(0)
    else:
        adapter._client.http.get_members.side_effect = RuntimeError('403 members unavailable')
    assert not (await adapter.send('10', 'sensitive answer')).success
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_common_admin_incomplete_full_page_does_not_use_cache_or_next_page():
    adapter, target, _, _, members = common_admin_fixture()
    guild = adapter._client.fetch_guild.return_value
    guild.members = members  # A matching cached list cannot certify the REST page.
    adapter._client.http.get_members.return_value = members + [raw_member(1000 + n) for n in range(996)]
    assert not (await adapter.send('10', 'answer')).success
    adapter._client.http.get_members.assert_awaited_once_with(99, 1000, None)
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_common_admin_unmanaged_view_role_is_allowed_only_for_actual_approved_holders():
    adapter, target, _, roles, members = common_admin_fixture()
    roles.append(role(56, view=True))
    target._overwrites.append(overwrite(56, 0, allow=1024))
    members[0]['roles'].append('56')
    assert (await adapter.send('10', 'owner-only role result')).success
    members.append(raw_member(404, [56]))
    assert not (await adapter.send('10', 'later result')).success
    assert target.send.await_count == 1


@pytest.mark.asyncio
async def test_common_admin_everyone_administrator_is_never_allowed_by_small_member_count():
    adapter, target, _, roles, _ = common_admin_fixture()
    roles[0].permissions.administrator = True
    assert not (await adapter.send('10', 'answer')).success
    target.send.assert_not_awaited()
