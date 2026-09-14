"""Worker-side transport for one-command Kanban owner approval."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import time
from typing import Callable, Mapping, Optional

from hermes_cli.approval_transport import ApprovalDecision, ApprovalRequest
from bridge.protocol import ApprovalBridgeRequest, decode_line, encode_line

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


def default_socket_path() -> str:
    base = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return str(base / "kanban-approval-bridge" / "worker.sock")


def build_bridge_request(
    request: ApprovalRequest, identity: WorkerIdentity, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
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
    )


def build_payload(request: ApprovalRequest, identity: WorkerIdentity) -> dict:
    return build_bridge_request(request, identity).to_dict()


def _socket_roundtrip(payload: dict, *, socket_path: str, timeout_seconds: float) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.001, float(timeout_seconds)))
        client.connect(socket_path)
        client.sendall(encode_line(payload))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    line = b"".join(chunks).split(b"\n", 1)[0]
    if not line:
        return {}
    value = decode_line(line + b"\n")
    return value if isinstance(value, dict) else {}


def send_payload(payload: dict, *, socket_path: str, timeout_seconds: float) -> dict:
    """Send one immutable request to the owner broker.

    A future broker module may provide a richer helper. This fallback keeps the
    wire contract explicit and small for tests and dry-run validation.
    """
    try:
        from bridge import protocol
        helper = getattr(protocol, "send_worker_request", None)
        if callable(helper):
            return helper(payload, socket_path=socket_path, timeout_seconds=timeout_seconds)
    except Exception:
        logger.debug("Bridge protocol helper unavailable; using direct socket transport", exc_info=True)
    return _socket_roundtrip(payload, socket_path=socket_path, timeout_seconds=timeout_seconds)


def decision_from_response(
    request: ApprovalRequest, response: Mapping[str, object], bridge_request: ApprovalBridgeRequest,
    *, config, validator: Callable[..., dict],
) -> ApprovalDecision:
    response_digest = response.get("request_digest", response.get("digest"))
    if (
        response.get("request_id") == bridge_request.request_id
        and response_digest == bridge_request.digest
        and response.get("choice") == "once"
        and time.time() < bridge_request.expires_at
    ):
        validator(config, bridge_request, time.time())
        return request.respond("once")
    return request.respond("deny")


def present_request(
    request: ApprovalRequest, *, identity: WorkerIdentity | None, socket_path: str,
    config=None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sender: Callable[..., dict] = send_payload,
    validator: Callable[..., dict] | None = None,
) -> ApprovalDecision:
    if (
        identity is None
        or config is None
        or request.surface != "kanban_worker"
        or tuple(request.allowed_choices) != ("once", "deny")
    ):
        return request.respond("deny")
    try:
        bridge_request = build_bridge_request(request, identity, timeout_seconds=timeout_seconds)
        payload = bridge_request.to_dict()
        response = sender(payload, socket_path=socket_path, timeout_seconds=timeout_seconds)
        if validator is None:
            from bridge.broker import validate_current_request
            validator = validate_current_request
        return decision_from_response(
            request, response if isinstance(response, Mapping) else {},
            bridge_request, config=config, validator=validator,
        )
    except Exception:
        logger.warning("Kanban owner approval request failed closed for %s", request.request_id)
        return request.respond("deny")
