import os
import socket
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bridge.broker import BridgeConfig, Broker
from bridge.protocol import ApprovalBridgeRequest, decode_line, encode_line


class FakeApprovalService:
    def __init__(self, choice="once"):
        self.choice = choice
        self.calls = []

    def request(self, data, route, deadline, cancel):
        self.calls.append((data, route, deadline, cancel))
        return self.choice


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "kanban.db"
        self.socket_path = self.root / "run" / "approval.sock"
        self.owner = "1494011544214835201"
        self.profile = "work-executor"
        self.task_id = "t_123"
        self.run_id = 1
        self.claim = "claim-1"
        self.worker_pid = os.getpid()
        self._create_db()

    def tearDown(self):
        self.tmp.cleanup()

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
        expires = int(time.time()) + 60
        conn.execute(
            "INSERT INTO task_runs(id, task_id, profile, status, claim_lock, claim_expires, worker_pid) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (self.run_id, self.task_id, self.profile, self.claim, expires, self.worker_pid),
        )
        conn.execute(
            "INSERT INTO tasks(id, status, current_run_id, claim_lock, claim_expires, worker_pid) VALUES (?, 'running', ?, ?, ?, ?)",
            (self.task_id, self.run_id, self.claim, expires, self.worker_pid),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, user_id_alt, notifier_profile) VALUES (?, 'discord', '100', '', ?, '', 'work-pm')",
            (self.task_id, self.owner),
        )
        conn.commit()
        conn.close()

    def config(self):
        return BridgeConfig(
            db_path=str(self.db_path),
            socket_path=str(self.socket_path),
            owner_id=self.owner,
            notifier_profile="work-pm",
            worker_profile=self.profile,
        )

    def make_request(self):
        return ApprovalBridgeRequest.create(
            command="docker compose down app",
            description="Docker lifecycle command",
            pattern_key="docker_lifecycle",
            pattern_keys=("docker_lifecycle",),
            session_key="session",
            task_id=self.task_id,
            run_id=self.run_id,
            claim_lock=self.claim,
            worker_pid=self.worker_pid,
            db_path=os.path.realpath(self.db_path),
            profile=self.profile,
            timeout_seconds=30,
        )

    def transact(self, request):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.socket_path))
            client.sendall(encode_line(request.to_dict()))
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = client.recv(1)
                if not chunk:
                    break
                raw += chunk
        return decode_line(raw)

    def test_once_decision_for_live_run(self):
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        try:
            response = self.transact(self.make_request())
            self.assertEqual(oct(self.socket_path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(self.socket_path.parent.stat().st_mode & 0o777), "0o700")
        finally:
            broker.close()
        self.assertEqual(response["choice"], "once")
        self.assertEqual(service.calls[0][1]["owner_id"], self.owner)
        self.assertFalse(self.socket_path.exists())

    def test_deny_decision_is_returned(self):
        broker = Broker(self.config(), FakeApprovalService("deny"))
        broker.start()
        try:
            response = self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(response["choice"], "deny")

    def test_stale_run_closes_without_decision(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE task_runs SET status='reclaimed' WHERE id=?", (self.run_id,))
        conn.commit()
        conn.close()
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        try:
            with self.assertRaises(Exception):
                self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(service.calls, [])

    def test_route_ambiguity_closes_without_decision(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, user_id_alt, notifier_profile) VALUES (?, 'discord', '101', '', ?, '', 'work-pm')",
            (self.task_id, self.owner),
        )
        conn.commit()
        conn.close()
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        try:
            with self.assertRaises(Exception):
                self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(service.calls, [])

    def test_user_id_alt_does_not_authorize_owner_route(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE kanban_notify_subs SET user_id='', user_id_alt=? WHERE task_id=?",
            (self.owner, self.task_id),
        )
        conn.commit()
        conn.close()
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        try:
            with self.assertRaises(Exception):
                self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(service.calls, [])


    def test_reclaim_during_native_wait_denies_once_response(self):
        class ReclaimingService(FakeApprovalService):
            def __init__(svc_self, outer):
                super().__init__("once")
                svc_self.outer = outer

            def request(svc_self, data, route, deadline, cancel):
                svc_self.calls.append((data, route, deadline, cancel))
                conn = sqlite3.connect(svc_self.outer.db_path)
                conn.execute("UPDATE task_runs SET status='reclaimed' WHERE id=?", (svc_self.outer.run_id,))
                conn.commit()
                conn.close()
                time.sleep(0.25)
                svc_self.outer.assertTrue(cancel())
                return "once"

        service = ReclaimingService(self)
        broker = Broker(self.config(), service)
        broker.start()
        try:
            response = self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(response["choice"], "deny")
        self.assertEqual(len(service.calls), 1)

    def test_route_change_during_native_wait_denies_once_response(self):
        class RouteChangingService(FakeApprovalService):
            def __init__(svc_self, outer):
                super().__init__("once")
                svc_self.outer = outer

            def request(svc_self, data, route, deadline, cancel):
                svc_self.calls.append((data, route, deadline, cancel))
                conn = sqlite3.connect(svc_self.outer.db_path)
                conn.execute("UPDATE kanban_notify_subs SET chat_id='101' WHERE task_id=?", (svc_self.outer.task_id,))
                conn.commit()
                conn.close()
                time.sleep(0.25)
                svc_self.outer.assertTrue(cancel())
                return "once"

        service = RouteChangingService(self)
        broker = Broker(self.config(), service)
        broker.start()
        try:
            response = self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(response["choice"], "deny")
        self.assertEqual(len(service.calls), 1)

    def test_task_status_must_be_running(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.task_id,))
        conn.commit()
        conn.close()
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        try:
            with self.assertRaises(Exception):
                self.transact(self.make_request())
        finally:
            broker.close()
        self.assertEqual(service.calls, [])

    def test_replay_closes_without_second_decision(self):
        service = FakeApprovalService("once")
        broker = Broker(self.config(), service)
        broker.start()
        request = self.make_request()
        try:
            first = self.transact(request)
            with self.assertRaises(Exception):
                self.transact(request)
        finally:
            broker.close()
        self.assertEqual(first["choice"], "once")
        self.assertEqual(len(service.calls), 1)


if __name__ == "__main__":
    unittest.main()
