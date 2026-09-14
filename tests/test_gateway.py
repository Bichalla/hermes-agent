"""Isolated native queue fixtures; no Discord connection or human approval."""
import asyncio
import threading
import time
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest import mock

from gateway.config import Platform
from gateway.kanban_approval import KanbanApprovalService
from hermes_cli.approval_transport import ApprovalRequest
from tools import approval


class AdapterFixture:
    def __init__(self):
        self._allowed_user_ids = {"123"}
        self._allowed_role_ids = set()
        self.choice = "once"
        self.sent = threading.Event()
        self.delivery_success = True
        self.calls = []

    def _discord_allow_all_users(self):
        return False

    async def _send_prompt(self, chat, metadata, build):
        kwargs, view = build(None)
        key = view.session_key
        self.calls.append((chat, kwargs, key, view))
        self.sent.set()
        if self.choice is not None:
            approval.resolve_gateway_approval(key, self.choice)
        return SimpleNamespace(success=self.delivery_success)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(self.ready.set)
            self.loop.run_forever()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(self.ready.wait(3))
        self.adapter = AdapterFixture()
        self.gateway = SimpleNamespace(
            _gateway_loop=self.loop, _running=True,
            adapters={Platform.DISCORD: self.adapter},
        )
        self.service = KanbanApprovalService(self.gateway)
        req = ApprovalRequest.create(
            command="echo isolated-fixture", description="fixture only",
            pattern_key="fixture", pattern_keys=("fixture",), session_key="fixture",
            surface="kanban_worker", allow_session=False, allow_permanent=False,
            timeout_seconds=5,
        )
        self.data = {**asdict(req), "task_id": "t_fixture", "run_id": 1}
        self.route = {"platform": "discord", "owner_id": "123", "chat_id": "456", "thread_id": "789"}

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(3)
        self.loop.close()

    def request(self, cancel=lambda: False, timeout=3):
        return self.service.request(self.data, self.route, time.time() + timeout, cancel)

    def test_native_queue_once_is_single_use_and_scoped(self):
        self.assertEqual(self.request(), "once")
        chat, prompt, key, view = self.adapter.calls[0]
        self.assertEqual(chat, "789")
        self.assertIn(self.data["command"], prompt["content"])
        self.assertEqual({button.label for button in view.children}, {"Allow Once", "Deny"})
        self.assertEqual(approval.resolve_gateway_approval(key, "once"), 0)
        self.assertNotIn(key, approval._gateway_queues)

    def test_global_and_paired_users_cannot_click_owner_request(self):
        self.assertEqual(self.request(), "once")
        view = self.adapter.calls[0][3]
        owner = SimpleNamespace(user=SimpleNamespace(id=123))
        stranger = SimpleNamespace(user=SimpleNamespace(id=999))
        with mock.patch("plugins.platforms.discord.adapter._scoped_gate_env", return_value="999"):
            with mock.patch("gateway.pairing.PairingStore.is_approved", return_value=True):
                self.assertTrue(view._check_auth(owner))
                self.assertFalse(view._check_auth(stranger))
        with mock.patch("plugins.platforms.discord.adapter._scoped_gate_env", return_value="true"):
            self.assertFalse(view._check_auth(stranger))

    def test_deny_and_broad_choices_are_not_once(self):
        for choice in ["deny", "session", "always"]:
            with self.subTest(choice=choice):
                self.adapter.choice = choice
                self.assertEqual(self.request(), "deny")

    def test_failed_send_cannot_approve_even_with_queued_result(self):
        self.adapter.delivery_success = False
        self.assertEqual(self.request(), "deny")

    def test_owner_mismatch_and_roles_never_send(self):
        for users, roles in [({"999"}, set()), ({"123", "999"}, set()), ({"123"}, {42})]:
            self.adapter._allowed_user_ids, self.adapter._allowed_role_ids = users, roles
            self.assertEqual(self.request(), "deny")
        self.assertEqual(self.adapter.calls, [])

    def test_deadline_cancels_native_queue(self):
        self.adapter.choice = None
        self.assertEqual(self.request(timeout=2), "deny")
        self.assertNotIn(self.adapter.calls[0][2], approval._gateway_queues)

    def test_disconnect_or_reclaim_cancels_wait(self):
        self.adapter.choice = None
        self.assertEqual(self.request(cancel=self.adapter.sent.is_set), "deny")

    def test_gateway_shutdown_cancels_wait(self):
        self.adapter.choice = None
        def shutdown_after_send():
            self.adapter.sent.wait(2)
            self.gateway._running = False
        t = threading.Thread(target=shutdown_after_send)
        t.start()
        self.assertEqual(self.request(), "deny")
        t.join(3)

    def test_expired_or_truncated_or_fenced_display_never_sends(self):
        self.assertEqual(self.request(timeout=-1), "deny")
        for command in ["x" * 1201, "echo ```malformed"]:
            self.data["command"] = command
            self.assertEqual(self.request(), "deny")
        self.assertEqual(self.adapter.calls, [])

    def test_loop_thread_refuses_blocking_call(self):
        async def call():
            return self.request()
        self.assertEqual(asyncio.run_coroutine_threadsafe(call(), self.loop).result(3), "deny")
        self.assertEqual(self.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
