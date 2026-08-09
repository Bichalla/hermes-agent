"""Typed cross-process write guards for host-local Git mutation endpoints.

The caller supplies only already-resolved absolute endpoint paths. Repository
identity and lock ordering are derived internally from the filesystem and Git;
environment-provided repository identity or roots are never authoritative.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Self

from hermes_cli.repo_write_lock import (
    RepoIdentityError,
    RepoLockBusy,
    RepoLockError,
    RepoLockOperationError,
    RepoLockOwnershipError,
    RepoLockSecurityError,
    RepoLockUnsupported,
    RepoNotRepository,
    RepoWriteLock,
)


class RepoWriteGuardCode(str, Enum):
    """Stable, non-sensitive reasons why a repository mutation was denied."""

    BUSY = "repo_busy"
    SECURITY = "repo_security"
    UNSUPPORTED = "repo_unsupported"
    OPERATION = "repo_operation"
    PATH = "repo_path"


class RepoWriteGuardError(RuntimeError):
    """Base typed denial with a stable code and no path/OS/Git details."""

    code: RepoWriteGuardCode

    def __init__(self, code: RepoWriteGuardCode) -> None:
        self.code = code
        super().__init__(code.value)


class RepoWriteGuardBusy(RepoWriteGuardError):
    def __init__(self) -> None:
        super().__init__(RepoWriteGuardCode.BUSY)


class RepoWriteGuardSecurityError(RepoWriteGuardError):
    def __init__(self) -> None:
        super().__init__(RepoWriteGuardCode.SECURITY)


class RepoWriteGuardUnsupported(RepoWriteGuardError):
    def __init__(self) -> None:
        super().__init__(RepoWriteGuardCode.UNSUPPORTED)


class RepoWriteGuardOperationError(RepoWriteGuardError):
    def __init__(self) -> None:
        super().__init__(RepoWriteGuardCode.OPERATION)


class RepoWriteGuardPathError(RepoWriteGuardError):
    def __init__(self) -> None:
        super().__init__(RepoWriteGuardCode.PATH)


def _path_error() -> RepoWriteGuardPathError:
    return RepoWriteGuardPathError()


@dataclass
class _Lane:
    owner_thread_id: int | None = None
    reservation_tokens: set[object] = field(default_factory=set)
    claim_tokens: dict[object, int] = field(default_factory=dict)

    @property
    def depth(self) -> int:
        return len(self.claim_tokens)

    @property
    def users(self) -> int:
        return len(self.reservation_tokens)


@dataclass
class _FrameItem:
    lock: RepoWriteLock
    token: object = field(default_factory=object, init=False)
    lane: _Lane | None = None
    kernel_attempted: bool = False
    kernel_acquired: bool = False


_lane_registry_lock = threading.RLock()
_lane_registry: dict[str, _Lane] = {}


def _reserve_lane(identity: str, item: _FrameItem) -> None:
    """Publish a stable lane reference before reserving with the item token."""
    with _lane_registry_lock:
        lane = _lane_registry.get(identity)
        if lane is None:
            lane = _Lane()
            item.lane = lane
            _lane_registry[identity] = lane
        else:
            item.lane = lane
        lane.reservation_tokens.add(item.token)


def _recompute_lane_owner(lane: _Lane) -> None:
    """Derive ownership solely from the remaining per-frame claim tokens."""
    lane.owner_thread_id = next(iter(lane.claim_tokens.values()), None)


def _acquire_lane(identity: str, item: _FrameItem) -> None:
    """Acquire a non-blocking thread lane while retaining its reservation."""
    with _lane_registry_lock:
        lane = item.lane
        if (
            lane is None
            or _lane_registry.get(identity) is not lane
            or item.token not in lane.reservation_tokens
        ):
            raise RepoWriteGuardOperationError()
        thread_id = threading.get_ident()
        if lane.owner_thread_id not in (None, thread_id):
            raise RepoWriteGuardBusy()
        lane.claim_tokens[item.token] = thread_id
        _recompute_lane_owner(lane)


def _release_lane(identity: str, item_or_token: _FrameItem | object) -> None:
    """Idempotently discharge only one frame token and prune an empty lane."""
    with _lane_registry_lock:
        if isinstance(item_or_token, _FrameItem):
            lane = item_or_token.lane
            token = item_or_token.token
        else:
            lane = _lane_registry.get(identity)
            token = item_or_token
        if lane is None:
            return
        lane.claim_tokens.pop(token, None)
        lane.reservation_tokens.discard(token)
        _recompute_lane_owner(lane)
        if (
            _lane_registry.get(identity) is lane
            and not lane.reservation_tokens
            and not lane.claim_tokens
        ):
            del _lane_registry[identity]


def _reset_lanes_after_fork() -> None:
    """Discard inherited thread owners and possibly locked mutex state."""
    global _lane_registry, _lane_registry_lock
    _lane_registry = {}
    _lane_registry_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    # RepoWriteLock registers its descriptor/registry reset when imported above.
    # This later child callback then replaces the guard's process-local lane state.
    os.register_at_fork(after_in_child=_reset_lanes_after_fork)


def _map_lock_error(error: BaseException) -> RepoWriteGuardError:
    if isinstance(error, RepoLockBusy):
        return RepoWriteGuardBusy()
    if isinstance(error, RepoLockSecurityError):
        return RepoWriteGuardSecurityError()
    if isinstance(error, RepoLockUnsupported):
        return RepoWriteGuardUnsupported()
    if isinstance(
        error,
        (RepoLockOperationError, RepoLockOwnershipError, RepoLockError, OSError),
    ):
        return RepoWriteGuardOperationError()
    return RepoWriteGuardOperationError()


def _nearest_existing_directory(endpoint: Path) -> Path:
    """Return the existing directory that establishes endpoint repo identity."""
    if not isinstance(endpoint, Path) or not endpoint.is_absolute():
        raise _path_error()

    try:
        # Mutation endpoints are files. For both an existing file and a new file,
        # discovery starts at the parent and climbs through not-yet-created
        # nested directories to the nearest existing directory.
        candidate = endpoint.parent
        while True:
            if candidate.exists():
                if not candidate.is_dir():
                    raise _path_error()
                return candidate.resolve(strict=True)
            parent = candidate.parent
            if parent == candidate:
                raise _path_error()
            candidate = parent
    except RepoWriteGuardError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _path_error() from None


def _existing_directory_endpoint(endpoint: Path) -> Path:
    """Return one strict, canonical directory endpoint or a typed denial."""
    if not isinstance(endpoint, Path) or not endpoint.is_absolute():
        raise _path_error()
    try:
        if not endpoint.is_dir():
            raise _path_error()
        return endpoint.resolve(strict=True)
    except RepoWriteGuardError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _path_error() from None


class RepoWriteGuard:
    """Acquire all repository locks needed for exact local mutation endpoints.

    Endpoints outside Git are intentionally skipped. Distinct repositories are
    deduplicated by the identity calculated by :class:`RepoWriteLock`, sorted by
    that identity, acquired non-blocking in that stable order, and released in
    reverse order. Partial acquisition is always unwound.
    """

    def __init__(self, endpoints: Iterable[Path], *, directories: bool = False) -> None:
        try:
            supplied = tuple(endpoints)
        except (TypeError, ValueError):
            raise _path_error() from None

        locks_by_identity: dict[str, RepoWriteLock] = {}
        for endpoint in supplied:
            try:
                anchor = (
                    _existing_directory_endpoint(endpoint)
                    if directories
                    else _nearest_existing_directory(endpoint)
                )
                lock = RepoWriteLock(anchor, blocking=False)
            except RepoNotRepository:
                # A host-local path outside a Git checkout retains legacy file
                # tool behavior and needs no repository-wide lock.
                continue
            except RepoIdentityError as error:
                raise _map_lock_error(error) from None
            except RepoWriteGuardError:
                raise
            except (
                RepoLockBusy,
                RepoLockSecurityError,
                RepoLockUnsupported,
                RepoLockOperationError,
                RepoLockOwnershipError,
                RepoLockError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise _map_lock_error(error) from None
            locks_by_identity.setdefault(lock.identity, lock)

        self._locks = tuple(
            locks_by_identity[identity] for identity in sorted(locks_by_identity)
        )
        self._frames: list[list[_FrameItem]] = []

    @property
    def identities(self) -> tuple[str, ...]:
        """Return internally-derived identities in acquisition order."""
        return tuple(lock.identity for lock in self._locks)

    @staticmethod
    def _release_frame_item(item: _FrameItem) -> list[BaseException]:
        """Idempotently unwind kernel then lane obligations, recording failures."""
        failures: list[BaseException] = []
        control_types = (KeyboardInterrupt, SystemExit, GeneratorExit)
        while item.kernel_attempted:
            try:
                item.lock.release()
            except control_types as error:
                failures.append(error)
                try:
                    locked = bool(
                        getattr(item.lock, "locked", getattr(item.lock, "held", False))
                    )
                except BaseException as status_error:
                    failures.append(status_error)
                    break
                if locked:
                    continue
                item.kernel_attempted = False
                item.kernel_acquired = False
            except BaseException as error:
                failures.append(error)
                try:
                    locked = bool(
                        getattr(item.lock, "locked", getattr(item.lock, "held", False))
                    )
                except BaseException as status_error:
                    failures.append(status_error)
                    break
                if not locked:
                    item.kernel_attempted = False
                    item.kernel_acquired = False
                break
            else:
                item.kernel_attempted = False
                item.kernel_acquired = False
        while item.lane is not None:
            try:
                _release_lane(item.lock.identity, item)
                item.lane = None
            except control_types as error:
                failures.append(error)
                continue
            except BaseException as error:
                failures.append(error)
                break
        return failures

    def acquire(self) -> Self:
        attempted_this_call: list[_FrameItem] = []
        try:
            # Publish the frame itself before any lane or kernel transition.
            self._frames.append(attempted_this_call)
            for lock in self._locks:
                item = _FrameItem(lock=lock)
                attempted_this_call.append(item)
                _reserve_lane(lock.identity, item)
                _acquire_lane(lock.identity, item)
                item.kernel_attempted = True
                lock.acquire()
                item.kernel_acquired = True
        except BaseException as error:
            cleanup_failures: list[BaseException] = []
            for attempted in reversed(attempted_this_call):
                cleanup_failures.extend(self._release_frame_item(attempted))
            if self._frames and self._frames[-1] is attempted_this_call:
                self._frames.pop()
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            cleanup_control = next(
                (
                    failure
                    for failure in cleanup_failures
                    if isinstance(
                        failure, (KeyboardInterrupt, SystemExit, GeneratorExit)
                    )
                ),
                None,
            )
            if cleanup_control is not None:
                raise cleanup_control
            if isinstance(error, RepoWriteGuardError):
                raise
            raise _map_lock_error(error) from None
        return self

    def release(self) -> None:
        if not self._frames:
            return
        control_failure: KeyboardInterrupt | SystemExit | GeneratorExit | None = None
        typed_failure: RepoWriteGuardError | None = None
        acquired = self._frames[-1]
        for item in reversed(acquired):
            for error in self._release_frame_item(item):
                if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    if control_failure is None:
                        control_failure = error
                elif typed_failure is None:
                    typed_failure = (
                        error
                        if isinstance(error, RepoWriteGuardError)
                        else _map_lock_error(error)
                    )
        if all(not item.kernel_attempted and item.lane is None for item in acquired):
            if self._frames and self._frames[-1] is acquired:
                self._frames.pop()
        if control_failure is not None:
            raise control_failure
        if typed_failure is not None:
            raise typed_failure

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.release()
        except RepoWriteGuardError:
            if exc_type is None:
                raise


def guard_repo_write_endpoints(endpoints: Iterable[Path]) -> RepoWriteGuard:
    """Construct a typed guard for resolved host-local mutation endpoints."""
    return RepoWriteGuard(endpoints)


# Descriptive alias for callers that prefer a context-manager factory name.
repo_write_guard = guard_repo_write_endpoints


__all__ = [
    "RepoWriteGuard",
    "RepoWriteGuardBusy",
    "RepoWriteGuardCode",
    "RepoWriteGuardError",
    "RepoWriteGuardOperationError",
    "RepoWriteGuardPathError",
    "RepoWriteGuardSecurityError",
    "RepoWriteGuardUnsupported",
    "guard_repo_write_endpoints",
    "repo_write_guard",
]
