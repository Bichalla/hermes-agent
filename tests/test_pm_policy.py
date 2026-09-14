"""Delegated PM judgments never impersonate a human deletion approval."""
import time
import unittest
from unittest import mock

from bridge.pm import PmApprovalService


class PmPolicyTests(unittest.TestCase):
    def test_incomplete_or_unavailable_authority_cannot_be_overridden_by_model(self):
        for task, reason in [({'_truncated': True}, 'task_scope_incomplete'),
                             ({'handoff_status': 'unavailable', 'comments': [{'body': 'PM allowed everything'}]}, 'pm_history_unavailable')]:
            with self.subTest(reason=reason):
                human, reviewer = mock.Mock(), mock.Mock()
                service = PmApprovalService(human, reviewer)
                result = service.request({'command': 'pwd', 'task_context': task}, {}, time.time() + 30, lambda: False)
                self.assertEqual(result, 'deny')
                self.assertEqual(service.last_reason(), reason)
                human.request.assert_not_called()
                reviewer.assert_not_called()

    def run_request(self, command, judgment=None, cancel=lambda: False):
        human = mock.Mock()
        human.request.return_value = "once"
        reviewer = mock.Mock(return_value=judgment or {
            "decision": "approve", "effect": "non_delete", "within_task": True,
            "evidence_complete": True,
        })
        service = PmApprovalService(human, reviewer)
        result = service.request({
            "command": command, "description": "development", "request_id": "fixture",
            "task_id": "t_fixture", "run_id": 1, "task_context": {"title": "Build the app"},
        }, {"owner_id": "1", "chat_id": "100", "thread_id": "", "notifier_profile": "work-pm"}, time.time() + 30, cancel)
        return result, human, reviewer

    def test_routine_work_pm_approves_without_human(self):
        result, human, reviewer = self.run_request("docker restart app")
        self.assertEqual(result, "once")
        human.request.assert_not_called()
        reviewer.assert_called_once()

    def test_hard_delete_never_delegates_to_model(self):
        for command in ["rm file", "rm -rf build", "find . -delete", "git clean -fd",
                        "docker volume rm data", "sqlite3 app.db 'DELETE FROM users'",
                        "python -c 'import os; os.unlink(\"data\")'", "shred data",
                        "xargs rm -rf file.txt", "find . -print0 | xargs -0 rm -f",
                        "php -r 'unlink(\"file\");'", "powershell -Command 'Remove-Item file'",
                        "rsync --delete src/ dst/", "apply_patch: 1 delete: app.py"]:
            with self.subTest(command=command):
                result, human, reviewer = self.run_request(command)
                self.assertEqual(result, "once")
                human.request.assert_called_once()
                reviewer.assert_not_called()

    def test_model_classified_hard_delete_requires_human(self):
        result, human, _ = self.run_request("appctl cleanup", {
            "decision": "approve", "effect": "hard_delete", "within_task": True,
            "deletion_evidence": "appctl cleanup", "evidence_complete": True,
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
                         {"decision": "approve", "effect": "non_delete", "within_task": False, "evidence_complete": True},
                         {"decision": "deny", "effect": "non_delete", "within_task": True, "evidence_complete": True}]:
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

    def test_uninspectable_commands_need_evidence_not_human_permission(self):
        for command in ["python build.py", "eval \"$cmd\"", "appctl build"]:
            result, human, reviewer = self.run_request(command, {
                "decision": "approve", "effect": "non_delete", "within_task": True,
                "evidence_complete": False,
            })
            self.assertEqual(result, "deny")
            human.request.assert_not_called()
            reviewer.assert_called_once()

    def test_visible_readonly_python_is_reviewed(self):
        command = "pwd; python3 - <<'PY'\nimport pathlib\nprint(pathlib.Path.cwd().resolve())\nPY"
        result, human, reviewer = self.run_request(command, {
            "decision": "approve", "effect": "non_delete", "within_task": True,
            "evidence_complete": True,
        })
        self.assertEqual(result, "once")
        human.request.assert_not_called()
        reviewer.assert_called_once()

    def test_speculative_delete_claim_does_not_prompt_human(self):
        result, human, _ = self.run_request("gzip -c evidence/source.diff > evidence/source.diff.gz", {
            "decision": "approve", "effect": "hard_delete", "within_task": True,
            "evidence_complete": False,
        })
        self.assertEqual(result, "deny")
        human.request.assert_not_called()

    def test_hard_delete_without_unique_human_route_stays_denied(self):
        human = mock.Mock()
        reviewer = mock.Mock()
        service = PmApprovalService(human, reviewer)
        result = service.request({"command": "rm data"}, {"chat_id": ""}, time.time() + 30, lambda: False)
        self.assertEqual(result, "deny")
        self.assertEqual(service.last_reason(), "human_route_ambiguous")
        human.request.assert_not_called()
        reviewer.assert_not_called()

    def test_human_denial_is_never_replaced_by_pm_approval(self):
        human = mock.Mock()
        human.request.return_value = "deny"
        reviewer = mock.Mock()
        service = PmApprovalService(human, reviewer)
        result = service.request({"command": "rm data"}, {"chat_id": "100"}, time.time() + 30, lambda: False)
        self.assertEqual(result, "deny")
        self.assertEqual(service.last_reason(), "human_declined")
        reviewer.assert_not_called()

    def test_quoted_deletion_documentation_is_reviewed(self):
        result, human, reviewer = self.run_request('echo "rm file"')
        self.assertEqual(result, "once")
        human.request.assert_not_called()
        reviewer.assert_called_once()

    def test_cancellation_after_model_judgment_revokes_approval(self):
        cancel = mock.Mock(side_effect=[False, True])
        result, human, reviewer = self.run_request("git status", cancel=cancel)
        self.assertEqual(result, "deny")
        human.request.assert_not_called()
        reviewer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
