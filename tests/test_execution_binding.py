from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bridge.execution import ExecutionBindings
from bridge.protocol import ProtocolError
from hermes_cli.approval_transport import ApprovalRequest


class ExecutionBindingTests(unittest.TestCase):
    def test_native_task_cwd_override_is_used_when_workdir_is_omitted(self):
        bindings = ExecutionBindings()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch('tools.terminal_tool.resolve_task_overrides', return_value={'cwd': temp}):
                result = bindings.wrap(tool_name='terminal', args={'command': 'pwd'},
                                       next_call=lambda args: args, task_id='native-session')
            self.assertEqual(result['workdir'], str(Path(temp).resolve()))

    def test_native_ids_and_digest_bind_context_then_cleanup_revokes_it(self):
        bindings = ExecutionBindings()
        command = 'pwd'
        req = ApprovalRequest.create(command=command, description='Review', pattern_key='review', pattern_keys=('review',), session_key='s', surface='kanban_worker', allow_session=False, allow_permanent=False)
        ids = {'session_id': 'session', 'turn_id': 'turn', 'tool_call_id': 'call'}
        hook = dict(ids, surface='transport:kanban-owner', command=command, request_id=req.request_id,
                    request_digest=req.digest, session_key='kanban-worker:t:1:'+hashlib.sha256(command.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as temp:
            def downstream(args):
                bindings.before_approval(**dict(hook, tool_call_id='wrong'))
                with self.assertRaises(ProtocolError):
                    bindings.context(req)
                bindings.before_approval(**hook)
                self.assertEqual(bindings.context(req)['cwd'], str(Path(temp).resolve()))
                with self.assertRaises(ProtocolError):
                    bindings.context(replace(req, digest='other'))
                bindings.decision(req, 'outside_task_scope', 'Remote access is outside the card scope.')
                return json.dumps({'status': 'blocked', 'exit_code': -1})
            result = json.loads(bindings.wrap(tool_name='terminal', args={'command': command, 'workdir': temp}, next_call=downstream, **ids))
            self.assertEqual(result['approval_policy']['reason'], 'outside_task_scope')
            self.assertIn('Remote access', result['approval_policy']['details'])
        with self.assertRaises(ProtocolError):
            bindings.context(req)

    def test_exception_revokes_active_binding(self):
        bindings = ExecutionBindings()
        with tempfile.TemporaryDirectory() as temp:
            def downstream(args):
                raise RuntimeError('fixture failure')
            with self.assertRaises(RuntimeError):
                bindings.wrap(tool_name='terminal', args={'command': 'pwd', 'workdir': temp}, next_call=downstream, tool_call_id='same')
            result = bindings.wrap(tool_name='terminal', args={'command': 'pwd', 'workdir': temp}, next_call=lambda args:'ok', tool_call_id='same')
            self.assertEqual(result, 'ok')
