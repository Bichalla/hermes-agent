"""Opt-in Discord turn delivery barrier and private, receipt-bearing journal.

This journal deliberately does not use delivery_ledger's ambiguous-send replay policy.
An interrupted HTTP request may have reached Discord: persist the intent first, keep
confirmed chunk IDs, and stop rather than replaying unknown delivery or execution.
No model prompts, history, or tool schemas are changed by this UI protocol.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
import tempfile
import time
import uuid
from typing import Any

from gateway.platforms.base import SendResult
from hermes_constants import get_hermes_home


class TurnStatusBlocked(RuntimeError):
    """No request-performing tool may start without confirmed ACK and card IDs."""


def _number(config, key, default, minimum, maximum):
    try:
        value = float(config.get(key, default))
        return min(maximum, max(minimum, value)) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _receipt(result):
    raw = getattr(result, 'raw_response', None)
    raw = raw if isinstance(raw, dict) else {}
    ids = [str(x) for x in raw.get('message_ids', []) if x]
    for mid in (getattr(result, 'message_id', None), *getattr(result, 'continuation_message_ids', ())):
        if mid and str(mid) not in ids:
            ids.append(str(mid))
    if getattr(result, 'success', False) and ids and not raw.get('uncertain'):
        state = 'delivered'
    elif ids:
        state = 'partial'
    elif raw.get('delivery_state') == 'not_sent' and not raw.get('uncertain'):
        state = 'not_sent'
    else:
        state = 'unknown'
    return state, ids, {k: raw[k] for k in (
        'delivery_state', 'delivered_chunks', 'total_chunks', 'failed_chunk_index', 'uncertain', 'target_id'
    ) if k in raw}


class TurnStatus:
    """One request owner; async sends run on the gateway loop, tools wait off-loop.

    Defaults: 10 seconds per send/edit, at most 2 proven-not-sent attempts,
    30 seconds between status edits, cancellation checked every 100 ms. Limits
    are clamped (send 0.1–30 s, attempts 1–3, edits 1–300 s). Unknown outcomes
    never consume a resend attempt. Each request has its own async lock.
    """
    def __init__(self, *, adapter, home, source, request_id, session_id, session_key,
                 run_generation, loop, config, is_current, is_cancelled):
        self.adapter, self.source, self.loop = adapter, source, loop
        self.is_current, self.is_cancelled = is_current, is_cancelled
        self.timeout = _number(config, 'timeout_seconds', 10.0, .1, 30.0)
        self.max_attempts = int(_number(config, 'max_attempts', 2, 1, 3))
        self.update_interval = _number(config, 'update_interval_seconds', 30.0, 1.0, 300.0)
        identity = {
            'request_id': str(request_id), 'session_id': session_id,
            'session_key': session_key, 'profile': getattr(source, 'profile', None) or 'default',
            'chat_id': str(source.chat_id), 'thread_id': getattr(source, 'thread_id', None),
        }
        # Compression/new-session bookkeeping must not grant permission to replay
        # an already-seen Discord message after restart.
        request_key = {k: v for k, v in identity.items() if k not in {'session_id', 'session_key'}}
        key = hashlib.sha256(json.dumps(request_key, sort_keys=True).encode()).hexdigest()
        self.path = Path(home) / 'sessions' / 'turn-status' / (key + '.json')
        from gateway.delivery_ledger import _owner_stamp
        owner_pid, owner_start = _owner_stamp()
        self.data: dict[str, Any] = {
            **identity, 'run_id': uuid.uuid4().hex, 'run_generation': run_generation,
            'owner_pid': owner_pid, 'owner_start': owner_start,
            'execution_status': 'not_started', 'delivery_status': 'pending', 'preflight_status': 'pending',
            'phase': 'received', 'card_message_id': None, 'card_chat_id': None,
            'card_update_unresolved': False, 'intents': [], 'result': None,
        }
        self.metadata = {'_interim_send': True, 'communication_source_channel_id': str(source.chat_id)}
        if getattr(source, 'thread_id', None) is not None:
            self.metadata['thread_id'] = source.thread_id
        self._lock = asyncio.Lock()
        self._claimed = self._recovered = self._ready = self._closed = False
        self._heartbeat = None
        self._component: dict[str, Any] | None = None
        self._blocked = None
        self._last_edit = 0.0
        self._result_prepared = False
        self._recovery_edit_done = False

    def _save(self):
        """Atomic replacement plus fsync: a send is forbidden until its intent is durable."""
        self.data['updated_at'] = time.time()
        fd, name = tempfile.mkstemp(prefix='.turn-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(self.data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _claim(self):
        if self._claimed:
            return
        if self.path.parent.is_symlink() or self.path.parent.parent.is_symlink() or self.path.is_symlink():
            raise TurnStatusBlocked('Turn-status journal must be inside the profile sessions directory')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # An existing request is never a license to repeat work. In-flight sends
            # could have reached the server even if the last local ID was not saved.
            with self.path.open(encoding='utf-8') as inp:
                self.data = json.load(inp)
            self._recovered = True
            for intent in self.data['intents']:
                if intent['state'] == 'attempting':
                    intent['state'] = 'unknown'
            if self.data['execution_status'] == 'running':
                self.data['execution_status'] = 'unknown'
            if any(i['state'] == 'unknown' and i['role'] not in {'edit', 'status_notice'} for i in self.data['intents']):
                if any(i['role'] in {'result', 'attachments', 'tts', 'attachment'} for i in self.data['intents']):
                    self.data['delivery_status'] = self._delivery_outcome()
                elif self.data['delivery_status'] != 'delivered':
                    self.data['delivery_status'] = 'unknown'
            self._claimed = True
            self._save()
            return
        else:
            os.close(fd)
        self._claimed = True
        self._save()

    def _check(self):
        if self._closed or not self.is_current() or self.is_cancelled():
            self.data['phase'] = 'cancelled'
            if not self._ready:
                self.data['execution_status'] = 'not_started'
            if self._claimed:
                self._save()
            raise TurnStatusBlocked('Request cancelled before execution or no longer current')
        if self._blocked:
            raise TurnStatusBlocked(self._blocked)

    def begin_component(self, role):
        """Record a send intent before crossing the transport boundary (also media groups)."""
        self._claim()
        if self._recovered and role != 'edit':
            raise TurnStatusBlocked('Existing request requires delivery/execution confirmation; not replayed')
        item = {'intent_id': uuid.uuid4().hex, 'role': role, 'state': 'attempting', 'message_ids': []}
        self.data['intents'].append(item)
        self._component = item
        self._save()
        return item

    def record_result(self, result, intent=None):
        """Record exact confirmed IDs and uncertainty; never infer success from a scheduled future."""
        intent = intent if intent is not None else self._component
        if intent is None:
            intent = self.begin_component('attachment')
        state, ids, detail = _receipt(result)
        old_ids = intent['message_ids']
        prior_state = intent['state']
        intent['message_ids'] = old_ids + [mid for mid in ids if mid not in old_ids]
        if prior_state not in {'attempting', 'delivered'} and state != prior_state:
            intent['state'] = 'partial' if intent['message_ids'] else 'unknown'
        else:
            intent['state'] = 'partial' if old_ids and state != 'delivered' else state
        intent.setdefault('receipts', []).append({'state': state, 'message_ids': ids, **detail})
        intent['receipt'] = detail
        # Error text may contain a URL/token; store only the transport's classified kind.
        intent['error_kind'] = getattr(result, 'error_kind', None)
        self._save()
        return state

    async def _bounded(self, operation, *, cancellable=False, on_late_result=None):
        task = asyncio.create_task(operation())
        deadline = self.loop.time() + self.timeout
        observed = False
        try:
            while True:
                if cancellable:
                    self._check()
                remaining = deadline - self.loop.time()
                if remaining <= 0:
                    raise TimeoutError('transport confirmation timed out')
                done, _ = await asyncio.wait({task}, timeout=min(.1, remaining))
                if done:
                    observed = True
                    return task.result()
        finally:
            if not observed:
                if not task.done():
                    task.cancel()
                # Discord may return confirmed earlier chunk IDs while handling
                # cancellation. Capture THIS intent, never the mutable current
                # component. No cancel-join or further send is needed to settle it.
                def settle(done):
                    if done.cancelled():
                        return
                    try:
                        result = done.result()
                        if on_late_result is not None:
                            on_late_result(result)
                    except Exception:
                        pass  # The durable unknown intent remains conservative.
                task.add_done_callback(settle)

    def _settle_late(self, intent, result):
        if not intent.get('receipts'):
            intent['state'] = 'attempting'  # A late receipt resolves this same unknown attempt.
        outcome = self.record_result(result, intent)
        if intent['role'] == 'edit':
            if intent['intent_id'] == self.data.get('card_edit_intent_id'):
                self.data['card_update_unresolved'] = outcome != 'delivered'
            elif outcome == 'delivered':
                # An older timed-out edit may have landed after a newer one.
                # Preserve both receipts and converge only this known card to
                # the latest desired text; never let the old receipt claim it.
                self.data['card_update_unresolved'] = True
                desired = self.data.get('desired_card_text')
                if desired:
                    self._correct_card_once(desired)
        elif intent['role'] == 'card' and outcome == 'delivered' and getattr(result, 'message_id', None):
            self.data['card_message_id'] = str(result.message_id)
            raw = getattr(result, 'raw_response', None) or {}
            self.data['card_chat_id'] = str(raw.get('target_id') or self.source.chat_id)
            self.data['card_update_unresolved'] = True
            # Only correct this known message; a late receipt never reopens the tool gate.
            self._correct_card_once('실행 전 중단 · 전달 확인 지연으로 업무를 시작하지 않았습니다.')
        if intent['role'] in {'result', 'attachments', 'tts', 'attachment'}:
            self.data['delivery_status'] = self._delivery_outcome()
        self._save()

    def _correct_card_once(self, text):
        # Late responses must not create an endless convergence/edit loop.
        if self.data.get('late_card_correction_attempted'):
            return
        self.data['late_card_correction_attempted'] = True
        self._save()
        task = asyncio.create_task(self._edit(text))
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def _send(self, role, operation, *, cancellable=False, attempts=None):
        operation_id = uuid.uuid4().hex
        for _ in range(self.max_attempts if attempts is None else attempts):
            if cancellable:
                self._check()
            intent = self.begin_component(role)
            intent['operation_id'] = operation_id
            self._save()
            try:
                result = await self._bounded(operation, cancellable=cancellable,
                                             on_late_result=lambda result: self._settle_late(intent, result))
            except BaseException:
                intent['state'] = 'unknown'
                self._save()
                raise
            outcome = self.record_result(result, intent)
            if outcome != 'not_sent':
                return result
        return result

    async def ensure_started(self):
        async with self._lock:
            self._check()
            if self._ready:
                return
            self._claim()
            if self._recovered:
                await self.recover_card()
                self._blocked = 'Existing request requires confirmation; tools were not restarted'
                raise TurnStatusBlocked(self._blocked)
            try:
                for role, text in (
                    ('ack', '요청을 접수했습니다. 전달 상태를 확인한 뒤 진행하겠습니다.'),
                    ('card', '작업 중 · 요청을 확인하고 있습니다.'),
                ):
                    result = await self._send(role, lambda text=text: self.adapter.send(
                        self.source.chat_id, text, metadata=dict(self.metadata)), cancellable=True)
                    if _receipt(result)[0] != 'delivered' or not getattr(result, 'message_id', None):
                        raise TurnStatusBlocked(f'{role} delivery not confirmed; tools were not started')
                    if role == 'card':
                        self.data['card_message_id'] = str(result.message_id)
                        raw = getattr(result, 'raw_response', None) or {}
                        self.data['card_chat_id'] = str(raw.get('target_id') or self.source.chat_id)
                        self._save()
                self._check()
                self.data['execution_status'], self.data['phase'] = 'running', 'working'
                self.data['preflight_status'] = 'ready'
                self._save()
                self._ready = True
                self._heartbeat = asyncio.create_task(self._update_loop())
            except BaseException as exc:
                self._blocked = 'Acknowledgement/status delivery not confirmed; tools were not started'
                self.data['delivery_status'] = (
                    'unknown' if any(i['state'] in {'unknown', 'partial'} for i in self.data['intents']) else 'failed'
                )
                self.data['preflight_status'] = self.data['delivery_status']
                self._save()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise TurnStatusBlocked(self._blocked) from exc

    def before_tool_execution(self, call_id, name, args):
        """Fail-closed dispatch hook: caller is the agent/tool worker, never the asyncio loop."""
        # A direct loop-thread call would deadlock on run_coroutine_threadsafe.result().
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            raise TurnStatusBlocked('Tool barrier must run off the gateway event loop')
        future = asyncio.run_coroutine_threadsafe(self.ensure_started(), self.loop)
        budget = 2 * self.max_attempts * self.timeout + 2
        try:
            future.result(timeout=budget)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError) as exc:
            future.cancel()
            self._blocked = 'Acknowledgement/status confirmation deadline exceeded'
            raise TurnStatusBlocked(self._blocked) from exc
        self.loop.call_soon_threadsafe(self._set_tool_phase, name)

    def _set_tool_phase(self, name):
        if not self._closed:
            self.data['phase'] = 'waiting' if name == 'clarify' else 'working'
            self._save()

    async def _edit(self, text):
        mid = self.data['card_message_id']
        if not mid:
            return
        self.data['card_update_unresolved'] = True
        try:
            intent = self.begin_component('edit')
        except Exception:
            # An edit is optional; failure to journal it must not discard the
            # separately persisted result or prevent its delivery.
            return
        self.data['card_edit_intent_id'] = intent['intent_id']
        self.data['desired_card_text'] = text
        self._save()
        target = self.data.get('card_chat_id') or self.source.chat_id
        metadata = dict(self.metadata)
        if str(target) != str(self.source.chat_id):
            metadata.pop('thread_id', None)
        try:
            result = await self._bounded(lambda: self.adapter.edit_message(
                target, mid, text, metadata=metadata),
                on_late_result=lambda result: self._settle_late(intent, result))
            self.data['card_update_unresolved'] = self.record_result(result, intent) != 'delivered'
            self._save()
        except Exception:
            intent['state'] = 'unknown'
            self._save()
        self._last_edit = self.loop.time()

    async def _update_loop(self):
        try:
            while True:
                await asyncio.sleep(self.update_interval)
                if self._closed or not self.is_current():
                    return
                text = '답변 대기 · 확인 후 이어서 진행합니다.' if self.data['phase'] == 'waiting' else '작업 중 · 요청을 계속 처리하고 있습니다.'
                await self._edit(text)
        except asyncio.CancelledError:
            return

    async def close(self):
        self._closed = True
        if self._heartbeat:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
            self._heartbeat = None

    async def execution_finished(self, result):
        await self.close()
        if self._claimed:
            if not self._ready:
                outcome = 'not_started'
            elif result.get('interrupted'):
                outcome = 'cancelled'
            elif result.get('failed') or result.get('completed') is False:
                outcome = 'failed'
            else:
                outcome = 'completed' if self._ready else 'not_started'
            if self._recovered:
                return
            self.data['execution_status'] = outcome
            self.data['execution_effects'] = 'unknown' if self._ready and outcome in {'failed', 'cancelled'} else outcome
            self._save()

    async def recover_card(self):
        if not self._recovery_edit_done:
            self._recovery_edit_done = True
            self.data['phase'] = 'confirmation_required'
            self._save()
            unsettled = any(i['role'] not in {'edit', 'status_notice'} and i['state'] in {'attempting', 'unknown', 'partial'}
                            for i in self.data['intents'])
            display_only = (not unsettled and self.data['delivery_status'] == 'delivered'
                            and self.data['execution_status'] not in {'running', 'unknown'}
                            and self.data.get('execution_effects') != 'unknown')
            if display_only:
                label = {'failed': '작업 실패', 'cancelled': '작업 취소'}.get(self.data['execution_status'], '작업 완료')
                if self.data.get('preflight_status') in {'failed', 'unknown'} and self.data['execution_status'] == 'not_started':
                    label = '실행 전 중단'
                await self._edit(label + ' · 결과 전달 확인 (상태 복구)')
                if not self.data.get('card_update_unresolved'):
                    self.data['phase'] = 'finished'
                    self._save()
            else:
                await self._edit('확인 필요 · 재시작 전 실행·전달 상태를 확인해야 합니다. 업무는 자동 재실행하지 않습니다.')

    async def before_result(self, content):
        self._claim()
        if self._recovered:
            await self.recover_card()
            raise TurnStatusBlocked('Recovered request requires confirmation; result was not replayed')
        self.data['result'] = str(content or '')
        self.data['phase'] = 'delivering'
        self._save()  # Result survives an edit failure or crash before sending.
        if self._result_prepared:
            return
        self._result_prepared = True
        if self.data.get('preflight_status') in {'failed', 'unknown'} and not self._ready:
            text = '실행 전 중단 · 안내 전달 중'
        elif self.data['execution_status'] in {'failed', 'cancelled'}:
            text = '작업 취소' if self.data['execution_status'] == 'cancelled' else '작업 실패 · 결과 전달 중'
        else:
            text = '작업 완료 · 결과 전달 중'
        await self._edit(text)

    async def send_result(self, operation, *, role='result'):
        try:
            return await self._send(role, operation)
        except asyncio.CancelledError:
            raise
        except Exception:
            return SendResult(False, error='turn_status_delivery_unknown', raw_response={
                'delivery_state': 'unknown', 'uncertain': True,
            })

    def _delivery_outcome(self):
        attempts = [i for i in self.data['intents'] if i['role'] in {'result', 'attachments', 'tts', 'attachment'}]
        # Proven-not-sent attempts may be retried; the final receipt for that
        # logical component determines delivery without hiding the earlier audit.
        items = list({i.get('operation_id', i['intent_id']): i for i in attempts}.values())
        states = [i['state'] for i in items]
        if not states:
            outcome = 'pending'
        elif all(s == 'delivered' for s in states):
            outcome = 'delivered'
        elif 'partial' in states or (any(i['message_ids'] for i in items) and any(s != 'delivered' for s in states)):
            outcome = 'partial'
        elif any(s in {'unknown', 'attempting'} for s in states):
            outcome = 'unknown'
        else:
            outcome = 'failed'
        return outcome

    async def finish_delivery(self):
        if not self._claimed or self._recovered:
            return
        outcome = self._delivery_outcome()
        self.data['delivery_status'], self.data['phase'] = outcome, 'finished'
        self._save()
        text = '작업 완료 · 결과 전달 확인' if outcome == 'delivered' else '결과 전달 확인 필요 · 결과는 보존되어 있습니다.'
        if self.data.get('preflight_status') in {'failed', 'unknown'} and not self._ready:
            text = '실행 전 중단 · ' + ('안내 전달 확인' if outcome == 'delivered' else '안내 전달 확인 필요')
        elif self.data['execution_status'] == 'cancelled':
            text = '작업 취소 · ' + ('안내 전달 확인' if outcome == 'delivered' else '안내 전달 확인 필요')
        elif self.data['execution_status'] == 'failed':
            text = '작업 실패 · ' + ('안내 전달 확인' if outcome == 'delivered' else '안내 전달 확인 필요')
        await self._edit(text)
        if self.data.get('card_update_unresolved') and not any(i['role'] == 'status_notice' for i in self.data['intents']):
            # One safely routed, journaled notice makes a stale working card
            # explicit without discarding/repeating the separately sent result.
            notice = ('상태 카드 갱신을 확인하지 못했습니다. 위 작업 중 표시는 최신 상태가 아닐 수 있습니다. '
                      + ('결과 전달은 확인되었습니다.' if outcome == 'delivered' else '결과 전달은 확인이 필요하며 결과는 보존되어 있습니다.'))
            try:
                await self._send('status_notice', lambda: self.adapter.send(
                    self.source.chat_id, notice, metadata=dict(self.metadata)), attempts=1)
            except Exception:
                pass  # Its attempted/unknown intent remains durable; no blind replay.
        await self.close()


def create_turn_status(ctx, adapter, is_cancelled):
    """Read the real raw gateway YAML namespace; default-off and interactive Discord only."""
    cfg = (((ctx.user_config or {}).get('platforms') or {}).get('discord') or {}).get('extra') or {}
    cfg = cfg.get('turn_status') or {}
    if not isinstance(cfg, dict) or cfg.get('enabled') is not True:
        return None
    if getattr(ctx.source.platform, 'value', ctx.source.platform) != 'discord' or ctx.persist_user_display_kind:
        return None
    if adapter is None:
        raise TurnStatusBlocked('Discord transport unavailable')
    return TurnStatus(
        adapter=adapter, home=get_hermes_home(), source=ctx.source,
        request_id=ctx.inbound_message_id or ctx.event_message_id or uuid.uuid4().hex,
        session_id=ctx.session_id, session_key=ctx.session_key, run_generation=ctx.run_generation,
        loop=ctx._loop_for_step, config=cfg, is_current=ctx._run_still_current,
        is_cancelled=is_cancelled,
    )


async def recover_turn_statuses(home, adapter, *, profile='default', max_files=100, max_edits=5, budget_seconds=5):
    """Bounded boot recovery: no new messages, result replay, or model/tool execution.

    At most 100 regular journals (1 MiB each), 5 existing-card edits and 5 total
    seconds per profile. Unreadable/symlinked files and overflow remain HOLD
    evidence. Process liveness is reused from the existing delivery ledger.
    """
    from gateway.delivery_ledger import _owner_alive
    from gateway.session import SessionSource
    from gateway.config import Platform

    root = Path(home) / 'sessions' / 'turn-status'
    report = {'scanned': 0, 'unresolved': 0, 'invalid': 0, 'overflow': False, 'session_keys': []}
    if root.is_symlink() or root.parent.is_symlink():
        report['invalid'] = 1
        return report
    if not root.is_dir():
        return report
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    edits = 0
    with os.scandir(root) as entries:
        for entry in entries:
            if not re.fullmatch(r'[a-f0-9]{64}\.json', entry.name):
                continue
            if report['scanned'] >= max_files or loop.time() >= deadline:
                report['overflow'] = True
                break
            report['scanned'] += 1
            try:
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                    raise ValueError('invalid journal file')
                fd = os.open(entry.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, encoding='utf-8') as inp:
                    data = json.load(inp)
                if data.get('profile') != profile or not isinstance(data.get('intents'), list):
                    raise ValueError('journal profile/shape mismatch')
                if _owner_alive(data.get('owner_pid'), data.get('owner_start')):
                    continue
                # Fully finished rows do not require remote changes. Every other
                # row holds auto-resume until its execution/delivery is reconciled.
                unsettled = any(i.get('role') not in {'edit', 'status_notice'} and i.get('state') in {'attempting', 'unknown', 'partial'}
                                for i in data['intents'])
                if (data.get('delivery_status') == 'delivered' and data.get('phase') == 'finished'
                        and not unsettled and not data.get('card_update_unresolved')
                        and data.get('execution_status') not in {'running', 'unknown'}
                        and data.get('execution_effects') != 'unknown'):
                    continue
                state = TurnStatus(
                    adapter=adapter, home=home,
                    source=SessionSource(platform=Platform.DISCORD, chat_id=data['chat_id'],
                                         thread_id=data.get('thread_id'), profile=profile),
                    request_id=data['request_id'], session_id=data.get('session_id'),
                    session_key=data.get('session_key'), run_generation=data.get('run_generation'),
                    loop=loop, config={'timeout_seconds': min(1, max(.1, deadline - loop.time())), 'max_attempts': 1},
                    is_current=lambda: True, is_cancelled=lambda: False,
                )
                if state.path != Path(entry.path):
                    raise ValueError('journal identity mismatch')
                state._claim()
                report['unresolved'] += 1
                execution_or_delivery_risk = (unsettled or data.get('delivery_status') != 'delivered'
                                              or data.get('execution_status') in {'running', 'unknown'}
                                              or data.get('execution_effects') == 'unknown')
                if execution_or_delivery_risk and data.get('session_key'):
                    report['session_keys'].append(data['session_key'])
                if state.data.get('card_message_id') and edits < max_edits and adapter is not None:
                    edits += 1
                    await state.recover_card()
                    if not execution_or_delivery_risk and not state.data.get('card_update_unresolved'):
                        report['unresolved'] -= 1
            except (OSError, ValueError, TypeError, KeyError):
                report['invalid'] += 1
    return report
