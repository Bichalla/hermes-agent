"""Profile-local claim fencing and drain evidence for delivery-route transitions.

The control file never restores job snapshots or retries work. Freeze fences new
claims while admitted executions and the gateway delivery queue finish normally.
A READY result is permission to change delivery references, not proof of transport
receipt. Unknown outcomes always hold the transition for external reconciliation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from contextlib import contextmanager

from hermes_time import now

_lock = threading.RLock()
_local = threading.local()
_RUNTIME_FIELDS = frozenset({
    "next_run_at", "last_run_at", "last_status", "last_error", "last_delivery_error",
    "last_delivery_outcome", "failure_streak", "fire_claim", "run_claim",
    "execution_id", "_scheduled_instant", "_communication_transition", "state",
    "monitor_state", "manual_run_at", "manual_run_prompt",
})


class TransitionHeld(RuntimeError):
    """The frozen generation still has unresolved work; leave routes unchanged."""


class DefinitionDrift(TransitionHeld):
    """The queued definition changed before its fire claim; no work may start."""

    def __init__(self, observed_hash):
        self.observed_hash = observed_hash
        super().__init__("Queued cron definition changed before its fire claim")


def _directory() -> Path:
    from cron.jobs import _current_cron_store
    return _current_cron_store().cron_dir


@contextmanager
def _control():
    """Strict lock order: transition -> jobs/executions/queue; never wait for work here."""
    directory = _directory()
    key = str(directory.resolve())
    with _lock:
        nested = getattr(_local, "control", None)
        if nested is not None:
            if nested[0] != key:
                raise RuntimeError("Cannot switch cron profile inside a claim transaction")
            yield nested[1]
            return
        from cron.jobs import _acquire_flock, _ensure_cron_dir, _release_flock
        _ensure_cron_dir(directory)
        directory.chmod(0o700)
        lock_path = directory / ".communication-transition.lock"
        fd = lock_path.open("a+", encoding="utf-8")
        lock_path.chmod(0o600)
        if _acquire_flock(fd, 5.0) is not True:
            fd.close()
            raise TransitionHeld("Communication claim fence unavailable; no new claims allowed")
        path = directory / "communication-transition.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "paused": False, "generation": 0, "runs": {}}
            if (not isinstance(data, dict) or not isinstance(data.get("paused"), bool)
                    or not isinstance(data.get("generation"), int)
                    or not isinstance(data.get("runs"), dict)):
                raise TransitionHeld("Invalid communication control; no new claims allowed")
            before = json.dumps(data, sort_keys=True)
            _local.control = (key, data)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                temporary = path.with_suffix(".json.tmp")
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(out, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                if os.name == "posix":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        finally:
            _local.control = None
            _release_flock(fd)


@contextmanager
def claim_gate(admission_id=None):
    """Fence a new claim; admitted runs may finish, including external handoff."""
    with _control() as control:
        previous = getattr(_local, "continuation", False)
        previous_id = getattr(_local, "admission_id", None)
        admitted = bool(admission_id and str(admission_id) in control["runs"])
        allowed = not control["paused"] or previous or admitted
        _local.continuation = previous or admitted
        _local.admission_id = str(admission_id) if admitted else previous_id
        try:
            yield allowed
        finally:
            _local.continuation = previous
            _local.admission_id = previous_id


def _definition_hash(job):
    from cron.jobs import _apply_skill_fields
    definition = {key: value for key, value in _apply_skill_fields(job).items()
                  if key not in _RUNTIME_FIELDS and not key.startswith("last_")}
    if isinstance(definition.get("repeat"), dict):
        definition["repeat"] = {key: value for key, value in definition["repeat"].items()
                                if key != "completed"}
    return hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False,
                                    default=str).encode()).hexdigest()


def validate_definition(job, expected_hash):
    observed = _definition_hash(job)
    if expected_hash is not None and observed != expected_hash:
        raise DefinitionDrift(observed)


def record_definition_changed(job, observed_hash):
    with _control() as control:
        token = job["_communication_transition"]["admission_id"]
        control["runs"][token]["disposition"] = "definition_changed"
        control["runs"][token]["observed_definition_hash"] = observed_hash



def stamp_claim(job, *, admission_id=None):
    """Pin destinations and their provenance without rewriting execution origin."""
    from cron.scheduler_delivery import _resolve_delivery_targets
    snapshot = copy.deepcopy(job)
    with _control() as control:
        metadata = snapshot.get("_communication_transition")
        if metadata and metadata.get("admission_id") in control["runs"]:
            return snapshot
        token = str(admission_id or getattr(_local, "admission_id", None)
                    or (job.get("fire_claim") or {}).get("by") or "")
        if token in control["runs"]:
            snapshot["_communication_transition"] = copy.deepcopy(control["runs"][token])
            return snapshot
        if not token:
            raise TransitionHeld("An admitted run needs an execution or fire-claim identity")
        if control["paused"] and not getattr(_local, "continuation", False):
            raise TransitionHeld("New cron claims are frozen")
        metadata = {
            "admission_id": token,
            "job_id": str(job["id"]),
            "profile_home": str(_directory().parent.resolve()),
            "definition_hash": _definition_hash(job),
            "generation": control["generation"],
            "origin": copy.deepcopy(job.get("origin")),
            "run_claim": copy.deepcopy(job.get("run_claim")),
            "deliver": job.get("deliver", "local"),
            "failure_deliver": job.get("failure_deliver"),
            "targets": _resolve_delivery_targets(job),
            "failure_targets": _resolve_delivery_targets(job, for_failure=True),
            "admitted_at": now().isoformat(),
            "execution_id": job.get("execution_id"),
        }
        control["runs"][token] = metadata
        snapshot["_communication_transition"] = copy.deepcopy(metadata)
    return snapshot


def bind_execution(job):
    """Join a claimed snapshot to its durable execution before worker dispatch."""
    with _control() as control:
        metadata = job.get("_communication_transition") or {}
        token = metadata.get("admission_id")
        if token not in control["runs"]:
            raise TransitionHeld("Execution is missing its admitted delivery snapshot")
        control["runs"][token]["execution_id"] = str(job["execution_id"])
        metadata["execution_id"] = str(job["execution_id"])


def settle_execution(job):
    """Keep the terminal outcome after the bounded execution ledger prunes old rows."""
    from cron.executions import get_execution
    execution = get_execution(str(job["execution_id"]))
    if execution and execution["status"] in {"completed", "failed", "unknown"}:
        with _control() as control:
            token = (job.get("_communication_transition") or {}).get("admission_id")
            if token in control["runs"]:
                control["runs"][token]["terminal_status"] = execution["status"]
                control["runs"][token]["started_at"] = execution.get("started_at")
                control["runs"][token]["delivery_outcome"] = execution.get("delivery_outcome")


def _read_rows(path, query):
    """Inventory never initializes/migrates a ledger and does not paginate away blockers."""
    import sqlite3
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query)]


def _status(control):
    from cron import delivery_queue, executions, jobs
    from cron.scheduler_delivery import _normalize_deliver_value
    home = _directory().parent.resolve()
    execution_rows = _read_rows(executions.EXECUTIONS_FILE or home / "cron/executions.db",
                               "SELECT * FROM executions")
    delivery_rows = _read_rows(delivery_queue._path(), "SELECT * FROM deliveries")
    tombstones = _read_rows(delivery_queue._path(),
                           "SELECT * FROM delivery_tombstones WHERE terminal_status='unknown'")
    by_execution = {row["id"]: row for row in execution_rows}
    blockers = []
    for row in execution_rows:
        if row["status"] in {"claimed", "running", "unknown"}:
            blockers.append({"kind": "execution", "run_id": row["id"],
                             "job_id": row["job_id"], "status": row["status"]})
    deliveries = []
    for row in delivery_rows:
        snapshot = json.loads(row.get("job_json") or "{}")
        metadata = snapshot.get("_communication_transition") or {}
        deliveries.append({"run_id": row["execution_id"], "status": row["status"],
                           "job_id": snapshot.get("id"), "profile_home": str(home),
                           "generation": metadata.get("generation", "untracked"),
                           "definition_hash": metadata.get("definition_hash"),
                           "targets": metadata.get("targets"),
                           "failure_targets": metadata.get("failure_targets"),
                           "origin": snapshot.get("origin"),
                           "deliver": snapshot.get("deliver"),
                           "destination_pin_known": bool(metadata)})
        if row["status"] in {"pending", "delivering", "unknown", "failed"}:
            blockers.append({"kind": "delivery", "run_id": row["execution_id"],
                             "status": row["status"]})
    blockers.extend({"kind": "delivery_tombstone", "run_id": row["execution_id"],
                     "status": "unknown"} for row in tombstones)
    runs = []
    for metadata in control["runs"].values():
        run = copy.deepcopy(metadata)
        execution = by_execution.get(metadata.get("execution_id"))
        run["status"] = execution["status"] if execution else metadata.get("terminal_status", "unbound")
        if execution is None and run["status"] not in {"completed", "failed"}:
            blockers.append({"kind": "admission", "run_id": metadata["admission_id"],
                             "job_id": metadata["job_id"], "status": "unbound"})
        outcome = execution.get("delivery_outcome") if execution else metadata.get("delivery_outcome")
        started = execution.get("started_at") if execution else metadata.get("started_at")
        run["delivery_outcome"] = outcome
        current_cohort = metadata["generation"] == control["generation"]
        lane = metadata.get("deliver", "local")
        if run["status"] == "failed" and metadata.get("failure_deliver"):
            lane = metadata["failure_deliver"]
        if (current_cohort and started and _normalize_deliver_value(lane) != "local"
                and outcome in {"failed", "not_configured", "unknown"}):
            blockers.append({"kind": "delivery_outcome", "run_id": metadata.get("execution_id"),
                             "job_id": metadata["job_id"], "status": outcome})
        runs.append(run)
    definitions = [{"job_id": str(job["id"]), "profile_home": str(home),
                    "definition_hash": _definition_hash(job), "enabled": job.get("enabled"),
                    "deliver": job.get("deliver", "local"),
                    "failure_deliver": job.get("failure_deliver"),
                    "origin": job.get("origin"), "fire_claim": job.get("fire_claim"),
                    "run_claim": job.get("run_claim"), "next_run_at": job.get("next_run_at"),
                    "last_delivery_unverified": job.get("last_delivery_unverified")}
                   for job in jobs.load_jobs()]
    # Pre-upgrade fire/run claims have no new admission record. They still fence a cutover.
    for definition in definitions:
        if definition["last_delivery_unverified"]:
            blockers.append({"kind": "unverified_receipt", "job_id": definition["job_id"],
                             "status": "unknown"})
        claim = definition["fire_claim"]
        token = claim.get("by") if isinstance(claim, dict) else claim
        active = any(row["job_id"] == definition["job_id"]
                     and row["status"] in {"claimed", "running"} for row in execution_rows)
        rejected_run_claim = bool(definition["run_claim"]) and any(
            run["job_id"] == definition["job_id"]
            and run.get("disposition") == "definition_changed"
            and run.get("terminal_status") == "failed" and not run.get("started_at")
            and run.get("run_claim") == definition["run_claim"]
            for run in control["runs"].values())
        if (token or (definition["run_claim"] and not rejected_run_claim)) and not active:
            blockers.append({"kind": "store_claim", "job_id": definition["job_id"],
                             "status": "unresolved"})
    return {"paused": control["paused"], "generation": control["generation"],
            "phase": ("HOLD" if blockers else "READY") if control["paused"] else "ACTIVE",
            "blockers": blockers, "runs": runs, "jobs": definitions,
            "deliveries": deliveries}


def status():
    with _control() as control:
        return _status(control)


def freeze():
    """Stop new claims atomically; leave running jobs and delivery drain untouched."""
    with _control() as control:
        control["paused"] = True
        control.setdefault("frozen_at", now().isoformat())
    return status()


def wait_for_drain(*, timeout=30.0, poll_interval=0.25):
    """Bounded observation only. The live gateway performs normal authorized queue sends."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        result = status()
        if result["phase"] != "HOLD" or time.monotonic() >= deadline:
            return result
        threading.Event().wait(min(poll_interval, max(0.0, deadline - time.monotonic())))


def resume():
    """Activate the next generation only after all old work has a known terminal outcome."""
    with _control() as control:
        result = _status(control)
        if result["blockers"]:
            raise TransitionHeld("HOLD: current executions/deliveries/claims remain unresolved")
        if control["paused"]:
            control["generation"] += 1
            control["paused"] = False
            control.pop("frozen_at", None)
        return _status(control)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "status", "resume"))
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="freeze drain observation budget in seconds; sends no messages")
    args = parser.parse_args()
    try:
        result = {"freeze": freeze, "status": status, "resume": resume}[args.action]()
        if args.action == "freeze" and args.timeout > 0:
            result = wait_for_drain(timeout=args.timeout)
    except (TransitionHeld, OSError, ValueError) as error:
        print(json.dumps({"phase": "HOLD", "error": str(error)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result["phase"] == "HOLD" else 0


if __name__ == "__main__":
    raise SystemExit(main())
