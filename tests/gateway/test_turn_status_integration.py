"""Real gateway runner, real dispatcher policy, fake model/Discord transport only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


class Transport(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token='fake', typing_indicator=False), Platform.DISCORD)
        self.events = []
        self.gates = [asyncio.Event(), asyncio.Event()]
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.result = SendResult(True, 'final')

    async def connect(self, **kwargs): return True
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {'id': chat_id}
    async def send_typing(self, chat_id, metadata=None): pass
    async def stop_typing(self, chat_id, metadata=None): pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.events.append(('send', content))
        index = len([e for e in self.events if e[0] == 'send']) - 1
        if index < 2:
            self.entered[index].set()
            await self.gates[index].wait()
            return SendResult(True, f'm{index}')
        return self.result

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        self.events.append(('edit', content))
        return SendResult(True, message_id)


def runner_and_agent(monkeypatch, adapter, tools):
    import run_agent
    import gateway.run as gr
    from agent import tool_executor as te

    class Model:
        def __init__(self, **kwargs):
            self.tools = []
            self.session_id = kwargs.get('session_id')
            self._interrupt_requested = False
            self._tool_guardrails = SimpleNamespace(before_call=lambda *_: SimpleNamespace(allows_execution=True))

        @property
        def is_interrupted(self): return self._interrupt_requested

        def interrupt(self, reason): self._interrupt_requested = True

        def run_conversation(self, message, **kwargs):
            ref = te._ToolCallRef('terminal', {}, 'session', 'call', [])
            state = te._ManagedToolResult(None, {}, [], False, True)
            te._dispatch_authorized_once(
                self, state, ref, execute=lambda _: tools.append('started'), scope_block=None,
                display_index=None, begin_execution=None, authorization_gate=None,
            )
            return {'final_response': 'result', 'messages': [], 'api_calls': 1,
                    'completed': not state.blocked, 'interrupted': state.blocked}

    monkeypatch.setattr(run_agent, 'AIAgent', Model)
    monkeypatch.setattr(te, '_pre_tool_block', lambda *_: (None, {}))
    monkeypatch.setattr(te, '_begin_tool_execution', lambda *_: None)
    monkeypatch.setattr(te, '_run_with_activity_heartbeat', lambda _a, _n, f: f())
    monkeypatch.setattr(te, '_blocked_tool_result', lambda *_a, **kw: kw['block_message'])
    monkeypatch.setattr(gr, '_resolve_runtime_agent_kwargs', lambda: {'api_key': 'fake'})
    monkeypatch.setattr(gr, '_load_gateway_config', lambda: {
        'platforms': {'discord': {'extra': {'turn_status': {'enabled': True}}}},
        'display': {'tool_progress': 'off', 'streaming': False},
    })
    runner = object.__new__(gr.GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ''
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(thread_sessions_per_user=False, group_sessions_per_user=False, stt_enabled=False)
    adapter.gateway_runner = runner
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize('partial', [False, True])
async def test_live_gateway_dispatch_waits_for_both_receipts_then_card_precedes_final(tmp_path, monkeypatch, partial):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = Transport()
    if partial:
        adapter.result = SendResult(False, 'chunk1', raw_response={
            'delivery_state': 'partial', 'message_ids': ['chunk1'], 'uncertain': True,
            'delivered_chunks': 1, 'total_chunks': 2,
        })
    tools = []
    runner = runner_and_agent(monkeypatch, adapter, tools)
    source = SessionSource(platform=Platform.DISCORD, chat_id='chat', user_id='u', chat_type='dm')
    event = MessageEvent(text='work', message_type=MessageType.TEXT, source=source, message_id='input')
    key = 'agent:main:discord:dm:chat'

    async def handler(inbound):
        result = await runner._run_agent(message='work', context_prompt='', history=[], source=source,
                                         session_id='session', session_key=key, inbound_message_id='input')
        return await runner._hmwa_deliver_turn_response(
            inbound, source, SimpleNamespace(session_id='session'), key, None,
            result, [], result['final_response'], '', False,
        )

    adapter.set_message_handler(handler)
    task = asyncio.create_task(adapter._process_message_background(event, key))
    await asyncio.wait_for(adapter.entered[0].wait(), 10)
    assert tools == []
    adapter.gates[0].set()
    await asyncio.wait_for(adapter.entered[1].wait(), 10)
    assert tools == []
    adapter.gates[1].set()
    await asyncio.wait_for(task, 15)
    assert tools == ['started']
    assert [e[0] for e in adapter.events] == ['send', 'send', 'edit', 'send', 'edit']
    assert '결과 전달 중' in adapter.events[2][1]
    assert ('확인 필요' if partial else '결과 전달 확인') in adapter.events[-1][1]
    journals = list((tmp_path / 'sessions' / 'turn-status').glob('*.json'))
    assert len(journals) == 1
    data = json.loads(journals[0].read_text())
    assert data['execution_status'] == 'completed'
    assert data['delivery_status'] == ('partial' if partial else 'delivered')
    assert data['result'] == 'result'


@pytest.mark.asyncio
async def test_gateway_queued_and_voice_delivery_use_same_journal(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.turn_status import TurnStatus
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    source = SessionSource(platform=Platform.DISCORD, chat_id='chat', user_id='u', chat_type='dm')
    adapter = Transport()
    adapter.gates = []
    adapter.entered = []
    # Skip initial preflight for this separate result-path fixture.
    adapter.send = AsyncMock(return_value=SendResult(True, 'result'))
    adapter.send_voice = AsyncMock(return_value=SendResult(True, 'voice'))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    state = TurnStatus(adapter=adapter, home=tmp_path, source=source, request_id='request',
                       session_id='session', session_key='key', run_generation=1,
                       loop=asyncio.get_running_loop(), config={}, is_current=lambda: True,
                       is_cancelled=lambda: False)
    event = MessageEvent(text='work', message_type=MessageType.TEXT, source=source)
    event._turn_status = state
    await state.before_result('result')
    await runner._deliver_voice_reply(event, ['fake.ogg'])
    await runner._deliver_queued_first_response('result', source, adapter, deliver_media=False, turn_status=state)
    assert state.data['delivery_status'] == 'delivered'
    roles = [i['role'] for i in state.data['intents']]
    assert roles == ['tts', 'result']
    adapter.send_voice.assert_awaited_once()
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_recovery_holds_unknown_sessions_before_resume(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.turn_status import TurnStatus
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = Transport()
    adapter.send = AsyncMock(side_effect=[SendResult(True, 'ack'), SendResult(True, 'card')])
    source = SessionSource(platform=Platform.DISCORD, chat_id='chat')
    state = TurnStatus(adapter=adapter, home=tmp_path, source=source, request_id='input',
                       session_id='session', session_key='key', run_generation=1,
                       loop=asyncio.get_running_loop(), config={}, is_current=lambda: True,
                       is_cancelled=lambda: False)
    await state.ensure_started()
    await state.close()
    state.data['owner_pid'] = -1
    state._save()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    await runner._recover_turn_status_journals()
    runner._resume_pending_candidates = lambda _: [SimpleNamespace(session_key='key', origin=source)]
    assert runner._schedule_resume_pending_sessions() == 0
    assert runner._turn_status_recovery_holds == {'key'}
    assert runner._turn_status_recovery_reports['default']['unresolved'] == 1
    assert adapter.send.await_count == 2  # Recovery edits the known card; never sends a new one.
    assert len(adapter.events) == 1
    assert adapter.events[0][0] == 'edit'


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery_state', ['unknown', 'partial'])
async def test_base_retry_fallback_does_not_replay_uncertain_receipts(delivery_state):
    adapter = Transport()
    result = SendResult(False, error='forbidden', raw_response={'delivery_state': delivery_state})
    adapter.send = AsyncMock(return_value=result)
    assert await adapter._send_with_retry('chat', 'result') is result
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_base_retry_stops_when_later_attempt_becomes_partial(monkeypatch):
    adapter = Transport()
    partial = SendResult(False, error='forbidden', raw_response={'delivery_state': 'partial', 'message_ids': ['one']})
    adapter.send = AsyncMock(side_effect=[SendResult(False, error='connection reset', retryable=True), partial])
    monkeypatch.setattr('gateway.platforms.base.asyncio.sleep', AsyncMock())
    assert await adapter._send_with_retry('chat', 'result', base_delay=0) is partial
    assert adapter.send.await_count == 2


@pytest.mark.asyncio
async def test_status_card_keeps_tool_log_and_redacts_only_log_copy(tmp_path, monkeypatch):
    import queue
    import gateway.run as gr
    from gateway.turn_context import TurnContext
    from gateway.run_turn_runner import TurnRunner
    queue_for_log = queue.Queue()
    ctx = TurnContext(log_queue=queue_for_log, progress_queue=queue.Queue(), _run_still_current=lambda: True)
    runner = object.__new__(gr.GatewayRunner)
    turn = TurnRunner(runner, ctx)
    turn.turn_status = object()  # Opted-in status UI; no actual transport needed for this callback.
    preview = 'https://example.test/output?token=fixture-private-token&safe=keep'
    turn.progress_callback('tool.started', 'terminal', preview, {'command': preview})
    assert not queue_for_log.empty()
    assert ctx.progress_queue.empty()
    monkeypatch.setattr(gr, '_hermes_home', tmp_path)
    writer = asyncio.create_task(runner._run_agent_write_tool_log(queue_for_log))
    await asyncio.sleep(0)
    writer.cancel()
    await writer
    content = (tmp_path / 'logs' / 'tool_calls.log').read_text(encoding='utf-8')
    assert 'terminal:' in content and 'safe=keep' in content
    assert 'fixture-private-token' not in content
    assert preview.endswith('token=fixture-private-token&safe=keep')


@pytest.mark.asyncio
@pytest.mark.parametrize('communication_enabled', [True, False])
async def test_communication_voice_uses_authorized_text_surface_not_unproved_guild_vc(communication_enabled):
    from gateway.run import GatewayRunner
    adapter = Transport()
    adapter.config.extra['communication'] = {'enabled': communication_enabled}
    adapter.play_in_voice_channel = AsyncMock(return_value=True)
    adapter.is_in_voice_channel = lambda _: True
    adapter.send_voice = AsyncMock(return_value=SendResult(True, 'private-voice-attachment'))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id='private-text', chat_type='channel')
    event = MessageEvent(text='private request', source=source, raw_message=SimpleNamespace(guild_id=123))
    await runner._deliver_voice_reply(event, ['result.ogg'])
    if communication_enabled:
        adapter.play_in_voice_channel.assert_not_awaited()
        adapter.send_voice.assert_awaited_once()
        assert adapter.send_voice.await_args.kwargs['chat_id'] == 'private-text'
    else:
        adapter.play_in_voice_channel.assert_awaited_once_with(123, 'result.ogg')
        adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_discord_partial_receipt_survives_outer_status_timeout(tmp_path, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.turn_status import TurnStatus
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    second_chunk = asyncio.Event()
    settled = asyncio.Event()
    calls = []
    async def channel_send(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return SimpleNamespace(id=111)
        second_chunk.set()
        await asyncio.Future()
    channel = SimpleNamespace(id=10, type=0, guild=None, send=channel_send)
    adapter = DiscordAdapter(PlatformConfig(enabled=True))
    adapter._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
    adapter._record_response_async = AsyncMock(side_effect=lambda _reply, result, *_: result)
    source = SessionSource(platform=Platform.DISCORD, chat_id='10')
    state = TurnStatus(adapter=adapter, home=tmp_path, source=source, request_id='request',
                       session_id='session', session_key='key', run_generation=1,
                       loop=asyncio.get_running_loop(), config={'timeout_seconds': .1},
                       is_current=lambda: True, is_cancelled=lambda: False)
    original_record = state.record_result
    def record(result, intent=None):
        outcome = original_record(result, intent)
        if (getattr(result, 'raw_response', None) or {}).get('message_ids') == ['111']:
            settled.set()
        return outcome
    state.record_result = record
    content = 'X' * 2500
    await state.before_result(content)
    task = asyncio.create_task(state.send_result(lambda: adapter.send('10', content)))
    await asyncio.wait_for(second_chunk.wait(), 2)
    await asyncio.wait_for(task, 2)
    await asyncio.wait_for(settled.wait(), 2)
    await state.finish_delivery()
    assert len(calls) == 2  # No fallback or whole-response resend.
    assert state.data['intents'][0]['message_ids'] == ['111']
    assert state.data['intents'][0]['state'] == 'partial'
    assert state.data['delivery_status'] == 'partial'
