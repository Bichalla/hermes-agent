import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from bridge.broker import BridgeConfig
from bridge.worker import WorkerIdentity
from bridge.worker_tools import register_tools


class Ctx:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.sections = {}

    def register_tool(self, name, **kwargs):
        self.tools[name] = kwargs

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_system_prompt_section(self, name, content):
        self.sections[name] = content


class WorkerToolsTests(unittest.TestCase):
    def test_apply_patch_delete_shapes_are_blocked(self):
        ctx = Ctx()
        register_tools(ctx, None, None, Path("/unused"))
        hook = ctx.hooks["pre_tool_call"]
        for args in [{"patch": "*** Delete File: app.py"},
                     {"changes": [{"kind": "delete", "path": "app.py"}]},
                     {"changes": [{"kind": {"type": "delete"}, "path": "app.py"}]}]:
            self.assertEqual(hook(tool_name="apply_patch", args=args)["action"], "block")
        self.assertIsNone(hook(tool_name="apply_patch", args={"changes": [{"kind": "update"}]}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db_path = self.root / "kanban.db"
        self.task_id = "t_soft"
        self.run_id = 1
        self.claim = "claim-soft"
        self.profile = "work-executor"
        self.owner = "1494011544214835201"
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
                worker_pid INTEGER,
                workspace_path TEXT
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
            "INSERT INTO task_runs(id, task_id, profile, status, claim_lock, claim_expires, worker_pid) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (self.run_id, self.task_id, self.profile, self.claim, expires, os.getpid()),
        )
        conn.execute(
            "INSERT INTO tasks(id, status, current_run_id, claim_lock, claim_expires, worker_pid, workspace_path) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?)",
            (self.task_id, self.run_id, self.claim, expires, os.getpid(), str(self.workspace)),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, user_id_alt, notifier_profile) "
            "VALUES (?, 'discord', '100', '', ?, '', 'work-pm')",
            (self.task_id, self.owner),
        )
        conn.commit()
        conn.close()

    def _config(self):
        return BridgeConfig(
            db_path=str(self.db_path),
            socket_path=str(self.root / "approval.sock"),
            owner_id=self.owner,
            notifier_profile="work-pm",
            worker_profile=self.profile,
        )

    def _identity(self):
        return WorkerIdentity(
            task_id=self.task_id,
            run_id=str(self.run_id),
            claim_lock=self.claim,
            db_path=str(self.db_path),
            profile=self.profile,
            worker_pid=os.getpid(),
        )

    def test_soft_delete_and_restore_tools_are_workspace_scoped(self):
        (self.workspace / "file.txt").write_text("data")
        ctx = Ctx()
        register_tools(ctx, self._identity(), self._config(), self.root)
        self.assertIn("kanban_soft_delete", ctx.tools)
        self.assertIn("kanban_restore", ctx.tools)
        self.assertIn("pre_tool_call", ctx.hooks)
        self.assertIn("kanban-deletion-policy", ctx.sections)

        deleted = json.loads(ctx.tools["kanban_soft_delete"]["handler"]({"path": "file.txt"}))
        self.assertIn("receipt_id", deleted)
        self.assertFalse((self.workspace / "file.txt").exists())
        restored = json.loads(ctx.tools["kanban_restore"]["handler"]({"receipt_id": deleted["receipt_id"]}))
        self.assertEqual(restored["status"], "restored")
        self.assertEqual((self.workspace / "file.txt").read_text(), "data")

    def test_patch_delete_hook_blocks_hard_file_deletion(self):
        ctx = Ctx()
        register_tools(ctx, self._identity(), self._config(), self.root)
        result = ctx.hooks["pre_tool_call"]("patch", {"patch": "*** Delete File: app.py\n"})
        self.assertEqual(result["action"], "block")
        self.assertIn("kanban_soft_delete", result["message"])

    def test_stale_claim_refuses_without_moving_data(self):
        (self.workspace / "file.txt").write_text("data")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE task_runs SET status='reclaimed' WHERE id=?", (self.run_id,))
        conn.commit()
        conn.close()
        ctx = Ctx()
        register_tools(ctx, self._identity(), self._config(), self.root)
        result = json.loads(ctx.tools["kanban_soft_delete"]["handler"]({"path": "file.txt"}))
        self.assertIn("error", result)
        self.assertEqual((self.workspace / "file.txt").read_text(), "data")


if __name__ == "__main__":
    unittest.main()
