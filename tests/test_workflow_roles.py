"""One fixture card changes assignees; every native terminal call keeps its guard."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest import mock
import unittest

from bridge.broker import BridgeConfig, Broker
from bridge.pm import PmApprovalService
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from model_tools import _CallIds, _execute_tool
from tools import approval
import test_integration as fixture


class WorkflowRoleTests(unittest.TestCase):
    setUp = fixture.IntegrationTests.setUp
    tearDown = fixture.IntegrationTests.tearDown
    _create_db = fixture.IntegrationTests._create_db
    _write_hermes_config = fixture.IntegrationTests._write_hermes_config
    _write_bridge_config = fixture.IntegrationTests._write_bridge_config
    _install_fake_tirith = fixture.IntegrationTests._install_fake_tirith
    _restore_tirith = fixture.IntegrationTests._restore_tirith
    _clear_config_cache = fixture.IntegrationTests._clear_config_cache
    _env = fixture.IntegrationTests._env

    def test_executor_review_verification_and_remaining_roles_use_native_pm_transport(self):
        roles = ('work-executor', 'work-code-review', 'work-verifier', 'work-pm',
                 'work-planner', 'work-plan-review', 'work-deep-review', 'work-git', 'work-researcher')
        cfg = BridgeConfig(str(self.db_path), str(self.socket_path), fixture.OWNER_ID, 'work-pm',
                           worker_profiles=roles, max_timeout=5)
        path = self.plugin_root / '.local/config.json'
        path.write_text(json.dumps(cfg.__dict__))
        seen = []

        def review(data, *_):
            seen.append(data)
            return {'decision': 'approve', 'effect': 'non_delete', 'within_task': True, 'evidence_complete': True}

        human = mock.Mock()
        human.request.side_effect = AssertionError('Routine work must never ask a human')
        broker = Broker(cfg, PmApprovalService(human, review))
        broker.start()
        import bridge.pm as pm
        old_binding = pm._binding
        try:
            for index, role in enumerate(roles):
                with self.subTest(role=role):
                    profile = self.root / 'profiles' / role
                    profile.mkdir(parents=True)
                    (profile / 'config.yaml').write_text((self.hermes_home / 'config.yaml').read_text())
                    with sqlite3.connect(self.db_path) as db:
                        db.execute('UPDATE task_runs SET profile=? WHERE id=?', (role, fixture.RUN_ID))
                    env = dict(self._env(), HERMES_HOME=str(profile), HERMES_PROFILE=role,
                               TERMINAL_CWD=str(self.root), HERMES_KANBAN_WORKSPACE=str(self.root))
                    self._clear_config_cache()
                    with mock.patch.dict(os.environ, env, clear=True):
                        manager = PluginManager(scope_key=str(profile))
                        manager._discovered = True
                        ctx = PluginContext(PluginManifest(name='kanban-owner', path=str(fixture.PLUGIN_PATH.parent)), manager)
                        spec = importlib.util.spec_from_file_location('role_fixture_plugin', fixture.PLUGIN_PATH)
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        module.ROOT = self.plugin_root
                        module.register(ctx)
                        script = ('from pathlib import Path; Path("artifact.txt").write_text("verified")' if index == 0 else
                                  'from pathlib import Path; import hashlib; print(hashlib.sha256(Path("artifact.txt").read_bytes()).hexdigest())')
                        args = {'command': f"{sys.executable} -I -c '{script}'", 'timeout': 10}
                        session = 'role-session-' + role
                        with mock.patch('hermes_cli.plugins.get_plugin_manager', return_value=manager), mock.patch('tools.approval_prompt.get_plugin_manager', return_value=manager):
                            raw = _execute_tool('terminal', args, args, _CallIds(task_id=session, session_id=session,
                                turn_id='turn', tool_call_id='call-' + role), user_task=None,
                                enabled_tools=['terminal'], skip_tool_execution_middleware=False)
                        value = json.loads(raw)
                        self.assertEqual(value.get('exit_code'), 0, value)
                        self.assertEqual(value['approval_policy']['reason'], 'pm_approved')
                        self.assertEqual(seen[-1]['execution']['tool_call_id'], 'call-' + role)
                        self.assertEqual(seen[-1]['task_context']['current_run']['profile'], role)
                        self.assertEqual(seen[-1]['execution']['cwd'], str(self.root.resolve()))
                        if index:
                            self.assertIn(hashlib.sha256(b'verified').hexdigest(), value['output'])
                        self.assertEqual(os.environ['HERMES_SINGLE_QUERY_SESSION'], '1')
            self.assertEqual(len(seen), len(roles))
            human.request.assert_not_called()
        finally:
            pm._binding = old_binding
            broker.close()
