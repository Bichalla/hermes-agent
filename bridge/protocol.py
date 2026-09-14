"""Wire protocol for the Hermes Kanban approval bridge.

The bridge never sends a raw shell command.  The worker sends the redacted
display text plus a digest that is bound to the exact request fields.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import time
import uuid


MAX_LINE_BYTES = 16 * 1024
MAX_COMMAND_DISPLAY = 1200
MAX_DESCRIPTION = 200
ALLOWED_CHOICES = ("once", "deny")


class ProtocolError(ValueError):
    """Invalid bridge protocol data."""


@dataclass(frozen=True)
class ApprovalBridgeRequest:
    request_id: str
    command: str
    description: str
    pattern_key: str
    pattern_keys: tuple[str, ...]
    session_key: str
    task_id: str
    run_id: int
    claim_lock: str
    worker_pid: int
    db_path: str
    profile: str
    timeout_seconds: int
    expires_at: float
    allowed_choices: tuple[str, ...] = ALLOWED_CHOICES
    digest: str = ""

    @classmethod
    def create(
        cls,
        *,
        command: str,
        description: str,
        pattern_key: str,
        pattern_keys: tuple[str, ...] | list[str],
        session_key: str,
        task_id: str,
        run_id: int,
        claim_lock: str,
        worker_pid: int,
        db_path: str,
        profile: str,
        timeout_seconds: int,
        now: float | None = None,
    ) -> "ApprovalBridgeRequest":
        now = time.time() if now is None else now
        timeout = _bounded_timeout(timeout_seconds)
        req = cls(
            request_id=uuid.uuid4().hex,
            command=command,
            description=description,
            pattern_key=pattern_key,
            pattern_keys=tuple(pattern_keys),
            session_key=session_key,
            task_id=task_id,
            run_id=_strict_int(run_id, "run_id"),
            claim_lock=claim_lock,
            worker_pid=_strict_int(worker_pid, "worker_pid"),
            db_path=db_path,
            profile=profile,
            timeout_seconds=timeout,
            expires_at=now + timeout,
        )
        return cls.from_dict(req.with_digest().to_dict())

    @classmethod
    def from_dict(cls, payload: dict) -> "ApprovalBridgeRequest":
        if not isinstance(payload, dict):
            raise ProtocolError("request must be an object")
        try:
            req = cls(
                request_id=_text(payload["request_id"], "request_id", 80),
                command=_text(payload["command"], "command", MAX_COMMAND_DISPLAY),
                description=_text(payload["description"], "description", MAX_DESCRIPTION),
                pattern_key=_text(payload["pattern_key"], "pattern_key", 200),
                pattern_keys=tuple(_texts(payload["pattern_keys"], "pattern_keys", 200)),
                session_key=_text(payload["session_key"], "session_key", 240),
                task_id=_text(payload["task_id"], "task_id", 120),
                run_id=_strict_int(payload["run_id"], "run_id"),
                claim_lock=_text(payload["claim_lock"], "claim_lock", 240),
                worker_pid=_strict_int(payload["worker_pid"], "worker_pid"),
                db_path=_text(payload["db_path"], "db_path", 4096),
                profile=_text(payload["profile"], "profile", 120),
                timeout_seconds=_bounded_timeout(payload["timeout_seconds"]),
                expires_at=float(payload["expires_at"]),
                allowed_choices=tuple(_texts(payload.get("allowed_choices", ALLOWED_CHOICES), "allowed_choices", 16)),
                digest=_text(payload["digest"], "digest", 128),
            )
        except KeyError as exc:
            raise ProtocolError(f"missing field: {exc.args[0]}") from None
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid request field: {exc}") from None
        req.validate()
        extra = set(payload) - {
            "request_id", "command", "description", "pattern_key", "pattern_keys", "session_key",
            "task_id", "run_id", "claim_lock", "worker_pid", "db_path", "profile",
            "timeout_seconds", "expires_at", "allowed_choices", "digest",
        }
        if extra:
            raise ProtocolError("unknown field")
        return req

    def to_dict(self) -> dict:
        data = asdict(self)
        data["pattern_keys"] = list(self.pattern_keys)
        data["allowed_choices"] = list(self.allowed_choices)
        return data

    def with_digest(self) -> "ApprovalBridgeRequest":
        data = self.to_dict()
        data["digest"] = ""
        digest = _digest(data)
        return ApprovalBridgeRequest(**{**data, "pattern_keys": tuple(data["pattern_keys"]),
                                        "allowed_choices": tuple(data["allowed_choices"]),
                                        "digest": digest})

    def validate(self) -> None:
        if tuple(self.allowed_choices) != ALLOWED_CHOICES:
            raise ProtocolError("unsupported choices")
        if self.timeout_seconds <= 0 or self.expires_at <= 0 or not math.isfinite(self.expires_at):
            raise ProtocolError("invalid timeout")
        if self.run_id <= 0 or self.worker_pid <= 0:
            raise ProtocolError("invalid identity")
        if not self.pattern_keys:
            raise ProtocolError("pattern_keys required")
        for field in ("request_id", "command", "description", "pattern_key", "session_key",
                      "task_id", "claim_lock", "db_path", "profile"):
            if not getattr(self, field):
                raise ProtocolError(f"{field} required")
        if "```" in self.command or "```" in self.description:
            raise ProtocolError("unsafe display text")
        expected = self.with_digest().digest
        if not hmac_compare(expected, self.digest):
            raise ProtocolError("digest mismatch")


@dataclass(frozen=True)
class ApprovalBridgeDecision:
    request_id: str
    request_digest: str
    choice: str
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict, request: ApprovalBridgeRequest) -> "ApprovalBridgeDecision":
        if not isinstance(payload, dict):
            raise ProtocolError("decision must be an object")
        decision = cls(
            request_id=_text(payload.get("request_id"), "request_id", 80),
            request_digest=_text(payload.get("request_digest"), "request_digest", 128),
            choice=_text(payload.get("choice"), "choice", 16),
            reason=_text(payload.get("reason", ""), "reason", 200),
        )
        if decision.request_id != request.request_id or not hmac_compare(decision.request_digest, request.digest):
            raise ProtocolError("decision correlation mismatch")
        if decision.choice not in ALLOWED_CHOICES:
            raise ProtocolError("unsupported decision")
        return decision


def encode_line(payload: dict) -> bytes:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("message too large")
    return data


def decode_line(data: bytes) -> dict:
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("message too large")
    if not data.endswith(b"\n"):
        raise ProtocolError("message must end with newline")
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid json") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("message must be an object")
    return payload


def request_payload_for_native(request: ApprovalBridgeRequest) -> dict:
    return {
        "request_id": request.request_id,
        "digest": request.digest,
        "command": request.command,
        "description": request.description,
        "pattern_key": request.pattern_key,
        "pattern_keys": list(request.pattern_keys),
        "session_key": request.session_key,
        "task_id": request.task_id,
        "run_id": request.run_id,
        "timeout_seconds": request.timeout_seconds,
        "allowed_choices": list(request.allowed_choices),
    }


def hmac_compare(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hashlib.sha256(left.encode()).digest() == hashlib.sha256(right.encode()).digest()


def _digest(payload: dict) -> str:
    wire = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _bounded_timeout(value: int) -> int:
    value = _strict_int(value, "timeout_seconds")
    if value <= 0:
        raise ProtocolError("timeout must be positive")
    return min(int(value), 300)


def _strict_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    return value


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be text")
    if not value and name not in {"reason"}:
        raise ProtocolError(f"{name} required")
    if len(value) > limit:
        raise ProtocolError(f"{name} too long")
    return value


def _texts(value, name: str, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ProtocolError(f"{name} must be a list")
    return [_text(item, name, limit) for item in value]


def _no_duplicate_object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ProtocolError(f"duplicate field: {key}")
        obj[key] = value
    return obj
