"""Bind native tool execution metadata to the host-created approval request.

The lifecycle hook carries native call IDs across the approval worker thread.
No argument, authority, or decision is recovered by matching command text alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import threading

from .protocol import ProtocolError

GUIDANCE = {
    'needs_evidence': 'PM could not establish effects from the bounded source evidence. Preserve the exact command and missing evidence; this is not a human denial or missing terminal capability.',
    'outside_task_scope': 'The command conflicts with this card scope. Follow the authorized scope; human approval of an unrelated operation is not a substitute.',
    'human_declined': 'The owner denied permanent deletion. Do not retry or use another route.',
    'human_route_ambiguous': 'Permanent deletion needs one explicit owner notification destination. Routine work does not require that destination.',
    'human_display_limit': 'The permanent deletion command cannot be displayed in full by the current human approval surface. Do not approve a truncated command.',
    'source_changed': 'Reviewed source changed before execution. Obtain a fresh review of the changed source.',
    'pm_review_failed': 'The PM reviewer failed or timed out. Record the infrastructure failure; do not treat it as owner denial.',
}


@dataclass
class Binding:
    execution: dict
    command_hash: str
    command_display: str
    requests: dict[str, str] = field(default_factory=dict)
    reason: str = ''
    details: str = ''


class ExecutionBindings:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}
        self._requests = {}

    @staticmethod
    def _key(data):
        return tuple(str(data.get(k) or '') for k in ('session_id', 'turn_id', 'tool_call_id'))

    def wrap(self, *, tool_name, args, next_call, **ids):
        if tool_name != 'terminal':
            return next_call(args)
        try:
            from agent.redact import redact_sensitive_text
            from tools import terminal_tool as terminal
            from tools.approval import get_current_session_key
            try:
                plan = terminal._plan_execution(
                    args.get('command'), task_id=ids.get('task_id'), timeout=args.get('timeout'),
                    background=bool(args.get('background')), _host_local=False,
                )
            except terminal._Rejected as exc:
                return exc.result_json
            if plan.env_type != 'local':
                raise ProtocolError('source review requires local terminal context')
            session_key = get_current_session_key(default='') or ids.get('task_id') or ''
            cwd = terminal._resolve_command_cwd(workdir=args.get('workdir'), default_cwd=plan.cwd,
                                               session_key=session_key, env_type='local')
            if not Path(cwd).is_absolute():
                raise ProtocolError('absolute execution directory required')
            # Pin the same resolved directory for review and native execution.
            cwd = str(Path(cwd).resolve(strict=True))
            command = args['command']
            binding = Binding({'cwd': cwd, 'tool_call_id': str(ids.get('tool_call_id') or 'native-call')},
                              hashlib.sha256(command.encode('utf-8', 'surrogatepass')).hexdigest(),
                              redact_sensitive_text(command, force=True))
            key = self._key(ids)
            with self._lock:
                if key in self._active:
                    raise ProtocolError('duplicate active native call identity')
                self._active[key] = binding
        except Exception:
            return json.dumps({'status': 'blocked', 'exit_code': -1, 'output': '',
                               'error': 'Kanban execution context unavailable; no command was executed.'})
        try:
            result = next_call(dict(args, workdir=cwd))
            if binding.reason and isinstance(result, str):
                try:
                    value = json.loads(result)
                    if isinstance(value, dict):
                        value['approval_policy'] = {'reason': binding.reason,
                                                   'guidance': GUIDANCE.get(binding.reason, ''),
                                                   'details': binding.details}
                        return json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    pass
            return result
        finally:
            with self._lock:
                self._active.pop(key, None)
                for request_id in binding.requests:
                    self._requests.pop(request_id, None)

    def before_approval(self, **data):
        if data.get('surface') != 'transport:kanban-owner':
            return
        with self._lock:
            binding = self._active.get(self._key(data))
            if (binding is None or data.get('command') != binding.command_display
                    or not str(data.get('session_key', '')).endswith(':' + binding.command_hash)):
                return
            request_id, digest = data.get('request_id'), data.get('request_digest')
            if not request_id or not digest or request_id in self._requests:
                return
            binding.requests[request_id] = digest
            self._requests[request_id] = binding

    def context(self, request) -> dict:
        with self._lock:
            binding = self._requests.get(request.request_id)
            if (binding is None or binding.requests.get(request.request_id) != request.digest
                    or binding.command_display != request.command):
                raise ProtocolError('native execution binding missing')
            return dict(binding.execution)

    def decision(self, request, reason, details=''):
        with self._lock:
            binding = self._requests.get(request.request_id)
            if binding is not None and binding.requests.get(request.request_id) == request.digest:
                binding.reason = reason
                if isinstance(details, str) and len(details) <= 2000:
                    from agent.redact import redact_sensitive_text
                    binding.details = redact_sensitive_text(details, force=True)

    def register(self, ctx):
        ctx.register_middleware('tool_execution', self.wrap)
        ctx.register_hook('pre_approval_request', self.before_approval)
