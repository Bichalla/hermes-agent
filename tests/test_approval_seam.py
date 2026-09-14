"""Candidate approval seam fixtures; no real broker, Discord, or command execution."""

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import builtins

from tools import approval


KANBAN_ENV = {
    "HERMES_SINGLE_QUERY_SESSION": "1",
    "HERMES_KANBAN_TASK": "t_fixture",
    "HERMES_KANBAN_RUN_ID": "17",
    "HERMES_KANBAN_CLAIM_LOCK": "claim-secret",
    "HERMES_KANBAN_DB": "/tmp/kanban.db",
    "HERMES_PROFILE": "work-executor",
}


def _dangerous(command):
    return True, "container_lifecycle", "docker restart/stop/kill (container lifecycle)"


class ApprovalSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._write_config(
            "security:\n"
            "  approval:\n"
            "    kanban_transport: kanban-owner\n"
            "approvals:\n"
            "  mode: smart\n"
            "  single_query_mode: deny\n"
        )
        env = {**KANBAN_ENV, "HOME": str(self.home), "HERMES_HOME": str(self.home)}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.patches = [
            mock.patch("tools.approval.approval_context._get_approval_mode", return_value="smart"),
            mock.patch("tools.approval.detect_dangerous_command", side_effect=_dangerous),
            mock.patch("tools.approval._tirith_fail_open", return_value=True),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_config(self, text: str) -> None:
        (self.home / "config.yaml").write_text(text)
        try:
            from hermes_cli import config as hermes_config
            hermes_config._LOAD_CONFIG_CACHE.clear()
            hermes_config._RAW_CONFIG_CACHE.clear()
            hermes_config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
        except Exception:
            pass

    def test_kanban_worker_once_approves_through_selected_transport(self):
        def present(**kwargs):
            return {"selected": True, "name": "kanban-owner", "choice": "once", "failure": None}

        with mock.patch("tools.approval._present_with_kanban_owner_transport", side_effect=present) as transport:
            result = approval.check_all_command_guards("docker restart app", "local")
        self.assertTrue(result["approved"])
        transport.assert_called_once()

    def test_transport_deny_or_failure_blocks(self):
        cases = [
            {"selected": True, "name": "kanban-owner", "choice": "deny", "failure": None},
            {"selected": True, "name": "kanban-owner", "choice": "deny", "failure": "timeout"},
            {"selected": False},
        ]
        for attempt in cases:
            with self.subTest(attempt=attempt), mock.patch(
                "tools.approval._present_with_kanban_owner_transport", return_value=attempt
            ):
                result = approval.check_all_command_guards("docker restart app", "local")
            self.assertFalse(result["approved"])

    def test_missing_kanban_identity_keeps_single_query_deny(self):
        with mock.patch.dict(os.environ, {"HERMES_SINGLE_QUERY_SESSION": "1"}, clear=True):
            with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
                result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertIn("single-query mode", result["message"])
        transport.assert_not_called()

    def test_missing_kanban_transport_key_keeps_single_query_deny(self):
        self._write_config("security:\n  approval: {}\napprovals:\n  mode: smart\n  single_query_mode: deny\n")
        with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
            result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertIn("single-query mode", result["message"])
        transport.assert_not_called()

    def test_global_transport_key_does_not_select_kanban_bridge(self):
        self._write_config(
            "security:\n"
            "  approval:\n"
            "    transport: kanban-owner\n"
            "approvals:\n"
            "  mode: smart\n"
            "  single_query_mode: deny\n"
        )
        with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
            result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertIn("single-query mode", result["message"])
        transport.assert_not_called()

    def test_top_level_approvals_kanban_transport_does_not_select_bridge(self):
        self._write_config(
            "approvals:\n"
            "  mode: smart\n"
            "  single_query_mode: deny\n"
            "  kanban_transport: kanban-owner\n"
        )
        with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
            result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertIn("single-query mode", result["message"])
        transport.assert_not_called()

    def test_cron_and_unattended_platform_do_not_use_kanban_bridge(self):
        base = {k: v for k, v in KANBAN_ENV.items() if k != "HERMES_SINGLE_QUERY_SESSION"}
        for extra_env, message in [
            ({"HERMES_CRON_SESSION": "1"}, "cron jobs"),
            ({"HERMES_SESSION_PLATFORM": "webhook"}, "unattended platform"),
        ]:
            with self.subTest(extra_env=extra_env):
                with mock.patch.dict(os.environ, {**base, **extra_env}, clear=True):
                    with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
                        result = approval.check_all_command_guards("docker restart app", "local")
                self.assertFalse(result["approved"])
                self.assertIn(message, result["message"])
                transport.assert_not_called()

    def test_tirith_import_failure_is_not_sent_to_bridge(self):
        real_import = builtins.__import__

        def import_without_tirith(name, *args, **kwargs):
            if name == "tools.tirith_security":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("tools.approval._tirith_fail_open", return_value=False):
            with mock.patch.object(builtins, "__import__", side_effect=import_without_tirith):
                with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
                    result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertIn("Tirith security scanner could not be imported", result["message"])
        transport.assert_not_called()

    def test_hardline_floor_blocks_before_kanban_bridge(self):
        with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
            result = approval.check_all_command_guards("rm -rf /", "local")
        self.assertFalse(result["approved"])
        self.assertIn("hardline", result["message"].lower())
        transport.assert_not_called()

    def test_user_deny_rule_blocks_before_kanban_bridge(self):
        with mock.patch(
            "tools.approval.approval_context._get_approval_config",
            return_value={"deny": ["docker restart *"]},
        ):
            with mock.patch("tools.approval._present_with_kanban_owner_transport") as transport:
                result = approval.check_all_command_guards("docker restart app", "local")
        self.assertFalse(result["approved"])
        self.assertTrue(result.get("user_deny"))
        self.assertIn("user-defined deny rule", result["message"].lower())
        transport.assert_not_called()

    def test_tirith_and_dangerous_findings_are_presented_together(self):
        fake_tirith = types.SimpleNamespace(
            check_command_security=lambda _command: {
                "action": "warn",
                "findings": [{
                    "rule_id": "fixture-rule",
                    "severity": "HIGH",
                    "title": "Scanner warning",
                    "description": "scanner detail",
                }],
            }
        )
        original = sys.modules.get("tools.tirith_security")
        sys.modules["tools.tirith_security"] = fake_tirith
        self.addCleanup(lambda: sys.modules.pop("tools.tirith_security", None)
                        if original is None else sys.modules.__setitem__("tools.tirith_security", original))

        def present(**kwargs):
            present.kwargs = kwargs
            return {"selected": True, "name": "kanban-owner", "choice": "once", "failure": None}

        with mock.patch("tools.approval._present_with_kanban_owner_transport", side_effect=present):
            result = approval.check_all_command_guards("docker restart app", "local")
        self.assertTrue(result["approved"])
        self.assertEqual(present.kwargs["pattern_keys"], ["tirith:fixture-rule", "container_lifecycle"])
        self.assertIn("Scanner warning", present.kwargs["description"])
        self.assertIn("docker restart/stop/kill", present.kwargs["description"])


if __name__ == "__main__":
    unittest.main()
