"""Local owner broker for Hermes Kanban worker approvals."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import stat
import threading
import time
from typing import Protocol

from .protocol import (
    ApprovalBridgeDecision,
    ApprovalBridgeRequest,
    ProtocolError,
    MAX_LINE_BYTES,
    decode_line,
    encode_line,
    request_payload_for_native,
)


@dataclass(frozen=True)
class BridgeConfig:
    db_path: str
    socket_path: str
    owner_id: str
    notifier_profile: str
    worker_profile: str = ""  # Read compatibility for pre-v5 installations.
    max_timeout: int = 300
    max_pending: int = 32
    worker_profiles: tuple[str, ...] = ()
    notifier_state_db: str = ""
    board_name: str = "default"

    def __post_init__(self) -> None:
        if not 0 < int(self.max_timeout) <= 300:
            raise ValueError("max_timeout out of range")
        if not 0 < int(self.max_pending) <= 32:
            raise ValueError("max_pending out of range")
        if not str(self.owner_id).isdecimal():
            raise ValueError("owner_id must be numeric")
        if not isinstance(self.worker_profiles, (tuple, list)):
            raise ValueError("worker_profiles must be a list of exact profile names")
        profiles = tuple(self.worker_profiles) or ((self.worker_profile,) if self.worker_profile else ())
        if (not profiles or len(set(profiles)) != len(profiles)
                or any(not isinstance(p, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', p) for p in profiles)
                or (self.worker_profile and self.worker_profile not in profiles)):
            raise ValueError("invalid worker profiles")
        object.__setattr__(self, 'worker_profiles', profiles)
        if not isinstance(self.board_name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', self.board_name):
            raise ValueError('invalid board name')
        if self.notifier_state_db and (not Path(self.notifier_state_db).is_absolute()
                or Path(self.notifier_state_db).name != 'state.db'
                or Path(self.notifier_state_db).parent.name != self.notifier_profile):
            raise ValueError("PM history must belong to the configured notifier profile")


logger = logging.getLogger(__name__)


class ApprovalService(Protocol):
    def request(self, data: dict, route: dict, deadline: float, cancel) -> str:
        """Return 'once' or 'deny' after delegated review or explicit human permission."""
        ...


class Broker:
    def __init__(self, config: BridgeConfig, approval_service: ApprovalService):
        self.config = config
        self.approval_service = approval_service
        self._server: socket.socket | None = None
        self._closed = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending = 0
        self._seen: dict[str, float] = {}
        self._threads: set[threading.Thread] = set()
        self._socket_path: Path | None = None
        self._socket_inode: int | None = None

    def start(self) -> None:
        sock_path = Path(self.config.socket_path)
        self._bind_socket(sock_path)
        self._accept_thread = threading.Thread(target=self._accept_loop, name="kanban-approval-broker", daemon=True)
        self._accept_thread.start()

    def close(self) -> None:
        self._closed.set()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1)
        deadline = time.time() + 2
        for thread in list(self._threads):
            remaining = max(0.01, deadline - time.time())
            thread.join(timeout=remaining)
        self._unlink_own_socket()

    def _bind_socket(self, sock_path: Path) -> None:
        parent = sock_path.parent
        if parent.exists():
            st_parent = parent.lstat()
            if stat.S_ISLNK(st_parent.st_mode) or not stat.S_ISDIR(st_parent.st_mode):
                raise RuntimeError(f"unsafe socket parent: {parent}")
            if (st_parent.st_mode & 0o777) != 0o700:
                raise RuntimeError(f"unsafe socket parent permissions: {parent}")
        else:
            parent.mkdir(mode=0o700, parents=True)
        existing = sock_path.exists() or sock_path.is_socket()
        if existing:
            st = sock_path.lstat()
            if not stat.S_ISSOCK(st.st_mode):
                raise RuntimeError(f"refusing to replace non-socket path: {sock_path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.1)
                probe.connect(str(sock_path))
            except OSError as exc:
                if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                    raise RuntimeError(f"refusing to unlink ambiguous socket path: {sock_path}") from None
                sock_path.unlink()
            else:
                raise RuntimeError(f"broker socket already active: {sock_path}")
            finally:
                probe.close()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(sock_path))
            os.chmod(sock_path, 0o600)
            st = sock_path.lstat()
            self._socket_path = sock_path
            self._socket_inode = st.st_ino
            server.listen(16)
            server.settimeout(0.25)
        except Exception:
            server.close()
            raise
        self._server = server

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            server = self._server
            if server is None:
                return
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                if self._pending >= self.config.max_pending:
                    conn.close()
                    continue
                self._pending += 1
            thread = threading.Thread(target=self._handle_client, args=(conn,), name="kanban-approval-client", daemon=True)
            self._threads.add(thread)
            thread.start()

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            with conn:
                conn.settimeout(1.0)
                peer_pid = _peer_pid(conn)
                raw = _read_line(conn)
                payload = decode_line(raw)
                if payload == {"type": "status"}:
                    status: dict = getattr(self.approval_service, "status", lambda: {"policy": "human-only"})()
                    status['worker_profiles'] = list(self.config.worker_profiles)
                    status['pm_history_configured'] = bool(self.config.notifier_state_db)
                    conn.sendall(encode_line(status))
                    return
                request = ApprovalBridgeRequest.from_dict(payload)
                self._serve_request(conn, request, peer_pid)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
            logger.warning("kanban_bridge rejected stage=broker reason=%s", reason)
            try:
                conn.close()
            except OSError:
                pass
        finally:
            with self._lock:
                self._pending -= 1
            self._threads.discard(threading.current_thread())

    def _serve_request(self, conn: socket.socket, request: ApprovalBridgeRequest, peer_pid: int) -> None:
        now = time.time()
        if request.expires_at <= now:
            raise ProtocolError("expired request")
        if request.timeout_seconds > self.config.max_timeout:
            raise ProtocolError("timeout exceeds config")
        if request.db_path != os.path.realpath(self.config.db_path):
            raise ProtocolError("wrong db")
        if peer_pid != request.worker_pid:
            raise ProtocolError("peer pid mismatch")
        with self._lock:
            if request.request_id in self._seen:
                raise ProtocolError("replayed request")
            self._prune_seen_locked(now)
            if len(self._seen) >= 4096:
                raise ProtocolError("replay cache full")
            self._seen[request.request_id] = request.expires_at

        route = validate_current_request(self.config, request, now)
        task_context = read_task_context(self.config, request.task_id)
        deadline = min(request.expires_at, now + self.config.max_timeout)
        next_validation = 0.0

        def still_current(force: bool = False) -> bool:
            nonlocal next_validation
            current_time = time.time()
            if not force and current_time < next_validation:
                return True
            next_validation = current_time + 0.2
            try:
                current_route = validate_current_request(self.config, request, current_time)
                if read_task_context(self.config, request.task_id) != task_context:
                    return False
            except Exception:
                return False
            return _same_route(route, current_route)

        def cancel() -> bool:
            return (
                self._closed.is_set()
                or time.time() >= deadline
                or _socket_closed(conn)
                or not still_current()
            )

        choice = "deny"
        if not cancel():
            native_data = request_payload_for_native(request)
            native_data["task_context"] = task_context
            choice = self.approval_service.request(native_data, route, deadline, cancel)
        if choice not in ("once", "deny"):
            choice = "deny"
        reason = getattr(self.approval_service, "last_reason", lambda: "")()
        if choice == "once" and not still_current(force=True):
            choice = "deny"
            reason = "authorization_changed"
        evidence = getattr(self.approval_service, 'last_evidence', lambda: [])()
        from .evidence import verify_snapshot
        if choice == 'once' and not verify_snapshot(evidence):
            choice, reason = 'deny', 'source_changed'
        details = getattr(self.approval_service, 'last_details', lambda: '')()
        decision = ApprovalBridgeDecision(request.request_id, request.digest, choice, reason, evidence, details)
        conn.sendall(encode_line(decision.to_dict()))

    def _prune_seen_locked(self, now: float) -> None:
        for request_id, expires_at in list(self._seen.items()):
            if expires_at <= now:
                self._seen.pop(request_id, None)

    def _unlink_own_socket(self) -> None:
        path = self._socket_path
        inode = self._socket_inode
        self._socket_path = None
        self._socket_inode = None
        if path is None or inode is None:
            return
        try:
            st = path.lstat()
            if stat.S_ISSOCK(st.st_mode) and st.st_ino == inode:
                path.unlink()
        except FileNotFoundError:
            return


def _ro_connect(path: str) -> sqlite3.Connection:
    real = os.path.realpath(path)
    uri = Path(real).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def read_task_context(config: BridgeConfig, task_id: str) -> dict:
    from .task_context import read_context
    return read_context(config, task_id)


def validate_current_request(config: BridgeConfig, request: ApprovalBridgeRequest, now: float | None = None) -> dict:
    """Freshly validate task/run ownership and return the one exact owner route."""
    now = time.time() if now is None else now
    db = _ro_connect(config.db_path)
    try:
        if request.expires_at <= now:
            raise ProtocolError("expired request")
        if request.db_path != os.path.realpath(config.db_path):
            raise ProtocolError("wrong db")
        if request.profile not in config.worker_profiles:
            raise ProtocolError("wrong worker profile")
        row = db.execute(
            """
            SELECT
                t.id AS task_id,
                t.status AS task_status,
                t.current_run_id AS current_run_id,
                t.claim_lock AS task_claim_lock,
                t.claim_expires AS task_claim_expires,
                t.worker_pid AS task_worker_pid,
                r.id AS run_id,
                r.status AS run_status,
                r.claim_lock AS run_claim_lock,
                r.claim_expires AS run_claim_expires,
                r.worker_pid AS run_worker_pid,
                r.profile AS run_profile
            FROM tasks t
            JOIN task_runs r ON r.id = t.current_run_id
            WHERE t.id = ? AND r.id = ? AND r.task_id = t.id
            """,
            (request.task_id, request.run_id),
        ).fetchone()
        if row is None:
            raise ProtocolError("run not current")
        if row["task_status"] != "running":
            raise ProtocolError("task not running")
        if row["run_status"] != "running":
            raise ProtocolError("run not running")
        if row["run_profile"] != request.profile:
            raise ProtocolError("wrong worker profile")
        if row["run_claim_lock"] != request.claim_lock or row["task_claim_lock"] != request.claim_lock:
            raise ProtocolError("claim mismatch")
        if int(row["run_worker_pid"] or 0) != request.worker_pid or int(row["task_worker_pid"] or 0) != request.worker_pid:
            raise ProtocolError("worker pid mismatch")
        claim_expires = min(int(row["run_claim_expires"] or 0), int(row["task_claim_expires"] or 0))
        if claim_expires <= int(now):
            raise ProtocolError("claim expired")
        return resolve_owner_context(db, config, request.task_id)
    finally:
        db.close()


def resolve_owner_context(db: sqlite3.Connection, config: BridgeConfig, task_id: str) -> dict:
    rows = db.execute(
        """
        SELECT platform, chat_id, thread_id, user_id, user_id_alt, notifier_profile
        FROM kanban_notify_subs
        WHERE task_id = ? AND platform = 'discord' AND notifier_profile = ?
        """,
        (task_id, config.notifier_profile),
    ).fetchall()
    matches = []
    for row in rows:
        if str(row["user_id"] or "") == config.owner_id and str(row["chat_id"] or "").isdigit():
            thread_id = str(row["thread_id"] or "")
            if thread_id and not thread_id.isdigit():
                continue
            matches.append({
                "platform": "discord",
                "chat_id": str(row["chat_id"]),
                "thread_id": thread_id,
                "owner_id": config.owner_id,
                "notifier_profile": config.notifier_profile,
            })
    unique = {(r["chat_id"], r["thread_id"]) for r in matches}
    if not unique:
        raise ProtocolError("no matching owner subscription")
    # Owner identity authorizes delegation; notification count does not.
    fingerprint = hashlib.sha256(json.dumps(sorted(unique)).encode()).hexdigest()
    context = dict(matches[0])
    context["route_fingerprint"] = fingerprint
    if len(unique) != 1:
        context["chat_id"] = ""
        context["thread_id"] = ""
    return context


def resolve_owner_route(db: sqlite3.Connection, config: BridgeConfig, task_id: str) -> dict:
    context = resolve_owner_context(db, config, task_id)
    if not context["chat_id"]:
        raise ProtocolError("approval route ambiguous")
    return context


def _same_route(left: dict, right: dict) -> bool:
    keys = ("platform", "chat_id", "thread_id", "owner_id", "notifier_profile", "route_fingerprint")
    return all(str(left.get(key, "")) == str(right.get(key, "")) for key in keys)


def _read_line(conn: socket.socket) -> bytes:
    data = bytearray()
    while len(data) <= MAX_LINE_BYTES:
        chunk = conn.recv(1)
        if not chunk:
            raise ProtocolError("client closed")
        data += chunk
        if chunk == b"\n":
            return bytes(data)
    raise ProtocolError("message too large")


def _socket_closed(conn: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        if not readable:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _peer_pid(conn: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        import struct

        creds = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_PEERCRED'), struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", creds)
        if int(uid) != os.getuid():
            raise ProtocolError("peer uid mismatch")
        return int(pid)
    sol_local = getattr(socket, "SOL_LOCAL", 0)
    local_peercred = getattr(socket, "LOCAL_PEERCRED", 1)
    local_peerpid = getattr(socket, "LOCAL_PEERPID", 2)
    try:
        import struct

        # macOS xucred: uint cr_version; uid_t cr_uid; ...
        cred = conn.getsockopt(sol_local, local_peercred, 128)
        version, uid = struct.unpack("=II", cred[:8])
        if version != 0 or int(uid) != os.getuid():
            raise ProtocolError("peer credential mismatch")
        raw = conn.getsockopt(sol_local, local_peerpid, 4)
        return int.from_bytes(raw, byteorder="little", signed=True)
    except OSError as exc:
        if exc.errno in (errno.ENOPROTOOPT, errno.EINVAL):
            raise ProtocolError("peer pid unsupported") from None
        raise
