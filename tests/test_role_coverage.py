"""The approval route must survive normal Kanban assignee changes."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bridge.broker import BridgeConfig, validate_current_request
from bridge.protocol import ProtocolError
import test_broker


class RoleAuthorizationTests(unittest.TestCase):
    setUp = test_broker.BrokerTests.setUp
    tearDown = test_broker.BrokerTests.tearDown
    _create_db = test_broker.BrokerTests._create_db
    make_request = test_broker.BrokerTests.make_request
    def test_all_configured_roles_still_require_the_exact_running_role(self):
        roles = ('work-executor', 'work-code-review', 'work-verifier', 'work-pm',
                 'work-planner', 'work-plan-review', 'work-deep-review', 'work-git', 'work-researcher')
        config = BridgeConfig(str(self.db_path), str(self.socket_path), self.owner, 'work-pm',
                              worker_profiles=roles)
        for role in roles:
            with self.subTest(role=role), sqlite3.connect(self.db_path) as db:
                db.execute('UPDATE task_runs SET profile=?', (role,))
                db.commit()
                self.profile = role
                validate_current_request(config, self.make_request())
                self.profile = 'work-executor' if role != 'work-executor' else 'work-code-review'
                with self.assertRaises(ProtocolError):
                    validate_current_request(config, self.make_request())


class ProfileInstallationTests(unittest.TestCase):
    def test_merge_preserves_other_settings_and_comments_and_is_idempotent(self):
        from bridge.profiles import stage_profile
        import yaml
        original = '# model settings are private\nmodel: {default: fixture}\nplugins:\n  enabled: []\n  disabled: [other-plugin]\nsecurity:\n  scanner: strict\napprovals:\n  single_query_mode: deny\n'
        staged = stage_profile(original)
        result = yaml.safe_load(staged)
        self.assertEqual(result['approvals']['single_query_mode'], 'deny')
        self.assertEqual(result['security']['scanner'], 'strict')
        self.assertEqual(result['plugins']['disabled'], ['other-plugin'])
        self.assertEqual(result['security']['approval']['kanban_transport'], 'kanban-owner')
        self.assertIn('# model settings are private', staged)
        self.assertEqual(stage_profile(staged), staged)

    def test_a_new_installed_work_role_cannot_silently_pass_doctor(self):
        from bridge.profiles import profile_coverage
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / 'home', Path(tmp) / 'bridge'
            profile = home / 'profiles/work-code-review'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text('plugins: {enabled: []}\n')
            cfg = BridgeConfig('/fixture.db', '/fixture.sock', '123', 'work-pm',
                               worker_profiles=('work-executor',))
            result = profile_coverage(home, root, cfg)
            self.assertFalse(result['ready'])
            self.assertIn('work-code-review', result['missing_profiles'])

    def test_disabled_bridge_or_other_transport_is_not_overwritten(self):
        from bridge.profiles import stage_profile
        for text in ('plugins: {disabled: [kanban-owner]}\n',
                     'security: {approval: {kanban_transport: another}}\n'):
            with self.assertRaises(ValueError):
                stage_profile(text)

    def test_install_stages_every_role_and_keeps_native_denial_and_other_plugins(self):
        import importlib.util
        import json
        from unittest import mock
        import yaml
        from bridge.profiles import profile_coverage
        script = Path(__file__).resolve().parents[1] / 'scripts/install_profiles.py'
        spec = importlib.util.spec_from_file_location('fixture_installer', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp) / 'bridge', Path(tmp) / 'hermes'
            roles = ('work-executor', 'work-code-review', 'work-verifier', 'work-pm', 'work-future-role')
            for role in roles:
                path = home / 'profiles' / role
                path.mkdir(parents=True)
                (path / 'config.yaml').write_text('''# keep this comment
plugins:
  enabled:
    - existing-plugin
  entries: {existing-plugin: {allow_tool_override: false}}
approvals: {single_query_mode: deny}
discord: {allow_from: ['123']}
''')
            (root / 'plugins/kanban-owner').mkdir(parents=True)
            (root / 'hooks/kanban-owner').mkdir(parents=True)
            module.ROOT = root
            with mock.patch('sys.argv', ['install_profiles', '--home', str(home), '--apply']), mock.patch('builtins.print'):
                module.main()
                config_bytes = (root / '.local/config.json').read_bytes()
                module.main()
                self.assertEqual(config_bytes, (root / '.local/config.json').read_bytes())
            cfg = BridgeConfig(**json.loads(config_bytes))
            self.assertEqual(set(cfg.worker_profiles), set(roles))
            self.assertTrue(profile_coverage(home, root, cfg)['ready'])
            for role in roles:
                path = home / 'profiles' / role / 'config.yaml'
                config = yaml.safe_load(path.read_text())
                self.assertEqual(config['approvals']['single_query_mode'], 'deny')
                self.assertEqual(config['plugins']['enabled'], ['existing-plugin', 'kanban-owner'])
                self.assertFalse(config['plugins']['entries']['existing-plugin']['allow_tool_override'])
                self.assertIn('# keep this comment', path.read_text())
