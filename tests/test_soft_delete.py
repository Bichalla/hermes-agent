import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge.soft_delete import SoftDeleteError, restore, soft_delete
import bridge.soft_delete as soft_delete_module


class SoftDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.trash = self.root / "trash"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_moves_to_private_trash_and_restores(self):
        target = self.workspace / "notes.txt"
        target.write_text("hello")
        receipt = soft_delete(self.workspace, "notes.txt", self.trash)
        self.assertFalse(target.exists())
        payload = Path(receipt["trash_payload"])
        self.assertEqual(payload.read_text(), "hello")
        meta = payload.parent / "receipt.json"
        self.assertEqual(oct(meta.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.trash.stat().st_mode & 0o777), "0o700")
        restored = restore(self.workspace, receipt["receipt_id"], self.trash)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(target.read_text(), "hello")

    def test_directory_tree_restores_without_symlinks(self):
        (self.workspace / "dir" / "sub").mkdir(parents=True)
        (self.workspace / "dir" / "sub" / "file.txt").write_text("data")
        receipt = soft_delete(self.workspace, "dir", self.trash)
        self.assertFalse((self.workspace / "dir").exists())
        restore(self.workspace, receipt["receipt_id"], self.trash)
        self.assertEqual((self.workspace / "dir" / "sub" / "file.txt").read_text(), "data")

    def test_restore_never_overwrites_existing_target(self):
        (self.workspace / "file.txt").write_text("old")
        receipt = soft_delete(self.workspace, "file.txt", self.trash)
        (self.workspace / "file.txt").write_text("new")
        with self.assertRaises(SoftDeleteError):
            restore(self.workspace, receipt["receipt_id"], self.trash)
        self.assertEqual((self.workspace / "file.txt").read_text(), "new")

    def test_restore_uses_atomic_no_overwrite_when_target_appears_late(self):
        (self.workspace / "file.txt").write_text("old")
        receipt = soft_delete(self.workspace, "file.txt", self.trash)
        original_validate = soft_delete_module._validate_payload

        def create_late_target(payload, data):
            original_validate(payload, data)
            (self.workspace / "file.txt").write_text("new")

        with mock.patch("bridge.soft_delete._validate_payload", side_effect=create_late_target):
            with self.assertRaises(SoftDeleteError):
                restore(self.workspace, receipt["receipt_id"], self.trash)
        self.assertEqual((self.workspace / "file.txt").read_text(), "new")
        self.assertTrue(Path(receipt["trash_payload"]).exists())

    def test_restore_recovers_when_final_metadata_write_failed_after_move(self):
        (self.workspace / "file.txt").write_text("old")
        original_write = soft_delete_module._write_private_json
        calls = 0

        def fail_second_write(path, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("metadata fsync failed")
            return original_write(path, payload)

        with mock.patch("bridge.soft_delete._write_private_json", side_effect=fail_second_write):
            with self.assertRaises(RuntimeError):
                soft_delete(self.workspace, "file.txt", self.trash)
        receipts = list((self.trash / "receipts").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["status"], "prepared")
        receipt_id = receipts[0].parent.name
        self.assertFalse((self.workspace / "file.txt").exists())
        restore(self.workspace, receipt_id, self.trash)
        self.assertEqual((self.workspace / "file.txt").read_text(), "old")

    def test_path_traversal_workspace_root_git_and_symlink_are_refused(self):
        (self.workspace / "file.txt").write_text("x")
        (self.workspace / ".git").mkdir()
        (self.workspace / "link").symlink_to(self.workspace / "file.txt")
        cases = ["../workspace/file.txt", ".", ".git", ".git/config", "link"]
        for target in cases:
            with self.subTest(target=target):
                with self.assertRaises(SoftDeleteError):
                    soft_delete(self.workspace, target, self.trash)

    def test_refuses_target_that_contains_trash_root(self):
        nested_trash = self.workspace / "dir" / ".trash"
        (self.workspace / "dir" / "sub").mkdir(parents=True)
        with self.assertRaises(SoftDeleteError):
            soft_delete(self.workspace, "dir", nested_trash)

    def test_directory_containing_symlink_is_refused(self):
        (self.workspace / "dir").mkdir()
        (self.workspace / "outside.txt").write_text("x")
        (self.workspace / "dir" / "link").symlink_to(self.workspace / "outside.txt")
        with self.assertRaises(SoftDeleteError):
            soft_delete(self.workspace, "dir", self.trash)
        self.assertTrue((self.workspace / "dir").exists())

    def test_cross_device_rename_fails_without_copy_delete(self):
        target = self.workspace / "file.txt"
        target.write_text("x")

        def raise_exdev(_src, _dst):
            raise OSError(errno.EXDEV, "cross-device")

        with mock.patch("bridge.soft_delete.os.rename", side_effect=raise_exdev):
            with self.assertRaises(SoftDeleteError):
                soft_delete(self.workspace, "file.txt", self.trash)
        self.assertEqual(target.read_text(), "x")
        receipts = list((self.trash / "receipts").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())["status"], "prepared")

    def test_special_file_is_refused(self):
        fifo = self.workspace / "pipe"
        os.mkfifo(fifo)
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        with self.assertRaises(SoftDeleteError):
            soft_delete(self.workspace, "pipe", self.trash)


if __name__ == "__main__":
    unittest.main()
