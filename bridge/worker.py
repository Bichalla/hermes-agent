"""Worker-side transport for one-command Kanban owner approval."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import socket
import time
from typing import Callable, Mapping, Optional

from hermes_cli.approval_transport import ApprovalDecision, ApprovalRequest
from bridge.protocol import ApprovalBridgeRequest, ProtocolError, MAX_LINE_BYTES, decode_line, encode_line

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300.0
REQUIRED_ENV = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_DB",
    "HERMES_PROFILE",
)


@dataclass(frozen=True)
class WorkerIdentity:
    task_id: str
    run_id: str
    claim_lock: str
    db_path: str
    profile: str
    worker_pid: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Optional["WorkerIdentity"]:
        src = env or os.environ
        values = {name: str(src.get(name, "")).strip() for name in REQUIRED_ENV}
        if not all(values.values()):
            return None
        return cls(
            task_id=values["HERMES_KANBAN_TASK"],
            run_id=values["HERMES_KANBAN_RUN_ID"],
            claim_lock=values["HERMES_KANBAN_CLAIM_LOCK"],
            db_path=values["HERMES_KANBAN_DB"],
            profile=values["HERMES_PROFILE"],
            worker_pid=os.getpid(),
        )


def build_bridge_request(
    request: ApprovalRequest, identity: WorkerIdentity, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    execution: dict | None = None,
) -> ApprovalBridgeRequest:
    return ApprovalBridgeRequest.create(
        command=request.command,
        description=request.description,
        pattern_key=request.pattern_key,
        pattern_keys=request.pattern_keys,
        session_key=request.digest,
        task_id=identity.task_id,
        run_id=int(identity.run_id),
        claim_lock=identity.claim_lock,
        worker_pid=identity.worker_pid,
        db_path=os.path.realpath(identity.db_path),
        profile=identity.profile,
        timeout_seconds=int(min(float(request.timeout_seconds), float(timeout_seconds), DEFAULT_TIMEOUT_SECONDS)),
        execution=execution,
    )


def build_payload(request: ApprovalRequest, identity: WorkerIdentity) -> dict:
    return build_bridge_request(request, identity).to_dict()


def _socket_roundtrip(payload: dict, *, socket_path: str, timeout_seconds: float) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.001, float(timeout_seconds)))
        client.connect(socket_path)
        client.sendall(encode_line(payload))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LINE_BYTES:
                raise ProtocolError('response too large')
            if b"\n" in chunk:
                break
    line = b"".join(chunks).split(b"\n", 1)[0]
    if not line:
        return {}
    value = decode_line(line + b"\n")
    return value if isinstance(value, dict) else {}


def send_payload(payload: dict, *, socket_path: str, timeout_seconds: float) -> dict:
    """Send one immutable request to the configured owner broker."""
    return _socket_roundtrip(payload, socket_path=socket_path, timeout_seconds=timeout_seconds)


def decision_from_response(
    request: ApprovalRequest, response: Mapping[str, object], bridge_request: ApprovalBridgeRequest,
    *, config, validator: Callable[..., dict], initial_route: dict,
) -> ApprovalDecision:
    response_digest = response.get("request_digest", response.get("digest"))
    if (
        response.get("request_id") == bridge_request.request_id
        and response_digest == bridge_request.digest
        and response.get("choice") == "once"
        and time.time() < bridge_request.expires_at
    ):
        from bridge.evidence import verify_snapshot
        if not verify_snapshot(response.get('evidence', [])):
            return request.respond('deny')
        if validator(config, bridge_request, time.time()) == initial_route:
            return request.respond("once")
    return request.respond("deny")


def present_request(
    request: ApprovalRequest, *, identity: WorkerIdentity | None, socket_path: str,
    config=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sender: Callable[..., dict] = send_payload,
    validator: Callable[..., dict] | None = None,
    bindings=None,
) -> ApprovalDecision:
    if (
        identity is None
        or config is None
        or request.surface != "kanban_worker"
        or tuple(request.allowed_choices) != ("once", "deny")
    ):
        return request.respond("deny")
    try:
        execution = bindings.context(request) if bindings is not None else {}
        bridge_request = build_bridge_request(request, identity, timeout_seconds=timeout_seconds, execution=execution)
        payload = bridge_request.to_dict()
        if validator is None:
            from bridge.broker import validate_current_request
            validator = validate_current_request
        initial_route = dict(validator(config, bridge_request, time.time()))
        response = sender(payload, socket_path=socket_path, timeout_seconds=timeout_seconds)
        if (isinstance(response, Mapping) and response.get("request_id") == bridge_request.request_id
                and response.get("request_digest") == bridge_request.digest):
            reason = response.get("reason", "")
            if isinstance(reason, str) and reason and len(reason) <= 80 and all(c.islower() or c == "_" for c in reason):
                if bindings is not None:
                    bindings.decision(request, reason, response.get('details', ''))
                logger.info("Kanban approval task=%s run=%s request=%s reason=%s",
                            identity.task_id, identity.run_id, bridge_request.request_id, reason)
        decision = decision_from_response(
            request, response if isinstance(response, Mapping) else {},
            bridge_request, config=config, validator=validator, initial_route=initial_route,
        )
        if bindings is not None and decision.choice == 'deny' and isinstance(response, Mapping) and response.get('choice') == 'once':
            from bridge.evidence import verify_snapshot
            reason = 'authorization_changed' if verify_snapshot(response.get('evidence', [])) else 'source_changed'
            bindings.decision(request, reason)
        return decision
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
        if bindings is not None:
            bindings.decision(request, 'transport_invalid', reason)
        logger.warning("Kanban owner approval request failed closed for %s reason=%s", request.request_id, reason)
        return request.respond("deny")
