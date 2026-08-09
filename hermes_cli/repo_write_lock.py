"""POSIX process locks keyed by a repository's Git common directory.

The lock namespace is shared across Hermes profiles and is deliberately derived
inside this module.  Repository identity discovery ignores inherited Git
repository-selection variables, and filesystem objects are opened relative to
a validated private directory without following the leaf symlink.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from hermes_constants import get_default_hermes_root

_POSIX = os.name == "posix"
if _POSIX:
    import fcntl
else:  # pragma: no cover - imported only to keep native Windows importable
    fcntl = None  # type: ignore[assignment]

_UNSUPPORTED = "repo_lock_unsupported"
_IDENTITY_UNAVAILABLE = "repo_identity_unavailable"
_NOT_REPOSITORY = "repo_not_repository"
_SECURITY_VIOLATION = "repo_lock_security_violation"
_BUSY = "repo_lock_busy"
_OPERATION_FAILED = "repo_lock_operation_failed"
_WRONG_PROCESS = "repo_lock_wrong_process"
_NOT_HELD = "repo_lock_not_held"


class RepoLockError(RuntimeError):
    """Base class for repository lock failures."""


class RepoLockUnsupported(RepoLockError):
    """Raised when safe repository locking is unavailable."""


class RepoIdentityError(RepoLockError):
    """Raised when a repository identity cannot be established."""


class RepoNotRepository(RepoIdentityError):
    """Raised only when an existing directory tree has no Git marker."""


class RepoLockBusy(RepoLockError):
    """Raised when a non-blocking lock is already held."""


class RepoLockSecurityError(RepoLockError):
    """Raised when a lock filesystem object fails validation."""


class RepoLockOwnershipError(RepoLockError):
    """Raised when a lock handle is used by the wrong process."""


class RepoLockOperationError(RepoLockError):
    """Raised when the operating system cannot complete a lock operation."""


def _require_posix() -> None:
    if not _POSIX or fcntl is None:
        raise RepoLockUnsupported(_UNSUPPORTED)


def _git_environment() -> dict[str, str]:
    """Return an environment without Git repository-selection state."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _has_git_marker(checkout: Path) -> bool:
    """Inspect checkout parents for a directory or linked-worktree marker."""
    current = checkout
    while True:
        marker = current / ".git"
        marker_info: os.stat_result | None = None
        try:
            marker_info = marker.stat(follow_symlinks=False)
        except FileNotFoundError:
            # Distinguish an absent marker from a checkout-parent race. Any
            # ambiguity while walking an established directory fails closed.
            current_info = current.stat(follow_symlinks=False)
            if not stat.S_ISDIR(current_info.st_mode):
                raise OSError
        if marker_info is not None:
            if stat.S_ISDIR(marker_info.st_mode) or stat.S_ISREG(marker_info.st_mode):
                return True
            raise OSError
        parent = current.parent
        if parent == current:
            return False
        current = parent


def repo_identity(repository: str | os.PathLike[str]) -> str:
    """Return SHA-256 of an existing absolute Git common-directory path.

    Linked worktrees therefore share an identity with their primary checkout,
    while unrelated repositories do not. Existing directory trees with no
    ``.git`` marker raise :class:`RepoNotRepository`; all ambiguous discovery
    and Git failures collapse to a constant-safe :class:`RepoIdentityError`.
    """
    _require_posix()
    identity_failed = False
    checkout: Path | None = None
    marked_checkout = False
    try:
        checkout = Path(repository).expanduser().resolve(strict=True)
        if not checkout.is_dir():
            raise OSError
        marked_checkout = _has_git_marker(checkout)
    except (OSError, RuntimeError, TypeError, ValueError):
        identity_failed = True
    if identity_failed:
        raise RepoIdentityError(_IDENTITY_UNAVAILABLE) from None
    if not marked_checkout:
        raise RepoNotRepository(_NOT_REPOSITORY)

    identity_failed = False
    assert checkout is not None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=checkout,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise OSError
        raw = result.stdout.rstrip(b"\r\n")
        if not raw or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
            raise OSError
        common_dir = Path(os.fsdecode(raw))
        if not common_dir.is_absolute():
            raise OSError
        sanitized = common_dir.resolve(strict=True)
        if not sanitized.is_dir():
            raise OSError
    except Exception:
        identity_failed = True
    if identity_failed:
        raise RepoIdentityError(_IDENTITY_UNAVAILABLE) from None
    return hashlib.sha256(os.fsencode(str(sanitized))).hexdigest()


def _lock_root() -> Path:
    root = Path(get_default_hermes_root()).expanduser()
    if not root.is_absolute():
        root = root.absolute()
    return root / "kanban" / "repo-locks"


def _secure_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY if directory else os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOCTTY", 0)
    return flags


def _best_effort_close(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _best_effort_unlock(fd: int) -> bool:
    assert fcntl is not None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        return False
    return True


def _validate_root(root_fd: int) -> None:
    info = os.fstat(root_fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RepoLockSecurityError(_SECURITY_VIOLATION)


def _open_root(root: Path) -> int:
    if not root.is_absolute() or len(root.parts) < 2:
        raise RepoLockSecurityError(_SECURITY_VIOLATION)

    directory_fd: int | None = None
    try:
        directory_fd = os.open(os.sep, _secure_flags(directory=True))
        os.set_inheritable(directory_fd, False)
        components = root.parts[1:]
        for index, component in enumerate(components):
            created = False
            mode = 0o700 if index == len(components) - 1 else 0o777
            try:
                os.mkdir(component, mode=mode, dir_fd=directory_fd)
                created = True
            except FileExistsError:
                pass

            next_fd = os.open(
                component,
                _secure_flags(directory=True),
                dir_fd=directory_fd,
            )
            os.set_inheritable(next_fd, False)
            os.close(directory_fd)
            directory_fd = next_fd
            if created and index == len(components) - 1:
                os.fchmod(directory_fd, 0o700)

        _validate_root(directory_fd)
        return directory_fd
    except RepoLockSecurityError:
        if directory_fd is not None:
            _best_effort_close(directory_fd)
        raise
    except OSError:
        if directory_fd is not None:
            _best_effort_close(directory_fd)
    raise RepoLockSecurityError(_SECURITY_VIOLATION)


def _validate_leaf(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise RepoLockSecurityError(_SECURITY_VIOLATION)


def _open_leaf(root_fd: int, name: str) -> int:
    flags = _secure_flags()
    created = False
    fd: int | None = None
    try:
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
            created = True
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=root_fd)
        os.set_inheritable(fd, False)
        if created:
            os.fchmod(fd, 0o600)
        _validate_leaf(fd)
        return fd
    except RepoLockSecurityError:
        if fd is not None:
            _best_effort_close(fd)
        raise
    except OSError:
        if fd is not None:
            _best_effort_close(fd)
    raise RepoLockSecurityError(_SECURITY_VIOLATION)


def _open_and_lock(
    identity: str,
    *,
    blocking: bool,
    pending: _PendingLock,
) -> int:
    assert fcntl is not None
    root_fd = _open_root(_lock_root())
    try:
        with _registry_lock:
            fd = _open_leaf(root_fd, f"{identity}.lock")
            pending.fd = fd
    except BaseException:
        _best_effort_close(root_fd)
        raise
    if not _best_effort_close(root_fd):
        with _registry_lock:
            _best_effort_close(fd)
            pending.fd = None
        raise RepoLockOperationError(_OPERATION_FAILED)

    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    lock_failed = False
    lock_errno: int | None = None
    try:
        fcntl.flock(fd, operation)
    except OSError as exc:
        lock_failed = True
        lock_errno = exc.errno
    if lock_failed:
        with _registry_lock:
            _best_effort_close(fd)
            pending.fd = None
        if lock_errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise RepoLockBusy(_BUSY)
        raise RepoLockOperationError(_OPERATION_FAILED)

    metadata_failed = False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
    except OSError:
        metadata_failed = True
    if metadata_failed:
        with _registry_lock:
            _best_effort_unlock(fd)
            _best_effort_close(fd)
            pending.fd = None
        raise RepoLockOperationError(_OPERATION_FAILED)
    return fd


@dataclass
class _HeldLock:
    fd: int
    pid: int
    references: int


@dataclass
class _PendingLock:
    pid: int
    fd: int | None = None


_RegistryEntry = _HeldLock | _PendingLock
_registry_lock = threading.RLock()
_registry_changed = threading.Condition(_registry_lock)
_registry: dict[tuple[str, str], _RegistryEntry] = {}
_handles: weakref.WeakSet[RepoWriteLock] = weakref.WeakSet()


def _before_fork() -> None:
    _registry_lock.acquire()


def _after_fork_parent() -> None:
    _registry_lock.release()


def _after_fork_child() -> None:
    """Close inherited descriptions without unlocking the parent's flock."""
    try:
        descriptors = {entry.fd for entry in _registry.values() if entry.fd is not None}
        for fd in descriptors:
            _best_effort_close(fd)
        _registry.clear()
        for handle in list(_handles):
            handle._reset_after_fork()
    finally:
        _registry_lock.release()


if _POSIX and hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


class RepoWriteLock:
    """PID-bound, reentrant POSIX write lock for one Git repository.

    Separate handles in the same process explicitly share one kernel lock.
    ``blocking=False`` converts contention into :class:`RepoLockBusy`.
    """

    def __init__(
        self,
        repository: str | os.PathLike[str] | Path,
        *,
        blocking: bool = True,
    ) -> None:
        _require_posix()
        self.identity = repo_identity(repository)
        self.blocking = bool(blocking)
        # Normal operations take handle state before the registry.  The fork
        # child reset below must remain lock-free because it runs registry-first.
        self._state_lock = threading.RLock()
        self._pid: int | None = None
        self._fd: int | None = None
        self._depth = 0
        self._key: tuple[str, str] | None = None
        with _registry_lock:
            _handles.add(self)

    def acquire(self) -> Self:
        """Acquire the lock and return this handle."""
        with self._state_lock:
            return self._acquire()

    def _acquire(self) -> Self:
        _require_posix()
        pid = os.getpid()
        if self._depth:
            if self._pid != pid:
                raise RepoLockOwnershipError(_WRONG_PROCESS)
            self._depth += 1
            return self

        root = _lock_root()
        key = (os.fspath(root), self.identity)
        pending: _PendingLock | None = None
        fd: int | None = None
        held: _HeldLock | None = None
        shared_references_before: int | None = None
        acquisition_state = "idle"
        try:
            with _registry_changed:
                while isinstance(_registry.get(key), _PendingLock):
                    if not self.blocking:
                        raise RepoLockBusy(_BUSY)
                    _registry_changed.wait()
                entry = _registry.get(key)
                if isinstance(entry, _HeldLock):
                    if entry.pid != pid:
                        raise RepoLockOwnershipError(_WRONG_PROCESS)
                    held = entry
                    shared_references_before = held.references
                    try:
                        held.references += 1
                        acquisition_state = "shared-reference"
                        self._attach(key, held.fd, pid)
                        acquisition_state = "shared-attached"
                        return self
                    except BaseException:
                        try:
                            if held.references > shared_references_before:
                                held.references -= 1
                        except BaseException:
                            pass
                        try:
                            self._reset()
                        except BaseException:
                            pass
                        acquisition_state = "idle"
                        raise
                pending = _PendingLock(pid)
                _registry[key] = pending
                acquisition_state = "pending"

            fd = _open_and_lock(
                self.identity,
                blocking=self.blocking,
                pending=pending,
            )
            acquisition_state = "kernel-held"

            with _registry_changed:
                held = _HeldLock(fd=fd, pid=pid, references=1)
                _registry[key] = held
                acquisition_state = "new-held"
                self._attach(key, fd, pid)
                acquisition_state = "new-attached"
                _registry_changed.notify_all()
            return self
        except BaseException:
            cleanup_fd = fd
            if cleanup_fd is None and pending is not None:
                cleanup_fd = pending.fd
            if cleanup_fd is None and pending is not None and held is not None:
                cleanup_fd = held.fd
            unlock_attempted = False
            close_attempted = False

            try:
                with _registry_changed:
                    try:
                        entry = _registry.get(key)
                        if pending is not None:
                            if entry is pending or (held is not None and entry is held):
                                del _registry[key]
                        elif (
                            held is not None
                            and entry is held
                            and acquisition_state
                            in {"shared-reference", "shared-attached"}
                            and held.references > 0
                        ):
                            held.references -= 1
                    except BaseException:
                        pass
                    if cleanup_fd is not None:
                        unlock_attempted = True
                        try:
                            _best_effort_unlock(cleanup_fd)
                        except BaseException:
                            pass
                        close_attempted = True
                        try:
                            _best_effort_close(cleanup_fd)
                        except BaseException:
                            pass
                    if pending is not None:
                        try:
                            pending.fd = None
                        except BaseException:
                            pass
                    try:
                        self._reset()
                    except BaseException:
                        pass
                    try:
                        _registry_changed.notify_all()
                    except BaseException:
                        pass
            except BaseException:
                pass

            if cleanup_fd is not None and not unlock_attempted:
                try:
                    _best_effort_unlock(cleanup_fd)
                except BaseException:
                    pass
            if cleanup_fd is not None and not close_attempted:
                try:
                    _best_effort_close(cleanup_fd)
                except BaseException:
                    pass
            if pending is not None:
                try:
                    pending.fd = None
                except BaseException:
                    pass
            try:
                self._reset()
            except BaseException:
                pass
            raise

    def _attach(self, key: tuple[str, str], fd: int, pid: int) -> None:
        self._key = key
        self._fd = fd
        self._pid = pid
        self._depth = 1

    def _reset(self) -> None:
        self._key = None
        self._fd = None
        self._pid = None
        self._depth = 0

    def _reset_after_fork(self) -> None:
        """Replace a possibly inherited locked mutex without acquiring it."""
        self._state_lock = threading.RLock()
        self._reset()

    def release(self) -> None:
        """Release one acquisition level, unlocking after the final handle."""
        with self._state_lock:
            self._release()

    def _release(self) -> None:
        if not self._depth:
            return
        pid = os.getpid()
        if self._pid != pid:
            self._reset()
            raise RepoLockOwnershipError(_WRONG_PROCESS)
        if self._depth > 1:
            self._depth -= 1
            return

        key = self._key
        fd = self._fd
        if key is None or fd is None:
            self._reset()
            raise RepoLockOwnershipError(_WRONG_PROCESS)

        failure = False
        with _registry_changed:
            entry = _registry.get(key)
            if (
                not isinstance(entry, _HeldLock)
                or entry.pid != pid
                or entry.fd != fd
                or entry.references < 1
            ):
                self._reset()
                raise RepoLockOwnershipError(_WRONG_PROCESS)
            entry.references -= 1
            if entry.references == 0:
                failure = not _best_effort_unlock(fd)
                failure = not _best_effort_close(fd) or failure
                del _registry[key]
                _registry_changed.notify_all()
            self._reset()
        if failure:
            raise RepoLockOperationError(_OPERATION_FAILED)

    @property
    def locked(self) -> bool:
        """Whether this handle currently owns an acquisition in this PID."""
        with self._state_lock:
            return self._depth > 0 and self._pid == os.getpid()

    def fileno(self) -> int:
        """Return the live, non-inheritable lock descriptor."""
        with self._state_lock:
            if self._depth <= 0 or self._pid != os.getpid() or self._fd is None:
                raise RepoLockOwnershipError(_NOT_HELD)
            return self._fd

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.release()
            return
        if not isinstance(exc, RepoLockError):
            self.release()
            return
        try:
            self.release()
        except RepoLockOperationError:
            pass


__all__ = [
    "RepoIdentityError",
    "RepoLockBusy",
    "RepoLockError",
    "RepoLockOperationError",
    "RepoLockOwnershipError",
    "RepoLockSecurityError",
    "RepoLockUnsupported",
    "RepoNotRepository",
    "RepoWriteLock",
    "repo_identity",
]
