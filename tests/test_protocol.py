import unittest
from dataclasses import replace

from bridge.protocol import (
    ApprovalBridgeDecision,
    ApprovalBridgeRequest,
    ProtocolError,
    decode_line,
    encode_line,
    MAX_COMMAND_DISPLAY,
)


class ProtocolTests(unittest.TestCase):
    def make_request(self):
        return ApprovalBridgeRequest.create(
            command="docker compose down app",
            description="Docker lifecycle command",
            pattern_key="docker_lifecycle",
            pattern_keys=("docker_lifecycle",),
            session_key="session",
            task_id="t_1",
            run_id=7,
            claim_lock="claim",
            worker_pid=1234,
            db_path="/tmp/kanban.db",
            profile="work-executor",
            timeout_seconds=30,
            now=1000.0,
        )

    def test_round_trips_request_and_decision(self):
        request = ApprovalBridgeRequest.from_dict(decode_line(encode_line(self.make_request().to_dict())))
        decision = ApprovalBridgeDecision.from_dict({
            "request_id": request.request_id,
            "request_digest": request.digest,
            "choice": "once",
        }, request)
        self.assertEqual(decision.choice, "once")

    def test_rejects_digest_mismatch(self):
        payload = self.make_request().to_dict()
        payload["task_id"] = "t_other"
        with self.assertRaises(ProtocolError):
            ApprovalBridgeRequest.from_dict(payload)

    def test_rejects_oversize_command(self):
        payload = self.make_request().to_dict()
        payload["command"] = "x" * (MAX_COMMAND_DISPLAY + 1)
        with self.assertRaisesRegex(ProtocolError, 'command too long'):
            ApprovalBridgeRequest.from_dict(payload)

    def test_pm_envelope_preserves_long_command_findings_and_cwd(self):
        original = replace(self.make_request(), command='echo ' + 'a' * 3000,
                           description='scanner finding; ' * 100,
                           execution={'cwd': '/tmp/task', 'tool_call_id': 'call1'}).with_digest()
        received = ApprovalBridgeRequest.from_dict(decode_line(encode_line(original.to_dict())))
        self.assertEqual(received, original)
        changed = original.to_dict()
        changed['execution'] = {'cwd': '/tmp/other', 'tool_call_id': 'call1'}
        with self.assertRaisesRegex(ProtocolError, 'digest mismatch'):
            ApprovalBridgeRequest.from_dict(changed)

    def test_rejects_decision_mismatch(self):
        request = self.make_request()
        with self.assertRaises(ProtocolError):
            ApprovalBridgeDecision.from_dict({
                "request_id": request.request_id,
                "request_digest": "bad",
                "choice": "once",
            }, request)

    def test_rejects_unknown_and_non_integral_fields(self):
        payload = self.make_request().to_dict()
        payload["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            ApprovalBridgeRequest.from_dict(payload)

        for field, value in [("run_id", "7"), ("run_id", 7.5), ("worker_pid", True), ("timeout_seconds", "30")]:
            bad = self.make_request().to_dict()
            bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProtocolError):
                ApprovalBridgeRequest.from_dict(bad)


if __name__ == "__main__":
    unittest.main()
