import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from bridge.pm import PmApprovalService, review_with_sources


class ReviewLoopTests(unittest.TestCase):
    def test_pm_reads_dependency_before_approving_and_pins_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'test.py').write_text('import helper\nprint(helper.VALUE)\n')
            (root / 'helper.py').write_text('VALUE = 42\n')
            data = {'command': 'python3 test.py', 'execution': {'cwd': str(root)},
                    'task_context': {'workspace_path': str(root), 'body': 'Run local test'}}
            seen = []
            def complete(**kwargs):
                payload = json.loads(kwargs['input'][0]['text']); seen.append(payload)
                if len(seen) == 1:
                    return SimpleNamespace(parsed={'decision': 'inspect', 'read_requests': [{'path': 'helper.py'}]})
                self.assertTrue(any('VALUE = 42' in page.get('text', '') for page in payload['source_pages']))
                return SimpleNamespace(parsed={'decision': 'approve', 'effect': 'non_delete', 'within_task': True, 'evidence_complete': True})
            llm = SimpleNamespace(complete_structured=complete)
            service = PmApprovalService(None, lambda d, deadline, cancel: review_with_sources(llm, d, deadline, cancel))
            self.assertEqual(service.request(data, {}, time.time()+10, lambda: False), 'once')
            self.assertEqual(len(service.last_evidence()), 2)
            self.assertEqual(len(seen), 2)

    def test_source_change_after_model_read_revokes_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source = root / 'test.py'; source.write_text('print(1)')
            data = {'command': 'python3 test.py', 'execution': {'cwd': str(root)}, 'task_context': {'workspace_path': str(root)}}
            def complete(**kwargs):
                source.write_text('print(2)')
                return SimpleNamespace(parsed={'decision': 'approve', 'effect': 'non_delete', 'within_task': True, 'evidence_complete': True})
            service = PmApprovalService(None, lambda d, end, cancel: review_with_sources(SimpleNamespace(complete_structured=complete), d, end, cancel))
            self.assertEqual(service.request(data, {}, time.time()+10, lambda: False), 'deny')
            self.assertEqual(service.last_reason(), 'source_changed')

    def test_cancelled_inspection_does_not_make_another_model_call(self):
        llm = mock.Mock()
        self.assertEqual(review_with_sources(llm, {'command': 'pwd'}, time.time()+10, lambda: True), {})
        llm.complete_structured.assert_not_called()

    def test_last_inspection_has_a_final_decision_round(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'test.py').write_text('print(1)')
            replies = [SimpleNamespace(parsed={'decision': 'inspect', 'read_requests': [{'path': 'test.py'}]}) for _ in range(6)]
            replies.append(SimpleNamespace(parsed={'decision': 'approve'}))
            llm = mock.Mock()
            llm.complete_structured.side_effect = replies
            result = review_with_sources(llm, {'command': 'python3 test.py', 'execution': {'cwd': str(root)},
                                         'task_context': {'workspace_path': str(root)}}, time.time()+10, lambda: False)
            self.assertEqual(result['decision'], 'approve')
            final = json.loads(llm.complete_structured.call_args.kwargs['input'][0]['text'])
            self.assertEqual(final['inspection_rounds_remaining'], 0)

    def test_python_import_entry_effect_is_visible_before_first_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'module.py').write_text('def discover():\n    pass\n' + '\n'*500 + 'discover()\n')
            data = {'command': "python3 -c 'import module; print(module)'", 'execution': {'cwd': str(root)}, 'task_context': {'workspace_path': str(root)}}
            llm = mock.Mock(return_value=None)
            llm.complete_structured.return_value = SimpleNamespace(parsed={'decision': 'deny'})
            review_with_sources(llm, data, time.time()+10, lambda: False)
            payload = json.loads(llm.complete_structured.call_args.kwargs['input'][0]['text'])
            self.assertEqual(payload['source_pages'][0]['import_time_statements'][0]['text'], 'discover()')
