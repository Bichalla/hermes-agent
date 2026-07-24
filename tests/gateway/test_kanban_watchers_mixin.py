"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanWatchersMixin)
    # Each kanban method resolves to the mixin's implementation via the MRO.
    for m in KANBAN_METHODS:
        owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
        assert owner is GatewayKanbanWatchersMixin, (
            f"{m} resolved to {owner.__name__}, expected the mixin"
        )


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


class _FactoryBackend:
    kind = "codex-direct/v1"

    def __init__(self):
        self.prepares = 0
        self.releases = 0
        self.observes = 0
        self.terminates = 0

    def prepare(self, spec, worktree):
        self.prepares += 1
        return object()

    def release(self, handle):
        self.releases += 1

    def observe(self, handle):
        self.observes += 1
        return None

    def terminate(self, handle):
        self.terminates += 1
        return None


class _ActiveRunConnection:
    def execute(self, _sql, _parameters=()):
        return self

    def fetchone(self):
        return (1,)


@pytest.mark.parametrize(
    "config",
    [
        {},
        None,
        [],
        {"kanban": None},
        {"kanban": []},
        {"kanban": {"codex_direct": None}},
        {"kanban": {"codex_direct": []}},
        {"kanban": {"codex_direct": {}}},
        {"kanban": {"codex_direct": {"enabled": None}}},
        {"kanban": {"codex_direct": {"enabled": "true"}}},
        {"kanban": {"codex_direct": {"enabled": 1}}},
        {"kanban": {"codex_direct": {"enabled": False}}},
    ],
)
def test_codex_direct_missing_malformed_and_false_construct_no_launch_backend(config):
    from gateway.kanban_watchers import _compose_codex_backend_registry

    factory_calls = []

    def factory():
        factory_calls.append("launch-capable")
        return _FactoryBackend()

    registry = _compose_codex_backend_registry(
        config,
        "lifelog-control",
        connection=None,
        backend_factory=factory,
        startup_recovery=False,
    )

    assert registry is None
    assert factory_calls == []


def test_codex_direct_true_is_ignored_for_non_pilot_board():
    from gateway.kanban_watchers import _compose_codex_backend_registry

    factory_calls = []
    registry = _compose_codex_backend_registry(
        {"kanban": {"codex_direct": {"enabled": True}}},
        "default",
        connection=None,
        backend_factory=lambda: factory_calls.append("called"),
        startup_recovery=True,
    )

    assert registry is None
    assert factory_calls == []


def test_codex_direct_true_on_pilot_constructs_exactly_one_launch_registry():
    from gateway.kanban_watchers import _compose_codex_backend_registry

    backend = _FactoryBackend()
    factory_calls = []

    def factory():
        factory_calls.append("called")
        return backend

    registry = _compose_codex_backend_registry(
        {"kanban": {"codex_direct": {"enabled": True}}},
        "lifelog-control",
        connection=None,
        backend_factory=factory,
        startup_recovery=False,
    )

    assert registry == {backend.kind: backend}
    assert factory_calls == ["called"]


def test_startup_recovery_registry_is_launch_incapable_with_config_off():
    from gateway.kanban_watchers import _compose_codex_backend_registry

    backend = _FactoryBackend()
    factory_calls = []

    def factory():
        factory_calls.append("provider-free-constructor")
        return backend

    registry = _compose_codex_backend_registry(
        {"kanban": {"codex_direct": {"enabled": False}}},
        "lifelog-control",
        connection=_ActiveRunConnection(),
        backend_factory=factory,
        startup_recovery=True,
    )

    recovery = registry[backend.kind]
    assert factory_calls == ["provider-free-constructor"]
    with pytest.raises(RuntimeError, match="recovery backend cannot launch"):
        recovery.prepare(None, None)
    with pytest.raises(RuntimeError, match="recovery backend cannot launch"):
        recovery.release(None)
    assert backend.prepares == backend.releases == 0


def test_startup_recovery_factory_failure_still_returns_fail_closed_registry():
    from gateway.kanban_watchers import _compose_codex_backend_registry

    registry = _compose_codex_backend_registry(
        {"kanban": {"codex_direct": {"enabled": False}}},
        "lifelog-control",
        connection=_ActiveRunConnection(),
        backend_factory=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
        startup_recovery=True,
    )

    assert set(registry) == {"codex-direct/v1"}
    recovery = registry["codex-direct/v1"]
    with pytest.raises(RuntimeError, match="recovery observation unavailable"):
        recovery.observe(object())
    with pytest.raises(RuntimeError, match="recovery termination unavailable"):
        recovery.terminate(object())


def test_recovery_only_backend_terminates_matching_live_session():
    from gateway.kanban_watchers import _RecoveryOnlyCodexBackend
    from hermes_cli.kanban_execution import (
        ExecutionObservation,
        ExecutionState,
        TerminationObservation,
        TerminationState,
    )

    class Backend(_FactoryBackend):
        def observe(self, handle):
            self.observes += 1
            return ExecutionObservation(ExecutionState.RUNNING, None)

        def terminate(self, handle):
            self.terminates += 1
            return TerminationObservation(TerminationState.DEAD)

    backend = Backend()
    recovery = _RecoveryOnlyCodexBackend(backend)
    observation = recovery.observe(object())

    assert observation.state is ExecutionState.EXITED
    assert backend.observes == backend.terminates == 1


def test_recovery_only_backend_never_signals_identity_mismatch():
    from gateway.kanban_watchers import _RecoveryOnlyCodexBackend
    from hermes_cli.kanban_execution import ExecutionObservation, ExecutionState

    class Backend(_FactoryBackend):
        def observe(self, handle):
            self.observes += 1
            return ExecutionObservation(ExecutionState.IDENTITY_MISMATCH, None)

        def terminate(self, handle):
            raise AssertionError("identity mismatch must not be signalled")

    backend = Backend()
    recovery = _RecoveryOnlyCodexBackend(backend)
    observation = recovery.observe(object())

    assert observation.state is ExecutionState.IDENTITY_MISMATCH
    assert backend.observes == 1
    assert backend.terminates == 0


def test_active_run_query_database_error_propagates_for_startup_retry():
    import sqlite3

    from gateway.kanban_watchers import _compose_codex_backend_registry

    class FailingConnection:
        def execute(self, _sql, _parameters=()):
            raise sqlite3.OperationalError("transient read failure")

    with pytest.raises(sqlite3.OperationalError, match="transient read failure"):
        _compose_codex_backend_registry(
            {"kanban": {"codex_direct": {"enabled": False}}},
            "lifelog-control",
            connection=FailingConnection(),
            backend_factory=_FactoryBackend,
            startup_recovery=True,
        )


def test_lock_skipped_dispatch_does_not_clear_startup_recovery_pending():
    source = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)

    assert (
        'if startup_recovery and not getattr(result, "skipped_locked", False):'
        in source
    )


def test_codex_direct_registry_requires_exact_managed_schema_readback():
    from gateway.kanban_watchers import _compose_codex_backend_registry

    calls = []
    registry = _compose_codex_backend_registry(
        {"kanban": {"codex_direct": {"enabled": True}}},
        "lifelog-control",
        connection=None,
        managed_schema_ready=False,
        backend_factory=lambda: calls.append("called"),
        startup_recovery=False,
    )

    assert registry is None
    assert calls == []


def test_gateway_dispatch_tick_passes_composed_registry_into_locked_core():
    source = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)

    assert "managed_execution_schema_status(conn)" in source
    assert "_compose_codex_backend_registry(" in source
    assert "managed_schema_ready=schema_status[\"migrated\"] is True" in source
    assert "backend_registry=backend_registry" in source
    assert "startup_recovery_pending_boards.discard(slug)" in source
