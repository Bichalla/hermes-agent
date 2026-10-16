"""Transport receipts, rather than scheduled UI callbacks, authorize tool execution."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.config import Platform
from gateway.turn_context import TurnContext
from gateway.turn_status import TurnStatus, TurnStatusBlocked, create_turn_status


class Adapter:
    def __init__(self, results=None):
        self.calls = []
        self.edits = []
        self.results = list(results or [])
        self.gate = None
        self.entered = asyncio.Event()

    async def send(self, chat_id, content, **kwargs):
        self.calls.append(content)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        value = self.results.pop(0) if self.results else SendResult(True, str(len(self.calls)))
        if isinstance(value, BaseException):
            raise value
        return value

    async def edit_message(self, chat_id, message_id, content, **kwargs):
        self.edits.append((message_id, content))
        return SendResult(True, message_id)


def status(tmp_path, adapter=None, **kwargs):
    return TurnStatus(
        adapter=adapter or Adapter(), home=tmp_path,
        source=SessionSource(platform=Platform.DISCORD, chat_id='chat', thread_id='thread', profile='work'),
        request_id='request', session_id='session', session_key='key', run_generation=1,
        loop=asyncio.get_running_loop(), config={'timeout_seconds': 2, 'max_attempts': 2},
        is_current=lambda: True, is_cancelled=lambda: False, **kwargs,
    )


@pytest.mark.asyncio
async def test_receipts_are_a_barrier_for_no_commentary_and_concurrent_tools(tmp_path):
    adapter = Adapter()
    adapter.gate = asyncio.Event()
    state = status(tmp_path, adapter)
    started = []

    def execute():
        state.before_tool_execution('call', 'terminal', {})
        started.append(True)

    tasks = [asyncio.create_task(asyncio.to_thread(execute)) for _ in range(2)]
    await adapter.entered.wait()
    assert not started
    adapter.gate.set()
    await asyncio.gather(*tasks)
    assert len(started) == 2
    assert len(adapter.calls) == 2  # one acknowledgement, one distinct card
    assert state.data['execution_status'] == 'running'
    assert state.data['card_message_id'] == '2'
    await state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('result,attempts', [
    (SendResult(False, error='forbidden', raw_response={'delivery_state': 'not_sent'}), 2),
    (SendResult(False, error='timeout', raw_response={'delivery_state': 'unknown', 'uncertain': True}), 1),
    (SendResult(True), 1),
])
async def test_preflight_failure_never_authorizes_work(tmp_path, result, attempts):
    adapter = Adapter([result, result])
    state = status(tmp_path, adapter)
    with pytest.raises(TurnStatusBlocked):
        await asyncio.to_thread(state.before_tool_execution, 'call', 'terminal', {})
    assert len(adapter.calls) == attempts
    assert state.data['execution_status'] == 'not_started'
    await state.close()


@pytest.mark.asyncio
async def test_card_failure_keeps_ack_receipt_and_blocks_work(tmp_path):
    adapter = Adapter([SendResult(True, 'ack'), SendResult(False, raw_response={'delivery_state': 'unknown'})])
    state = status(tmp_path, adapter)
    with pytest.raises(TurnStatusBlocked):
        await state.ensure_started()
    assert state.data['intents'][0]['message_ids'] == ['ack']
    assert state.data['card_message_id'] is None
    assert len(adapter.calls) == 2
    await state.close()


@pytest.mark.asyncio
async def test_reopened_attempting_send_becomes_unknown_without_resending(tmp_path):
    state = status(tmp_path)
    state._claim()
    state.begin_component('card')
    reopened = status(tmp_path)
    with pytest.raises(TurnStatusBlocked):
        await reopened.ensure_started()
    assert reopened.data['intents'][0]['state'] == 'unknown'
    assert not reopened.adapter.calls
    await reopened.close()


@pytest.mark.asyncio
async def test_direct_answer_has_no_ack_or_status_card(tmp_path):
    state = status(tmp_path)
    await state.before_result('A short answer')
    result = await state.send_result(lambda: state.adapter.send('chat', 'A short answer'))
    await state.finish_delivery()
    assert result.success
    assert state.adapter.calls == ['A short answer']
    assert not state.adapter.edits
    assert state.data['delivery_status'] == 'delivered'


@pytest.mark.asyncio
async def test_result_partial_is_preserved_and_not_retried(tmp_path):
    adapter = Adapter()
    state = status(tmp_path, adapter)
    await state.ensure_started()
    await state.before_result('important result')
    adapter.results = [SendResult(False, 'chunk1', raw_response={
        'delivery_state': 'partial', 'message_ids': ['chunk1'], 'uncertain': True,
        'delivered_chunks': 1, 'total_chunks': 2,
    })]
    await state.send_result(lambda: adapter.send('chat', 'important result'))
    await state.finish_delivery()
    assert len(adapter.calls) == 3
    assert state.data['delivery_status'] == 'partial'
    persisted = json.loads(state.path.read_text())
    assert persisted['result'] == 'important result'
    assert persisted['intents'][-2]['message_ids'] == ['chunk1']
    assert '확인 필요' in adapter.edits[-1][1]


def test_factory_default_off_and_internal_events_do_not_create_journals(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    ctx = TurnContext(source=SessionSource(platform=Platform.DISCORD, chat_id='chat'), user_config={})
    assert create_turn_status(ctx, Adapter(), lambda: False) is None
    ctx.user_config = {'platforms': {'discord': {'extra': {'turn_status': {'enabled': True}}}}}
    ctx.persist_user_display_kind = 'internal_notification'
    assert create_turn_status(ctx, Adapter(), lambda: False) is None
    assert not (tmp_path / 'sessions').exists()


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_ack_never_starts_work(tmp_path):
    adapter = Adapter()
    adapter.gate = asyncio.Event()
    state = status(tmp_path, adapter)
    cancelled = False
    state.is_cancelled = lambda: cancelled
    task = asyncio.create_task(asyncio.to_thread(state.before_tool_execution, 'call', 'terminal', {}))
    await adapter.entered.wait()
    cancelled = True
    with pytest.raises(TurnStatusBlocked):
        await task
    assert len(adapter.calls) == 1
    assert state.data['execution_status'] == 'not_started'
    assert state.data['intents'][0]['state'] == 'unknown'
    await state.close()


@pytest.mark.asyncio
async def test_virtual_clock_timeout_is_unknown_and_has_no_resend(tmp_path, monkeypatch):
    adapter = Adapter()
    adapter.gate = asyncio.Event()
    state = status(tmp_path, adapter)
    clock = SimpleNamespace(now=0.0)
    state.loop = SimpleNamespace(time=lambda: clock.now)
    async def advance_wait(tasks, timeout):
        await asyncio.sleep(0)
        clock.now += timeout + state.timeout
        return set(), tasks
    monkeypatch.setattr(asyncio, 'wait', advance_wait)
    with pytest.raises(TurnStatusBlocked):
        await state.ensure_started()
    assert clock.now > state.timeout
    assert len(adapter.calls) == 1
    assert state.data['intents'][0]['state'] == 'unknown'
    await state.close()


@pytest.mark.asyncio
async def test_confirmed_failure_retry_can_deliver_without_partial_status(tmp_path):
    adapter = Adapter([SendResult(False, raw_response={'delivery_state': 'not_sent'}), SendResult(True, 'retry')])
    state = status(tmp_path, adapter)
    await state.before_result('result')
    await state.send_result(lambda: adapter.send('chat', 'result'))
    await state.finish_delivery()
    assert len(adapter.calls) == 2
    assert state.data['delivery_status'] == 'delivered'
    assert [i['state'] for i in state.data['intents']] == ['not_sent', 'delivered']


@pytest.mark.asyncio
async def test_status_edit_failure_does_not_drop_result(tmp_path):
    adapter = Adapter()
    async def broken_edit(*args, **kwargs):
        raise TimeoutError('lost edit acknowledgement')
    adapter.edit_message = broken_edit
    state = status(tmp_path, adapter)
    await state.ensure_started()
    await state.execution_finished({'completed': True})
    await state.before_result('retained')
    await state.send_result(lambda: adapter.send('chat', 'retained'))
    await state.finish_delivery()
    assert adapter.calls[-2] == 'retained'
    assert '상태 카드 갱신' in adapter.calls[-1]
    assert len(adapter.calls) == 4  # ACK, card, result, exactly one journaled fallback notice.
    assert state.data['card_update_unresolved'] is True
    assert state.data['delivery_status'] == 'delivered'
    assert state.data['result'] == 'retained'
    assert [i['state'] for i in state.data['intents'] if i['role'] == 'edit'] == ['unknown', 'unknown']
    # Display-only recovery repairs the known card without holding work replay
    # for an otherwise completed/delivered session or sending another notice.
    from gateway.turn_status import recover_turn_statuses
    state.data['owner_pid'] = -1
    state._save()
    recovery_adapter = Adapter()
    report = await recover_turn_statuses(tmp_path, recovery_adapter, profile='work')
    assert report['session_keys'] == [] and report['unresolved'] == 0
    assert not recovery_adapter.calls
    repaired = json.loads(state.path.read_text())
    assert repaired['card_update_unresolved'] is False
    assert repaired['delivery_status'] == 'delivered' and repaired['phase'] == 'finished'


@pytest.mark.asyncio
async def test_independent_requests_do_not_share_cards_and_session_rotation_does_not_replay(tmp_path):
    adapter = Adapter()
    first = status(tmp_path, adapter)
    # Request identity is fixed at construction; use a second constructor for a distinct input.
    second = TurnStatus(adapter=adapter, home=tmp_path, source=first.source, request_id='new-request',
                        session_id='session', session_key='key', run_generation=1,
                        loop=asyncio.get_running_loop(), config={}, is_current=lambda: True,
                        is_cancelled=lambda: False)
    await asyncio.gather(first.ensure_started(), second.ensure_started())
    assert first.path != second.path
    assert first.data['card_message_id'] != second.data['card_message_id']
    rotated = TurnStatus(adapter=adapter, home=tmp_path, source=first.source, request_id='request',
                         session_id='new-compressed-session', session_key='key', run_generation=2,
                         loop=asyncio.get_running_loop(), config={}, is_current=lambda: True,
                         is_cancelled=lambda: False)
    with pytest.raises(TurnStatusBlocked):
        await rotated.ensure_started()
    assert len(adapter.calls) == 4
    await first.close()
    await second.close()
    await rotated.close()


@pytest.mark.asyncio
async def test_boot_recovery_updates_only_existing_card_and_holds_auto_resume(tmp_path):
    from gateway.turn_status import recover_turn_statuses
    state = status(tmp_path)
    await state.ensure_started()
    await state.close()
    state.data['owner_pid'] = -1
    state.begin_component('result')
    state._save()
    adapter = Adapter()
    report = await recover_turn_statuses(tmp_path, adapter, profile='work')
    assert report['unresolved'] == 1
    assert report['session_keys'] == ['key']
    assert not adapter.calls
    assert len(adapter.edits) == 1
    assert '확인 필요' in adapter.edits[0][1]
    data = json.loads(state.path.read_text())
    assert data['execution_status'] == 'unknown'
    assert data['delivery_status'] == 'unknown'


@pytest.mark.asyncio
async def test_boot_recovery_refuses_symlink_and_records_scan_overflow(tmp_path):
    from gateway.turn_status import recover_turn_statuses
    state = status(tmp_path)
    state._claim()
    state.data['owner_pid'] = -1
    state._save()
    report = await recover_turn_statuses(tmp_path, Adapter(), profile='work', max_files=0)
    assert report['overflow']
    outside = tmp_path / 'outside.json'
    state.path.replace(outside)
    state.path.symlink_to(outside)
    report = await recover_turn_statuses(tmp_path, Adapter(), profile='work')
    assert report['invalid'] == 1
    assert json.loads(outside.read_text())['phase'] == 'received'


def test_runtime_delivery_owner_is_not_serialized_as_event_or_context():
    from dataclasses import asdict
    from gateway.platforms.event import MessageEvent
    class PrivateOwner:
        def __deepcopy__(self, memo):
            raise AssertionError('delivery controller must not escape through event serialization')
    event = MessageEvent(text='message')
    ctx = TurnContext()
    event._turn_status = ctx._turn_status = PrivateOwner()
    assert asdict(event)['text'] == 'message'
    assert '_turn_status' not in asdict(event)
    assert '_turn_status' not in asdict(ctx)


@pytest.mark.asyncio
async def test_barrier_rejects_accidental_loop_thread_call(tmp_path):
    state = status(tmp_path)
    with pytest.raises(TurnStatusBlocked, match='off the gateway event loop'):
        state.before_tool_execution('call', 'terminal', {})
    assert not state.adapter.calls


@pytest.mark.asyncio
async def test_late_older_edit_cannot_overwrite_newer_card_state(tmp_path):
    adapter = Adapter()
    state = status(tmp_path, adapter)
    await state.ensure_started()
    await state.execution_finished({'completed': True})
    state.timeout = .1
    late_release = asyncio.Event()
    corrected = asyncio.Event()
    async def edit(chat_id, message_id, content, **kwargs):
        adapter.edits.append((message_id, content))
        if len(adapter.edits) == 1:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await late_release.wait()
        return SendResult(True, message_id)
    adapter.edit_message = edit
    original_record = state.record_result
    def record(result, intent=None):
        outcome = original_record(result, intent)
        if intent and intent['role'] == 'edit' and len(adapter.edits) == 3 and outcome == 'delivered':
            corrected.set()
        return outcome
    state.record_result = record
    await state.before_result('result')  # First edit times out but is still in flight.
    await state.send_result(lambda: adapter.send('chat', 'result'))
    await state.finish_delivery()  # Newer final edit succeeds.
    late_release.set()
    await asyncio.wait_for(corrected.wait(), 2)
    # The final corrective edit confirms the newest desired display, not the old one.
    assert adapter.edits[-1][1] == '작업 완료 · 결과 전달 확인'
    assert state.data['card_update_unresolved'] is False
    assert len(adapter.calls) == 3  # Result is never replayed.
    assert state.data['intents'][2]['state'] == 'delivered'  # Original intent settled in place.
