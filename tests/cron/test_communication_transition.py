"""Real-store claim fencing and drain safety for communication cutovers."""
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from cron import delivery_queue, executions, jobs, scheduler


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(delivery_queue, "DELIVERY_DB", None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_reap_dead_owners", lambda: None)
    monkeypatch.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
    with jobs.use_cron_store(tmp_path):
        yield tmp_path


def test_freeze_blocks_real_tick_and_force_claim_without_rewinding_schedule(store, monkeypatch):
    from cron import communication_transition as transition

    clock = [datetime(2026, 10, 16, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    job = jobs.create_job("brief", "every 10m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": clock[0].isoformat()})
    before = jobs.load_jobs()
    assert transition.freeze()["phase"] == "READY"
    clock[0] += timedelta(seconds=30)
    assert scheduler.tick(verbose=False) == 0
    assert jobs.claim_job_for_fire(job["id"], force=True, return_job=True) is False
    assert jobs.load_jobs() == before
    assert transition.resume()["generation"] == 1
    # Resume uses the existing scheduler's catch-up rules, not a restored snapshot.
    seen = []
    def run(claimed, **kwargs):
        seen.append(claimed)
        jobs.mark_job_run(claimed["id"], True)
        executions.finish_execution(claimed["execution_id"], success=True)
        return True
    monkeypatch.setattr(scheduler, "run_one_job", run)
    assert scheduler.tick(verbose=False) == 1
    assert len(seen) == 1
    assert seen[0]["_communication_transition"]["generation"] == 1
    assert jobs.load_jobs()[0]["repeat"]["completed"] == 1


@pytest.mark.parametrize("state", ["running", "pending", "delivering", "unknown"])
def test_current_generation_must_drain_and_queue_keeps_original_job_copy(store, state):
    from cron import communication_transition as transition
    from cron.scheduler_delivery import _resolve_delivery_targets

    job = jobs.create_job("brief", "every 10m", deliver="origin", origin={
        "platform": "discord", "chat_id": "old-private", "user_id": "owner"})
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    execution = executions.create_execution(job["id"], source="test")
    claimed["execution_id"] = execution["id"]
    transition.bind_execution(claimed)
    executions.mark_execution_running(execution["id"])
    if state != "running":
        delivery_queue.enqueue(execution["id"], claimed, "saved result")
        jobs.mark_job_run(job["id"], True)
        executions.finish_execution(execution["id"], success=True)
    if state in {"delivering", "unknown"}:
        delivery_queue.claim_next()
    if state == "unknown":
        delivery_queue._terminalize_wait_timeout(execution["id"])
    result = transition.freeze()
    assert result["phase"] == "HOLD"
    with pytest.raises(transition.TransitionHeld):
        transition.resume()
    jobs.update_job(job["id"], {"deliver": "discord:new-private"})
    assert _resolve_delivery_targets(claimed)[0]["chat_id"] == "old-private"
    assert claimed["origin"]["user_id"] == "owner"
    if state == "running":
        jobs.mark_job_run(job["id"], True)
        executions.finish_execution(execution["id"], success=True)
    elif state == "pending":
        sent = []
        delivery_queue.drain(lambda snapshot, content, failure: sent.append(snapshot))
        assert sent[0]["origin"] == job["origin"]
        assert _resolve_delivery_targets(sent[0])[0]["chat_id"] == "old-private"
        assert sent[0]["_communication_transition"]["generation"] == 0
    elif state == "delivering":
        delivery_queue._finish(execution["id"], error=None)
    if state != "unknown":
        assert transition.status()["phase"] == "READY"
        assert transition.resume()["generation"] == 1
    else:
        assert transition.wait_for_drain(timeout=0)["phase"] == "HOLD"
        assert delivery_queue.drain(lambda *args: pytest.fail("unknown replayed")) == 0


def test_freeze_does_not_wait_for_tick_or_deadlock_its_running_worker(store, monkeypatch):
    import contextvars
    from cron import communication_transition as transition

    job = jobs.create_job("brief", "every 10m", deliver="discord:old-private")
    jobs.update_job(job["id"], {"next_run_at": datetime.now(timezone.utc).isoformat()})
    started, finish, ended = threading.Event(), threading.Event(), threading.Event()
    def run(claimed, **kwargs):
        executions.mark_execution_running(claimed["execution_id"])
        started.set()
        assert finish.wait(5)
        jobs.mark_job_run(claimed["id"], True)
        executions.finish_execution(claimed["execution_id"], success=True)
        return True
    monkeypatch.setattr(scheduler, "run_one_job", run)
    context = contextvars.copy_context()
    def tick():
        try:
            assert scheduler.tick(verbose=False, sync=True) == 1
        finally:
            ended.set()
    thread = threading.Thread(target=lambda: context.run(tick))
    thread.start()
    try:
        assert started.wait(5)
        # tick still owns .tick.lock and waits for the worker; freeze must return HOLD now.
        assert transition.freeze()["phase"] == "HOLD"
        assert jobs.claim_job_for_fire(job["id"], force=True) is False
    finally:
        finish.set()
        assert ended.wait(5)
        thread.join()
    assert transition.status()["phase"] == "READY"
    transition.resume()


def test_external_worker_adopts_original_snapshot_during_freeze(store, monkeypatch):
    from cron import communication_transition as transition
    from cron.scheduler_delivery import _resolve_delivery_targets

    job = jobs.create_job("brief", "every 10m", deliver="discord:old-private")
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    execution = executions.create_execution(job["id"], source="external")
    claimed["execution_id"] = execution["id"]
    transition.bind_execution(claimed)
    executions.mark_execution_handoff_pending(execution["id"])
    payload = store / "cron/external-workers/payload.json"
    payload.parent.mkdir(parents=True)
    payload.write_text(json.dumps({"job": claimed, "profile_home": str(store)}))
    ack = payload.with_suffix(".ready")
    assert transition.freeze()["phase"] == "HOLD"
    jobs.update_job(job["id"], {"deliver": "discord:new-private"})
    def run(snapshot, **kwargs):
        assert _resolve_delivery_targets(snapshot)[0]["chat_id"] == "old-private"
        assert snapshot["_communication_transition"]["generation"] == 0
        assert executions.get_execution(snapshot["execution_id"])["status"] == "running"
        jobs.mark_job_run(snapshot["id"], True)
        executions.finish_execution(snapshot["execution_id"], success=True)
        return True
    monkeypatch.setattr(scheduler, "run_one_job", run)
    assert scheduler._run_external_worker_payload(payload, ack)
    assert json.loads(ack.read_text())["execution_id"] == execution["id"]
    assert transition.status()["phase"] == "READY"


@pytest.mark.parametrize("deliver,provenance,origin_user,mirror", [
    ("origin", "origin", "owner", True),
    ("discord:new-private", "explicit", None, False),
])
def test_destination_pinning_preserves_origin_and_mirror_eligibility(
        store, deliver, provenance, origin_user, mirror):
    from cron import communication_transition as transition
    from cron.scheduler_delivery import _prepare_target_delivery, _resolve_delivery_targets
    from gateway.config import GatewayConfig, Platform, PlatformConfig

    origin = {"platform": "discord", "chat_id": "old-private", "user_id": "owner"}
    job = jobs.create_job("brief", "every 10m", deliver=deliver, origin=origin)
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    target = _resolve_delivery_targets(claimed)[0]
    assert target["_resolved_from"] == provenance
    prepared = _prepare_target_delivery(claimed, target, adapters=None, loop=None,
        config=GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")}),
        notify_delivery=False, mirror_enabled=True,
        mirror_text="brief", delivery_errors=[])
    assert prepared.origin_user_id == origin_user
    assert prepared.mirror_this_target is mirror
    assert claimed["origin"] == origin
    assert transition.freeze()["runs"][0]["origin"] == origin


def test_cli_control_is_seen_by_independent_claim_process_and_preserves_store(store):
    import os
    import subprocess
    import sys

    job = jobs.create_job("brief", "every 10m", deliver="local")
    before = jobs.load_jobs()
    def command(*args):
        return subprocess.run([sys.executable, *args], cwd=str(scheduler.Path(__file__).resolve().parents[2]),
            env=dict(os.environ, HERMES_HOME=str(store)), text=True, capture_output=True, timeout=15)
    frozen = command("-m", "cron.communication_transition", "freeze")
    assert frozen.returncode == 0, frozen.stderr
    assert json.loads(frozen.stdout)["phase"] == "READY"
    claimed = command("-c", "from cron.jobs import claim_job_for_fire; "
                      f"assert claim_job_for_fire({job['id']!r}, force=True) is False")
    assert claimed.returncode == 0, claimed.stderr
    assert jobs.load_jobs() == before
    resumed = command("-m", "cron.communication_transition", "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["generation"] == 1


def test_freeze_blocks_provider_audit_claim_before_store_or_execution_mutation(store):
    from cron import communication_transition as transition
    from cron.scheduler_provider import InProcessCronScheduler

    job = jobs.create_job("brief", "every 10m", deliver="local")
    before = jobs.load_jobs()
    transition.freeze()
    assert InProcessCronScheduler().claim_fire(job["id"], force=True) is None
    assert jobs.load_jobs() == before
    assert executions.list_executions() == []


@pytest.mark.parametrize("deliver,outcome,started,historical,blocked", [
    ("discord:old-private", "failed", True, False, True),
    ("origin", "not_configured", True, False, True),
    ("discord:old-private", "unknown", True, False, True),
    ("local", "not_configured", True, False, False),
    ("discord:old-private", "suppressed", True, False, False),
    ("discord:old-private", "failed", False, False, False),
    ("discord:old-private", "failed", True, True, False),
])
def test_cutover_holds_unconfirmed_terminal_delivery_only_for_admitted_executed_runs(
        store, deliver, outcome, started, historical, blocked):
    from cron import communication_transition as transition
    from cron.scheduler_provider import InProcessCronScheduler

    job = jobs.create_job("brief", "every 10m", deliver=deliver)
    if historical:
        execution = executions.create_execution(job["id"], source="historical")
        claimed = dict(job, execution_id=execution["id"])
    else:
        claimed = InProcessCronScheduler().claim_fire(job["id"])
    run_id = claimed["execution_id"]
    if started:
        executions.mark_execution_running(run_id)
    if outcome == "failed" and started and not historical:
        delivery = scheduler._RunDelivery(job=claimed, success=True, error=None,
            should_deliver=True, delivery_attempted=True, delivery_error="transport rejected")
        assert scheduler._finish_completed_run(delivery, claimed["fire_claim"]["by"], run_id)
        assert jobs.get_job(job["id"])["last_status"] == "delivery_failed"
    else:
        jobs.mark_job_run(job["id"], True)
        executions.finish_execution(run_id, success=True, delivery_outcome=outcome)
    assert executions.get_execution(run_id)["status"] == "completed"
    assert executions.get_execution(run_id)["delivery_outcome"] == outcome
    assert jobs.get_job(job["id"])["fire_claim"] is None
    if not historical:
        transition.settle_execution(claimed)
    result = transition.freeze()
    assert result["phase"] == ("HOLD" if blocked else "READY")
    if blocked:
        assert any(row["kind"] == "delivery_outcome" and row["run_id"] == run_id
                   for row in result["blockers"])
        with pytest.raises(transition.TransitionHeld):
            transition.resume()
        # Bounded execution-ledger retention must not erase a settled delivery failure.
        with executions._transaction() as connection:
            connection.execute("DELETE FROM executions WHERE id=?", (run_id,))
        assert transition.status()["phase"] == "HOLD"
    else:
        assert transition.resume()["generation"] == 1


@pytest.mark.parametrize("schedule_kind", ["interval", "once"])
@pytest.mark.parametrize("change", [
    {"prompt": "new business definition"},
    {"origin": {"platform": "discord", "chat_id": "new-private", "user_id": "new-owner"}},
])
def test_pool_definition_drift_rejects_before_claim_and_next_scheduled_fire_is_consistent(
        store, monkeypatch, change, schedule_kind):
    import concurrent.futures
    from cron import communication_transition as transition

    class QueuedPool:
        def submit(self, call):
            self.call = call
            self.future = concurrent.futures.Future()
            return self.future
    pool = QueuedPool()
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda workers: pool)
    monkeypatch.setattr(scheduler, "run_one_job", lambda *args, **kwargs: pytest.fail(
        "mixed submission and claim definitions reached business execution"))
    schedule = ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
                if schedule_kind == "once" else "every 10m")
    job = jobs.create_job("old business definition", schedule, deliver="origin",
        origin={"platform": "discord", "chat_id": "old-private", "user_id": "old-owner"})
    jobs.update_job(job["id"], {"next_run_at": datetime.now(timezone.utc).isoformat()})
    assert scheduler.tick(verbose=False, sync=False) == 1
    jobs.update_job(job["id"], change)
    before_claim = jobs.load_jobs()
    if schedule_kind == "once":
        assert transition.freeze()["phase"] == "HOLD"
    pool.future.set_result(pool.call())
    assert jobs.load_jobs() == before_claim
    assert jobs.get_job(job["id"]).get("fire_claim") is None
    assert jobs.get_job(job["id"])["repeat"]["completed"] == 0
    rejected = executions.list_executions()[0]
    assert rejected["status"] == "failed"
    assert rejected["started_at"] is None
    assert rejected["delivery_outcome"] == "suppressed"
    result = transition.freeze()
    assert result["phase"] == "READY"
    assert result["runs"][0]["disposition"] == "definition_changed"
    assert transition.resume()["generation"] == 1
    if schedule_kind == "once":
        # Preserve the original run lease; normal due scanning still owns its expiry policy.
        assert jobs.load_jobs() == before_claim
        return
    # A later scheduled occurrence consumes one run using one consistent new definition.
    next_due = datetime.fromisoformat(before_claim[0]["next_run_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: next_due)
    seen = []
    def run_consistent(snapshot, **kwargs):
        seen.append(snapshot)
        metadata = snapshot["_communication_transition"]
        assert metadata["definition_hash"] == transition._definition_hash(snapshot)
        assert metadata["origin"] == snapshot["origin"]
        jobs.mark_job_run(snapshot["id"], True)
        executions.finish_execution(snapshot["execution_id"], success=True)
        return True
    monkeypatch.setattr(scheduler, "run_one_job", run_consistent)
    assert scheduler.tick(verbose=False, sync=False) == 1
    pool.future.set_result(pool.call())
    assert len(seen) == 1
    for field, value in change.items():
        assert seen[0][field] == value
    assert jobs.get_job(job["id"])["repeat"]["completed"] == 1
