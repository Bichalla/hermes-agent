"""Worker transport fixtures; no broker daemon or human approval is used."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge import worker
from bridge.broker import BridgeConfig
from hermes_cli.approval_transport import ApprovalRequest


def make_request(**updates):
    args = dict(
        command="docker restart app", description="container lifecycle",
        pattern_key="container_lifecycle", pattern_keys=("container_lifecycle",),
        session_key="fixture", surface="kanban_worker", allow_session=False,
        allow_permanent=False, timeout_seconds=30,
    )
    args.update(updates)
    return ApprovalRequest.create(**args)


class WorkerTransportTests(unittest.TestCase):
    def config(self):
        return BridgeConfig(
            db_path="/tmp/kanban.db",
            socket_path="/tmp/socket",
            owner_id="1494011544214835201",
            notifier_profile="work-pm",
            worker_profile="work-executor",
        )

    def validator(self, _config, _bridge_request, _now):
        return {"platform": "discord", "chat_id": "100", "thread_id": "", "owner_id": "1494011544214835201"}

    def identity(self):
        return worker.WorkerIdentity(
            task_id="t_fixture", run_id="17", claim_lock="claim-secret",
            db_path="/tmp/kanban.db", profile="work-executor", worker_pid=12345,
        )

    def test_valid_once_response_round_trips(self):
        request = make_request()
        seen = {}

        def sender(payload, **kwargs):
            seen.update(payload)
            return {"request_id": payload["request_id"], "request_digest": payload["digest"], "choice": "once"}

        decision = worker.present_request(
            request, identity=self.identity(), socket_path="/tmp/socket",
            config=self.config(), timeout_seconds=7, sender=sender, validator=self.validator,
        )
        self.assertEqual(decision.choice, "once")
        self.assertEqual(seen["task_id"], "t_fixture")
        self.assertEqual(seen["run_id"], 17)
        self.assertEqual(seen["claim_lock"], "claim-secret")
        self.assertEqual(seen["worker_pid"], 12345)
        self.assertEqual(tuple(seen["allowed_choices"]), ("once", "deny"))
        self.assertEqual(seen["timeout_seconds"], 7)

    def test_mismatched_or_broad_response_denies(self):
        request = make_request()
        for response in [
            {"request_id": "stale", "request_digest": "filled-by-test", "choice": "once"},
            {"request_id": "filled-by-test", "request_digest": "stale", "choice": "once"},
            {"request_id": "filled-by-test", "request_digest": "filled-by-test", "choice": "session"},
            {},
        ]:
            with self.subTest(response=response):
                def sender(payload, **_kwargs):
                    updated = dict(response)
                    if updated.get("request_id") == "filled-by-test":
                        updated["request_id"] = payload["request_id"]
                    if updated.get("request_digest") == "filled-by-test":
                        updated["request_digest"] = payload["digest"]
                    return updated
                decision = worker.present_request(
                    request, identity=self.identity(), socket_path="/tmp/socket",
                    config=self.config(), sender=sender, validator=self.validator,
                )
                self.assertEqual(decision.choice, "deny")

    def test_missing_identity_config_or_wrong_surface_denies_without_sender(self):
        request = make_request()
        sender = mock.Mock()
        cases = [
            dict(identity=None, config=self.config()),
            dict(identity=self.identity(), config=None),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                decision = worker.present_request(request, socket_path="/tmp/socket", sender=sender, **kwargs)
                self.assertEqual(decision.choice, "deny")
                sender.assert_not_called()

        bad_surface = make_request(surface="cli")
        decision = worker.present_request(
            bad_surface, identity=self.identity(), socket_path="/tmp/socket",
            config=self.config(), sender=sender,
        )
        self.assertEqual(decision.choice, "deny")
        sender.assert_not_called()

    def test_final_revalidation_failure_denies_after_once_response(self):
        request = make_request()

        def sender(payload, **_kwargs):
            return {"request_id": payload["request_id"], "request_digest": payload["digest"], "choice": "once"}

        def stale(_config, _bridge_request, _now):
            raise RuntimeError("reclaimed")

        decision = worker.present_request(
            request, identity=self.identity(), socket_path="/tmp/socket",
            config=self.config(), sender=sender, validator=stale,
        )
        self.assertEqual(decision.choice, "deny")

    def test_oversize_display_description_denies_without_sender(self):
        request = make_request(description="x" * 201)
        sender = mock.Mock()
        decision = worker.present_request(
            request, identity=self.identity(), socket_path="/tmp/socket",
            config=self.config(), sender=sender, validator=self.validator,
        )
        self.assertEqual(decision.choice, "deny")
        sender.assert_not_called()

    def test_identity_is_captured_from_worker_environment(self):
        env = {
            "HERMES_KANBAN_TASK": "t_fixture",
            "HERMES_KANBAN_RUN_ID": "17",
            "HERMES_KANBAN_CLAIM_LOCK": "claim-secret",
            "HERMES_KANBAN_DB": "/tmp/kanban.db",
            "HERMES_PROFILE": "work-executor",
        }
        with mock.patch.object(os, "getpid", return_value=222):
            identity = worker.WorkerIdentity.from_env(env)
        self.assertIsNotNone(identity)
        self.assertEqual(identity.worker_pid, 222)
        self.assertIsNone(worker.WorkerIdentity.from_env({}))

    def test_plugin_registers_transport_with_captured_config(self):
        import importlib.util
        from pathlib import Path

        plugin_path = Path(__file__).resolve().parents[1] / "plugins/kanban-owner/__init__.py"
        spec = importlib.util.spec_from_file_location("kanban_owner_fixture", plugin_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Ctx:
            def __init__(self):
                self.present = None

            def get_config(self, key, default=None):
                raise AssertionError("plugin config must not be read for socket or timeout")

            def register_approval_transport(self, name, present):
                self.name = name
                self.present = present

        env = {
            "HERMES_KANBAN_TASK": "t_fixture",
            "HERMES_KANBAN_RUN_ID": "17",
            "HERMES_KANBAN_CLAIM_LOCK": "claim-secret",
            "HERMES_KANBAN_DB": "/tmp/kanban.db",
            "HERMES_PROFILE": "work-executor",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = BridgeConfig(
                db_path=str(Path(tmp) / "kanban.db"),
                socket_path=str(Path(tmp) / "approval.sock"),
                owner_id="1494011544214835201",
                notifier_profile="work-pm",
                worker_profile="work-executor",
            )
            with mock.patch.dict(os.environ, env, clear=True):
                ctx = Ctx()
                with mock.patch("bridge.runtime.require_compatible_runtime"):
                    with mock.patch.object(module, "load_config", return_value=config):
                        module.register(ctx)
        self.assertEqual(ctx.name, "kanban-owner")

        request = make_request(timeout_seconds=99)

        def fake_present(req, **kwargs):
            fake_present.kwargs = kwargs
            return req.respond("once")

        with mock.patch.object(module, "present_request", side_effect=fake_present):
            self.assertEqual(ctx.present(request).choice, "once")
        self.assertEqual(fake_present.kwargs["socket_path"], config.socket_path)
        self.assertEqual(fake_present.kwargs["config"], config)
        self.assertEqual(fake_present.kwargs["timeout_seconds"], config.max_timeout)

    def test_plugin_register_fails_when_private_config_is_missing(self):
        import importlib.util

        plugin_path = Path(__file__).resolve().parents[1] / "plugins/kanban-owner/__init__.py"
        spec = importlib.util.spec_from_file_location("kanban_owner_missing_config_fixture", plugin_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Ctx:
            def get_config(self, _key, default=None):
                return default

            def register_approval_transport(self, _name, _present):
                raise AssertionError("transport must not register without config")

        with mock.patch("bridge.runtime.require_compatible_runtime"):
            with mock.patch.object(module, "load_config", side_effect=FileNotFoundError):
                with self.assertRaises(FileNotFoundError):
                    module.register(Ctx())


if __name__ == "__main__":
    unittest.main()
