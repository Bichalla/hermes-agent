"""Delegated PM judgments never impersonate a human deletion approval."""
import time
import unittest
from unittest import mock

from bridge.pm import PmApprovalService


class PmPolicyTests(unittest.TestCase):
    def run_request(self, command, judgment=None, cancel=lambda: False):
        human = mock.Mock()
        human.request.return_value = "once"
        reviewer = mock.Mock(return_value=judgment or {
            "decision": "approve", "effect": "non_delete", "within_task": True,
        })
        service = PmApprovalService(human, reviewer)
        result = service.request({
            "command": command, "description": "development", "request_id": "fixture",
            "task_id": "t_fixture", "run_id": 1, "task_context": {"title": "Build the app"},
        }, {"owner_id": "1"}, time.time() + 30, cancel)
        return result, human, reviewer

    def test_routine_work_pm_approves_without_human(self):
        result, human, reviewer = self.run_request("docker restart app")
        self.assertEqual(result, "once")
        human.request.assert_not_called()
        reviewer.assert_called_once()

    def test_hard_delete_never_delegates_to_model(self):
        for command in ["rm file", "rm -rf build", "find . -delete", "git clean -fd",
                        "docker volume rm data", "sqlite3 app.db 'DELETE FROM users'",
                        "python -c 'import os; os.unlink(\"data\")'", "shred data"]:
            with self.subTest(command=command):
                result, human, reviewer = self.run_request(command)
                self.assertEqual(result, "once")
                human.request.assert_called_once()
                reviewer.assert_not_called()

    def test_model_classified_hard_delete_requires_human(self):
        result, human, _ = self.run_request("appctl cleanup", {
            "decision": "approve", "effect": "hard_delete", "within_task": True,
        })
        self.assertEqual(result, "once")
        human.request.assert_called_once()

    def test_soft_delete_label_is_not_proof_of_recovery(self):
        result, human, _ = self.run_request("appctl soft-delete", {
            "decision": "approve", "effect": "soft_delete", "within_task": True,
        })
        self.assertEqual(result, "deny")
        human.request.assert_not_called()

    def test_uncertain_out_of_scope_or_malformed_deny_without_human_spam(self):
        for judgment in [{}, {"decision": "approve", "effect": "unknown", "within_task": True},
                         {"decision": "approve", "effect": "non_delete", "within_task": False},
                         {"decision": "deny", "effect": "non_delete", "within_task": True}]:
            with self.subTest(judgment=judgment):
                # A nonempty invalid value avoids the fixture's default response.
                result, human, _ = self.run_request("appctl build", judgment or {"invalid": True})
                self.assertEqual(result, "deny")
                human.request.assert_not_called()

    def test_cancelled_request_never_calls_any_approver(self):
        result, human, reviewer = self.run_request("docker restart app", cancel=lambda: True)
        self.assertEqual(result, "deny")
        human.request.assert_not_called()
        reviewer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
