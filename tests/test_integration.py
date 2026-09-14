"""End-to-end isolated approval bridge fixtures.

This exercises the candidate core guard, plugin transport, worker socket,
broker DB validation, and native gateway facade without running shell commands,
networking, or touching the production Hermes runtime/profile.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import types
import unittest
from unittest import mock

from bridge.broker import BridgeConfig, Broker
from bridge.pm import PmApprovalService
from gateway.config import Platform
from gateway.kanban_approval import KanbanApprovalService
from hermes_cli.approval_transport import RegisteredApprovalTransport
from tools import approval


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO_ROOT / "plugins/kanban-owner/__init__.py"
OWNER_ID = "1494011544214835201"
TASK_ID = "t_integration"
RUN_ID = 1
CLAIM = "claim-integration"
WORKER_PROFILE = "work-executor"
NOTIFIER_PROFILE = "work-pm"


class AdapterFixture:
    def __init__(self, owner_id: str, *, choice: str | None = "once", mutate_before_choice=None):
        self._allowed_user_ids = {owner_id}
        self._allowed_role_ids = set()
        self.choice = choice
        self.mutate_before_choice = mutate_before_choice
        self.sent = threading.Event()
        self.calls = []
        self.delivery_success = True

    def _discord_allow_all_users(self):
        return False

    async def _send_prompt(self, chat, metadata, build):
        kwargs, view = build(None)
        key = view.session_key
        self.calls.append((chat, kwargs, key, view))
        self.sent.set()
        if self.mutate_before_choice is not None:
            self.mutate_before_choice()
        if self.choice is not None:
            approval.resolve_gateway_approval(key, self.choice)
        return SimpleNamespace(success=self.delivery_success)


class Ctx:
    def __init__(self):
        self.present = None
        self.name = None
        self.tools = {}
        self.hooks = {}
        self.sections = {}

    def register_approval_transport(self, name, present):
        self.name = name
        self.present = present

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_system_prompt_section(self, name, content):
        self.sections[name] = content


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hermes_home = self.root / "hermes-home"
        self.hermes_home.mkdir(mode=0o700)
        self.plugin_root = self.root / "plugin-root"
        (self.plugin_root / ".local").mkdir(parents=True, mode=0o700)
        self.db_path = self.root / "kanban.db"
        self.socket_path = self.root / "run" / "approval.sock"
        self._create_db()
        self._write_hermes_config()
        self._write_bridge_config()
        self._install_fake_tirith()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

        def run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(self.ready.set)
            self.loop.run_forever()

        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()
        self.assertTrue(self.ready.wait(3))

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(3)
        self.loop.close()
        self._restore_tirith()
        self.tmp.cleanup()
        self._clear_config_cache()

    def _create_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT,
                current_run_id INTEGER,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                claim_lock TEXT,
                claim_expires INTEGER,
                worker_pid INTEGER
            );
            CREATE TABLE kanban_notify_subs (
                task_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT,
                user_id_alt TEXT,
                notifier_profile TEXT,
                PRIMARY KEY (task_id, platform, chat_id, thread_id)
            );
            """
        )
        expires = int(time.time()) + 300
        conn.execute(
            "INSERT INTO task_runs(id, task_id, profile, status, claim_lock, claim_expires, worker_pid) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (RUN_ID, TASK_ID, WORKER_PROFILE, CLAIM, expires, os.getpid()),
        )
        conn.execute(
            "INSERT INTO tasks(id, status, current_run_id, claim_lock, claim_expires, worker_pid) "
            "VALUES (?, 'running', ?, ?, ?, ?)",
            (TASK_ID, RUN_ID, CLAIM, expires, os.getpid()),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, user_id_alt, notifier_profile) "
            "VALUES (?, 'discord', '100', '', ?, '', ?)",
            (TASK_ID, OWNER_ID, NOTIFIER_PROFILE),
        )
        conn.commit()
        conn.close()

    def _write_hermes_config(self):
        (self.hermes_home / "config.yaml").write_text(
            "security:\n"
            "  approval:\n"
            "    kanban_transport: kanban-owner\n"
            "approvals:\n"
            "  mode: smart\n"
            "  single_query_mode: deny\n"
        )
        self._clear_config_cache()

    def _write_bridge_config(self):
        data = BridgeConfig(
            db_path=str(self.db_path),
            socket_path=str(self.socket_path),
            owner_id=OWNER_ID,
            notifier_profile=NOTIFIER_PROFILE,
            worker_profile=WORKER_PROFILE,
            max_timeout=5,
        )
        config_path = self.plugin_root / ".local/config.json"
        config_path.write_text(json.dumps(data.__dict__))
        os.chmod(config_path, 0o600)

    def _install_fake_tirith(self):
        self.original_tirith = sys.modules.get("tools.tirith_security")
        sys.modules["tools.tirith_security"] = types.SimpleNamespace(
            check_command_security=lambda _command: {"action": "allow", "findings": [], "summary": ""}
        )

    def _restore_tirith(self):
        if self.original_tirith is None:
            sys.modules.pop("tools.tirith_security", None)
        else:
            sys.modules["tools.tirith_security"] = self.original_tirith

    def _clear_config_cache(self):
        try:
            from hermes_cli import config as hermes_config
            hermes_config._LOAD_CONFIG_CACHE.clear()
            hermes_config._RAW_CONFIG_CACHE.clear()
            hermes_config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
        except Exception:
            pass

    def _load_plugin_present(self):
        spec = importlib.util.spec_from_file_location("kanban_owner_integration", PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ROOT = self.plugin_root
        ctx = Ctx()
        module.register(ctx)
        self.assertEqual(ctx.name, "kanban-owner")
        self.assertIsNotNone(ctx.present)
        return ctx.present

    def _env(self):
        return {
            "HOME": str(self.hermes_home),
            "HERMES_HOME": str(self.hermes_home),
            "HERMES_SINGLE_QUERY_SESSION": "1",
            "HERMES_KANBAN_TASK": TASK_ID,
            "HERMES_KANBAN_RUN_ID": str(RUN_ID),
            "HERMES_KANBAN_CLAIM_LOCK": CLAIM,
            "HERMES_KANBAN_DB": str(self.db_path),
            "HERMES_PROFILE": WORKER_PROFILE,
        }

    def _run_guard(self, adapter: AdapterFixture, pm_review=None, command="docker restart app"):
        service = KanbanApprovalService(SimpleNamespace(
            _gateway_loop=self.loop,
            _running=True,
            adapters={Platform.DISCORD: adapter},
        ))
        if pm_review is not None:
            service = PmApprovalService(service, pm_review)
        broker = Broker(
            BridgeConfig(
                db_path=str(self.db_path),
                socket_path=str(self.socket_path),
                owner_id=OWNER_ID,
                notifier_profile=NOTIFIER_PROFILE,
                worker_profile=WORKER_PROFILE,
                max_timeout=5,
            ),
            service,
        )
        broker.start()
        try:
            with mock.patch.dict(os.environ, self._env(), clear=True):
                present = self._load_plugin_present()
                manager = SimpleNamespace(get_approval_transport=lambda name: RegisteredApprovalTransport(
                    name="kanban-owner",
                    present=present,
                    plugin_id="kanban-owner",
                    profile_home=str(self.hermes_home.resolve()),
                ) if name == "kanban-owner" else None)
                with mock.patch("tools.approval_prompt.get_plugin_manager", return_value=manager):
                    result = approval.check_all_command_guards(command, "local")
                    self.assertEqual(os.environ["HERMES_SINGLE_QUERY_SESSION"], "1")
                    return result
        finally:
            broker.close()

    def test_once_decision_flows_from_core_guard_to_native_owner_facade(self):
        adapter = AdapterFixture(OWNER_ID, choice="once")
        result = self._run_guard(adapter)
        self.assertTrue(result["approved"])
        self.assertEqual(len(adapter.calls), 1)
        chat, prompt, key, view = adapter.calls[0]
        self.assertEqual(chat, "100")
        self.assertEqual({button.label for button in view.children}, {"Allow Once", "Deny"})
        self.assertIn("docker restart app", prompt["content"])
        self.assertEqual(approval.resolve_gateway_approval(key, "once"), 0)

    def test_deny_decision_blocks_core_guard(self):
        adapter = AdapterFixture(OWNER_ID, choice="deny")
        result = self._run_guard(adapter)
        self.assertFalse(result["approved"])
        self.assertIn("did not authorize", result["message"].lower())

    def test_pm_auto_approval_never_sends_a_human_prompt(self):
        adapter = AdapterFixture(OWNER_ID, choice=None)
        review = mock.Mock(return_value={"decision": "approve", "effect": "non_delete", "within_task": True})
        result = self._run_guard(adapter, pm_review=review)
        self.assertTrue(result["approved"])
        review.assert_called_once()
        self.assertEqual(adapter.calls, [])

    def test_hard_delete_uses_native_human_even_if_pm_would_approve(self):
        adapter = AdapterFixture(OWNER_ID, choice="deny")
        review = mock.Mock(return_value={"decision": "approve", "effect": "non_delete", "within_task": True})
        result = self._run_guard(adapter, pm_review=review, command="rm old.txt")
        self.assertFalse(result["approved"])
        review.assert_not_called()
        self.assertEqual(len(adapter.calls), 1)

    def test_reclaimed_run_during_native_prompt_denies_before_worker_authorizes(self):
        def reclaim():
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE task_runs SET status='reclaimed' WHERE id=?", (RUN_ID,))
            conn.commit()
            conn.close()

        adapter = AdapterFixture(OWNER_ID, choice="once", mutate_before_choice=reclaim)
        result = self._run_guard(adapter)
        self.assertFalse(result["approved"])
        self.assertEqual(len(adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
