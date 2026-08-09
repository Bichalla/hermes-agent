from __future__ import annotations

import dis
import errno
import hashlib
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import traceback
from pathlib import Path

import pytest

import hermes_cli.repo_write_guard as repo_guard
import hermes_cli.repo_write_lock as repo_lock
from hermes_cli.repo_write_lock import (
    RepoIdentityError,
    RepoLockBusy,
    RepoLockOperationError,
    RepoLockSecurityError,
    RepoLockUnsupported,
    RepoWriteLock,
    repo_identity,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX flock tests")


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_repo(path: Path) -> Path:
    path.mkdir()
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "repo-lock@example.invalid", cwd=path)
    _git("config", "user.name", "Repo Lock Test", cwd=path)
    (path / "tracked.txt").write_text("initial\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "initial", cwd=path)
    return path


def _lock_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    return env


def _contender(repo: Path, root: Path) -> subprocess.CompletedProcess[str]:
    code = textwrap.dedent(
        """
        import sys
        from hermes_cli.repo_write_lock import RepoLockBusy, RepoWriteLock
        try:
            with RepoWriteLock(sys.argv[1], blocking=False):
                print("acquired")
        except RepoLockBusy as exc:
            print(str(exc))
            raise SystemExit(23)
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(repo)],
        cwd=Path(__file__).parents[2],
        env=_lock_env(root),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _assert_constant_safe_failure(
    error: BaseException,
    expected_type: type[BaseException],
    expected_text: str,
    *hostile_text: str,
) -> None:
    assert type(error) is expected_type
    assert str(error) == expected_text
    assert expected_text in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    trace = "".join(traceback.format_exception(error))
    for secret in hostile_text:
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in trace


def test_repo_identity_uses_absolute_git_common_dir_and_ignores_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _make_repo(tmp_path / "primary")
    worktree = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "linked-test", str(worktree), cwd=primary)
    other = _make_repo(tmp_path / "other")

    common = Path(
        _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=primary)
    ).resolve(strict=True)
    expected = hashlib.sha256(os.fsencode(str(common))).hexdigest()

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    assert repo_identity(primary) == expected
    assert repo_identity(worktree) == expected
    assert repo_identity(other) != expected


def test_repo_identity_failure_is_typed_and_constant_safe(tmp_path: Path) -> None:
    missing = tmp_path / "secret-missing-repository"
    with pytest.raises(RepoIdentityError) as error:
        repo_identity(missing)
    _assert_constant_safe_failure(
        error.value,
        RepoIdentityError,
        "repo_identity_unavailable",
        str(missing),
    )


def test_existing_directory_without_git_marker_is_distinct_and_constant_safe(
    tmp_path: Path,
) -> None:
    outside_git = tmp_path / "secret-existing-non-repository"
    outside_git.mkdir()
    expected_type = getattr(repo_lock, "RepoNotRepository", None)
    assert expected_type is not None

    with pytest.raises(expected_type) as error:
        repo_identity(outside_git)

    _assert_constant_safe_failure(
        error.value,
        expected_type,
        "repo_not_repository",
        str(outside_git),
    )
    assert issubclass(expected_type, RepoIdentityError)


def test_existing_non_directory_keeps_exact_legacy_identity_error(tmp_path: Path) -> None:
    non_directory = tmp_path / "secret-regular-file"
    non_directory.write_text("not a checkout\n", encoding="utf-8")

    with pytest.raises(RepoIdentityError) as error:
        repo_identity(non_directory)

    _assert_constant_safe_failure(
        error.value,
        RepoIdentityError,
        "repo_identity_unavailable",
        str(non_directory),
    )


@pytest.mark.parametrize(
    "failure", ["timeout", "oserror", "nonzero", "malformed", "malformed-result"]
)
def test_marked_checkout_git_failures_remain_identity_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = _make_repo(tmp_path / f"repo-{failure}")
    secret = "TOP-SECRET /private/checkout"

    def fail_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(secret, 10)
        if failure == "oserror":
            raise OSError(secret)
        if failure == "nonzero":
            return subprocess.CompletedProcess([], 23, stdout=secret.encode())
        if failure == "malformed-result":
            return object()  # type: ignore[return-value]
        return subprocess.CompletedProcess([], 0, stdout=secret.encode() + b"\nother\n")

    monkeypatch.setattr(repo_lock.subprocess, "run", fail_git)

    with pytest.raises(RepoIdentityError) as error:
        repo_identity(repository)

    _assert_constant_safe_failure(
        error.value,
        RepoIdentityError,
        "repo_identity_unavailable",
        secret,
        str(repository),
    )


def test_git_marker_stat_ambiguity_fails_closed_as_identity_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo-marker-error")
    marker = repository / ".git"
    original_stat = Path.stat

    def ambiguous_stat(path: Path, *args: object, **kwargs: object):
        if path == marker:
            raise PermissionError("TOP-SECRET marker permission")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", ambiguous_stat)
    with pytest.raises(RepoIdentityError) as error:
        repo_identity(repository)

    _assert_constant_safe_failure(
        error.value,
        RepoIdentityError,
        "repo_identity_unavailable",
        "TOP-SECRET",
        str(repository),
    )


def test_windows_is_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(repo_lock, "_POSIX", False)
    with pytest.raises(RepoLockUnsupported, match="^repo_lock_unsupported$"):
        repo_identity(tmp_path)
    with pytest.raises(RepoLockUnsupported, match="^repo_lock_unsupported$"):
        RepoWriteLock(tmp_path)


def test_lock_root_is_internal_and_leaf_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "shared-hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))

    lock = RepoWriteLock(repository)
    with lock:
        assert lock.locked
        assert os.get_inheritable(lock.fileno()) is False
        lock_path = hermes_root / "kanban" / "repo-locks" / f"{repo_identity(repository)}.lock"
        assert lock_path.is_file()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock_path.stat().st_nlink == 1
        root_stat = lock_path.parent.stat()
        assert root_stat.st_uid == os.geteuid()
        assert stat.S_IMODE(root_stat.st_mode) == 0o700
    assert not lock.locked


@pytest.mark.parametrize("hostile_kind", ["mode", "symlink", "hardlink"])
def test_hostile_existing_leaf_is_rejected_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile_kind: str
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    lock_root = hermes_root / "kanban" / "repo-locks"
    lock_root.mkdir(parents=True, mode=0o700)
    lock_path = lock_root / f"{repo_identity(repository)}.lock"

    if hostile_kind == "mode":
        lock_path.write_text("hostile")
        lock_path.chmod(0o644)
    elif hostile_kind == "symlink":
        target = tmp_path / "target"
        target.write_text("hostile")
        lock_path.symlink_to(target)
    else:
        target = tmp_path / "target"
        target.write_text("hostile")
        target.chmod(0o600)
        os.link(target, lock_path)

    with pytest.raises(RepoLockSecurityError, match="^repo_lock_security_violation$"):
        RepoWriteLock(repository).acquire()

    if hostile_kind == "mode":
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o644
    elif hostile_kind == "symlink":
        assert lock_path.is_symlink()
    else:
        assert lock_path.stat().st_nlink == 2


def test_hostile_existing_root_is_rejected_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    lock_root = hermes_root / "kanban" / "repo-locks"
    lock_root.mkdir(parents=True, mode=0o755)
    lock_root.chmod(0o755)

    with pytest.raises(RepoLockSecurityError, match="^repo_lock_security_violation$"):
        RepoWriteLock(repository).acquire()
    assert stat.S_IMODE(lock_root.stat().st_mode) == 0o755


def test_symlink_lock_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    target = tmp_path / "hostile-root"
    target.mkdir(mode=0o700)
    lock_root = hermes_root / "kanban" / "repo-locks"
    lock_root.parent.mkdir(parents=True)
    lock_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RepoLockSecurityError, match="^repo_lock_security_violation$"):
        RepoWriteLock(repository).acquire()
    assert lock_root.is_symlink()


def test_upper_kanban_symlink_is_rejected_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    target = tmp_path / "hostile-kanban"
    target.mkdir(mode=0o700)
    (hermes_root / "kanban").symlink_to(target, target_is_directory=True)

    with pytest.raises(RepoLockSecurityError, match="^repo_lock_security_violation$"):
        RepoWriteLock(repository).acquire()
    assert (hermes_root / "kanban").is_symlink()
    assert list(target.iterdir()) == []


def test_nonblocking_contention_is_typed_and_releases_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))

    with RepoWriteLock(repository):
        contender = _contender(repository, hermes_root)
        assert contender.returncode == 23
        assert contender.stdout.strip() == "repo_lock_busy"
    contender = _contender(repository, hermes_root)
    assert contender.returncode == 0
    assert contender.stdout.strip() == "acquired"


def test_nonblocking_same_process_pending_acquisition_fails_promptly_and_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    original_open_and_lock = getattr(repo_lock, "_open_and_lock")
    pending_published = threading.Event()
    continue_first = threading.Event()
    second_finished = threading.Event()
    first_errors: list[BaseException] = []
    second_outcomes: list[RepoWriteLock | BaseException] = []

    def paused_open_and_lock(identity: str, *, blocking: bool, pending: object) -> int:
        if blocking:
            pending_published.set()
            assert continue_first.wait(timeout=10)
        return original_open_and_lock(identity, blocking=blocking, pending=pending)

    monkeypatch.setattr(repo_lock, "_open_and_lock", paused_open_and_lock)
    first = RepoWriteLock(repository)
    second = RepoWriteLock(repository, blocking=False)

    def acquire_first() -> None:
        try:
            first.acquire()
        except BaseException as exc:
            first_errors.append(exc)

    def acquire_second() -> None:
        try:
            second_outcomes.append(second.acquire())
        except BaseException as exc:
            second_outcomes.append(exc)
        finally:
            second_finished.set()

    first_worker = threading.Thread(target=acquire_first)
    second_worker = threading.Thread(target=acquire_second)
    first_worker.start()
    assert pending_published.wait(timeout=10)
    second_worker.start()
    completed_promptly = second_finished.wait(timeout=2)
    continue_first.set()
    first_worker.join(timeout=10)
    second_worker.join(timeout=10)

    try:
        assert completed_promptly
        assert not first_worker.is_alive()
        assert not second_worker.is_alive()
        assert first_errors == []
        assert len(second_outcomes) == 1
        outcome = second_outcomes[0]
        assert isinstance(outcome, BaseException)
        _assert_constant_safe_failure(outcome, RepoLockBusy, "repo_lock_busy")
    finally:
        if second.locked:
            second.release()
        if first.locked:
            first.release()

    contender = _contender(repository, hermes_root)
    assert (contender.returncode, contender.stdout.strip()) == (0, "acquired")
    with RepoWriteLock(repository, blocking=False):
        pass


def test_pending_to_held_attach_baseexception_rolls_back_and_wakes_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    original_open_and_lock = repo_lock._open_and_lock
    original_wait = repo_lock._registry_changed.wait
    kernel_locked = threading.Event()
    continue_transition = threading.Event()
    waiter_waiting = threading.Event()
    waiter_notified = threading.Event()
    allow_waiter_to_continue = threading.Event()
    waiter_acquired = threading.Event()
    owner_outcomes: list[RepoWriteLock | BaseException] = []
    waiter_errors: list[BaseException] = []
    captured_pending: list[repo_lock._PendingLock] = []
    captured_fd: list[int] = []

    class AttachInterrupted(BaseException):
        pass

    interruption = AttachInterrupted("attach-interrupted")
    owner = RepoWriteLock(repository)
    waiter = RepoWriteLock(repository)
    key = (os.fspath(repo_lock._lock_root()), owner.identity)

    def paused_open_and_lock(
        identity: str, *, blocking: bool, pending: repo_lock._PendingLock
    ) -> int:
        fd = original_open_and_lock(identity, blocking=blocking, pending=pending)
        captured_pending.append(pending)
        captured_fd.append(fd)
        kernel_locked.set()
        assert continue_transition.wait(timeout=10)
        return fd

    def interrupt_after_partial_attach(
        attach_key: tuple[str, str], fd: int, pid: int
    ) -> None:
        owner._key = attach_key
        owner._fd = fd
        raise interruption

    def observed_wait(timeout: float | None = None) -> bool:
        if threading.current_thread().name != "transition-waiter":
            return original_wait(timeout)
        waiter_waiting.set()
        result = original_wait(timeout)
        waiter_notified.set()
        assert allow_waiter_to_continue.wait(timeout=10)
        return result

    monkeypatch.setattr(repo_lock, "_open_and_lock", paused_open_and_lock)
    monkeypatch.setattr(owner, "_attach", interrupt_after_partial_attach)
    monkeypatch.setattr(repo_lock._registry_changed, "wait", observed_wait)

    def acquire_owner() -> None:
        try:
            owner_outcomes.append(owner.acquire())
        except BaseException as exc:
            owner_outcomes.append(exc)

    def acquire_waiter() -> None:
        try:
            waiter.acquire()
            waiter_acquired.set()
        except BaseException as exc:
            waiter_errors.append(exc)
        finally:
            if waiter.locked:
                waiter.release()

    owner_worker = threading.Thread(target=acquire_owner, name="transition-owner")
    waiter_worker = threading.Thread(target=acquire_waiter, name="transition-waiter")
    owner_worker.start()
    assert kernel_locked.wait(timeout=10)
    waiter_worker.start()
    assert waiter_waiting.wait(timeout=10)
    continue_transition.set()
    owner_worker.join(timeout=10)

    try:
        assert not owner_worker.is_alive()
        assert len(owner_outcomes) == 1
        assert owner_outcomes[0] is interruption
        assert waiter_notified.wait(timeout=2)
        assert (owner._key, owner._fd, owner._pid, owner._depth) == (
            None,
            None,
            None,
            0,
        )
        assert key not in repo_lock._registry
        assert captured_pending[0].fd is None
        with pytest.raises(OSError) as closed:
            os.fstat(captured_fd[0])
        assert closed.value.errno == errno.EBADF
        contender = _contender(repository, hermes_root)
        assert (contender.returncode, contender.stdout.strip()) == (0, "acquired")

        allow_waiter_to_continue.set()
        waiter_worker.join(timeout=10)
        assert not waiter_worker.is_alive()
        assert waiter_errors == []
        assert waiter_acquired.is_set()
        assert key not in repo_lock._registry
    finally:
        continue_transition.set()
        allow_waiter_to_continue.set()
        owner_worker.join(timeout=10)
        waiter_worker.join(timeout=1)
        if waiter_worker.is_alive():
            with repo_lock._registry_changed:
                leaked = repo_lock._registry.pop(key, None)
                if leaked is not None and leaked.fd is not None:
                    repo_lock._best_effort_unlock(leaked.fd)
                    repo_lock._best_effort_close(leaked.fd)
                for pending in captured_pending:
                    pending.fd = None
                repo_lock._registry_changed.notify_all()
            waiter_worker.join(timeout=10)
        with repo_lock._registry_changed:
            leaked = repo_lock._registry.pop(key, None)
            if leaked is not None and leaked.fd is not None:
                repo_lock._best_effort_unlock(leaked.fd)
                repo_lock._best_effort_close(leaked.fd)
            repo_lock._registry_changed.notify_all()
        owner._reset()
        if waiter.locked:
            waiter.release()


def test_same_pid_reentrancy_keeps_kernel_lock_until_last_handle_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    first = RepoWriteLock(repository).acquire()
    second = RepoWriteLock(repository).acquire()
    try:
        first.release()
        assert _contender(repository, hermes_root).returncode == 23
        second.release()
        assert _contender(repository, hermes_root).returncode == 0
    finally:
        if first.locked:
            first.release()
        if second.locked:
            second.release()


def test_lifetime_lock_allows_one_guard_thread_lane_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard, RepoWriteGuardBusy

    repository = _make_repo(tmp_path / "repo-lifetime-lane")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    lifetime = RepoWriteLock(repository, blocking=False).acquire()
    first_entered = threading.Event()
    allow_first = threading.Event()
    first_errors: list[BaseException] = []
    second_outcomes: list[str | BaseException] = []

    def hold_guard() -> None:
        try:
            with RepoWriteGuard([repository / "first.txt"]):
                first_entered.set()
                assert allow_first.wait(timeout=10)
        except BaseException as error:
            first_errors.append(error)

    def contend_guard() -> None:
        try:
            with RepoWriteGuard([repository / "second.txt"]):
                second_outcomes.append("acquired")
        except BaseException as error:
            second_outcomes.append(error)

    owner = threading.Thread(target=hold_guard, name="lifetime-lane-owner")
    owner.start()
    assert first_entered.wait(timeout=10)
    assert _contender(repository, hermes_root).returncode == 23

    blocked = threading.Thread(target=contend_guard, name="lifetime-lane-blocked")
    blocked.start()
    blocked.join(timeout=10)
    assert not blocked.is_alive()
    assert len(second_outcomes) == 1
    assert isinstance(second_outcomes[0], RepoWriteGuardBusy)
    assert str(second_outcomes[0]) == "repo_busy"

    allow_first.set()
    owner.join(timeout=10)
    assert not owner.is_alive()
    assert first_errors == []
    assert _contender(repository, hermes_root).returncode == 23

    second_outcomes.clear()
    successor = threading.Thread(target=contend_guard, name="lifetime-lane-successor")
    successor.start()
    successor.join(timeout=10)
    assert not successor.is_alive()
    assert second_outcomes == ["acquired"]
    assert _contender(repository, hermes_root).returncode == 23

    lifetime.release()
    assert _contender(repository, hermes_root).returncode == 0


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_child_gets_fresh_guard_lane_registry_without_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.repo_write_guard as guard_module

    repository = _make_repo(tmp_path / "repo-fork-lane")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    entered = threading.Event()
    allow_owner = threading.Event()
    owner_errors: list[BaseException] = []

    def hold_guard() -> None:
        try:
            with guard_module.RepoWriteGuard([repository / "owner.txt"]):
                entered.set()
                assert allow_owner.wait(timeout=10)
        except BaseException as error:
            owner_errors.append(error)

    owner = threading.Thread(target=hold_guard, name="fork-lane-owner")
    owner.start()
    assert entered.wait(timeout=10)
    assert guard_module._lane_registry

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            registry_fresh = not guard_module._lane_registry
            try:
                with guard_module.RepoWriteGuard([repository / "child.txt"]):
                    outcome = "acquired"
            except guard_module.RepoWriteGuardBusy:
                outcome = "kernel-busy"
            payload = f"{int(registry_fresh)}:{outcome}".encode("ascii")
        except BaseException as error:
            payload = f"error:{type(error).__name__}".encode("ascii")
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        payload = os.read(read_fd, 128).decode("ascii")
        waited, status = os.waitpid(child, 0)
        assert waited == child
        assert os.waitstatus_to_exitcode(status) == 0
        assert payload == "1:kernel-busy"
    finally:
        os.close(read_fd)
        allow_owner.set()
        owner.join(timeout=10)

    assert not owner.is_alive()
    assert owner_errors == []
    assert not guard_module._lane_registry


def test_concurrent_acquires_on_same_handle_release_every_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    original_open_and_lock = repo_lock._open_and_lock
    original_wait = repo_lock._registry_changed.wait
    pending_published = threading.Event()
    continue_first = threading.Event()
    second_blocked = threading.Event()
    outcomes: list[RepoWriteLock | BaseException] = []

    class ObservedRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self) -> ObservedRLock:
            if threading.current_thread().name == "same-handle-second":
                second_blocked.set()
            self._lock.acquire()
            return self

        def __exit__(self, *args: object) -> None:
            self._lock.release()

    def paused_open_and_lock(identity: str, *, blocking: bool, pending: object) -> int:
        pending_published.set()
        assert continue_first.wait(timeout=10)
        return original_open_and_lock(identity, blocking=blocking, pending=pending)

    def observed_wait(timeout: float | None = None) -> bool:
        second_blocked.set()
        return original_wait(timeout)

    monkeypatch.setattr(repo_lock, "_open_and_lock", paused_open_and_lock)
    monkeypatch.setattr(repo_lock._registry_changed, "wait", observed_wait)
    lock = RepoWriteLock(repository)
    monkeypatch.setattr(lock, "_state_lock", ObservedRLock(), raising=False)
    key = (os.fspath(repo_lock._lock_root()), lock.identity)

    def acquire() -> None:
        try:
            outcomes.append(lock.acquire())
        except BaseException as exc:
            outcomes.append(exc)

    first_worker = threading.Thread(target=acquire, name="same-handle-first")
    second_worker = threading.Thread(target=acquire, name="same-handle-second")
    first_worker.start()
    assert pending_published.wait(timeout=10)
    second_worker.start()
    assert second_blocked.wait(timeout=10)
    continue_first.set()
    first_worker.join(timeout=10)
    second_worker.join(timeout=10)

    try:
        assert not first_worker.is_alive()
        assert not second_worker.is_alive()
        assert outcomes == [lock, lock]

        lock.release()
        lock.release()

        contender = _contender(repository, hermes_root)
        assert (contender.returncode, contender.stdout.strip()) == (0, "acquired")
        assert not lock.locked
        assert (lock._key, lock._fd, lock._pid, lock._depth) == (None, None, None, 0)
        assert key not in repo_lock._registry
    finally:
        continue_first.set()
        first_worker.join(timeout=10)
        second_worker.join(timeout=10)
        with repo_lock._registry_changed:
            leaked = repo_lock._registry.pop(key, None)
            if leaked is not None and leaked.fd is not None:
                repo_lock._best_effort_unlock(leaked.fd)
                repo_lock._best_effort_close(leaked.fd)
            repo_lock._registry_changed.notify_all()
        lock._reset()


def test_stale_metadata_does_not_claim_kernel_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    lock_root = hermes_root / "kanban" / "repo-locks"
    lock_root.mkdir(parents=True, mode=0o700)
    lock_path = lock_root / f"{repo_identity(repository)}.lock"
    lock_path.write_text("999999\n")
    lock_path.chmod(0o600)

    with RepoWriteLock(repository, blocking=False):
        pass


def test_clean_process_exit_releases_lock(tmp_path: Path) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    code = textwrap.dedent(
        """
        import sys
        from hermes_cli.repo_write_lock import RepoWriteLock
        lock = RepoWriteLock(sys.argv[1]).acquire()
        print("ready", flush=True)
        sys.stdin.readline()
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(repository)],
        cwd=Path(__file__).parents[2],
        env=_lock_env(hermes_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert _contender(repository, hermes_root).returncode == 23
        assert process.stdin is not None
        process.stdin.write("exit\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
        assert _contender(repository, hermes_root).returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_sigkill_releases_lock(tmp_path: Path) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    code = textwrap.dedent(
        """
        import sys, time
        from hermes_cli.repo_write_lock import RepoWriteLock
        lock = RepoWriteLock(sys.argv[1]).acquire()
        print("ready", flush=True)
        time.sleep(30)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(repository)],
        cwd=Path(__file__).parents[2],
        env=_lock_env(hermes_root),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert _contender(repository, hermes_root).returncode == 23
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        assert _contender(repository, hermes_root).returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_child_closes_descriptor_so_parent_sigkill_releases_lock(
    tmp_path: Path,
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    code = textwrap.dedent(
        """
        import os, sys, time
        from hermes_cli.repo_write_lock import RepoWriteLock
        lock = RepoWriteLock(sys.argv[1]).acquire()
        child = os.fork()
        if child == 0:
            time.sleep(30)
        else:
            print(f"ready {os.getpid()} {child}", flush=True)
            time.sleep(30)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(repository)],
        cwd=Path(__file__).parents[2],
        env=_lock_env(hermes_root),
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        assert process.stdout is not None
        marker, owner_text, child_text = process.stdout.readline().split()
        assert marker == "ready"
        assert int(owner_text) == process.pid
        child_pid = int(child_text)
        assert _contender(repository, hermes_root).returncode == 23
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        os.kill(child_pid, 0)
        assert _contender(repository, hermes_root).returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("boundary", ["open-root", "open-leaf"])
def test_security_failure_cleanup_error_preserves_typed_constant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    hostile = f"hostile-cleanup:{tmp_path / 'private-path'}"
    original_close = os.close
    target_fd: int | None = None

    def reject(fd: int) -> None:
        nonlocal target_fd
        target_fd = fd
        raise RepoLockSecurityError("repo_lock_security_violation")

    def fail_target_close(fd: int) -> None:
        if fd == target_fd:
            original_close(fd)
            raise OSError(hostile)
        original_close(fd)

    monkeypatch.setattr(os, "close", fail_target_close)
    if boundary == "open-root":
        monkeypatch.setattr(repo_lock, "_validate_root", reject)
        action = lambda: repo_lock._open_root(tmp_path / "lock-root")
        root_fd = None
    else:
        lock_root = tmp_path / "lock-root"
        lock_root.mkdir(mode=0o700)
        root_fd = os.open(lock_root, repo_lock._secure_flags(directory=True))
        monkeypatch.setattr(repo_lock, "_validate_leaf", reject)
        action = lambda: repo_lock._open_leaf(root_fd, "identity.lock")

    try:
        with pytest.raises(RepoLockSecurityError) as captured:
            action()
    finally:
        if root_fd is not None:
            original_close(root_fd)
    _assert_constant_safe_failure(
        captured.value,
        RepoLockSecurityError,
        "repo_lock_security_violation",
        hostile,
        str(tmp_path / "private-path"),
    )


def test_root_cleanup_does_not_mask_selected_leaf_security_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = "hostile-root-close:/private/root"
    pending = repo_lock._PendingLock(os.getpid())
    monkeypatch.setattr(repo_lock, "_open_root", lambda root: 101)

    def reject_leaf(root_fd: int, name: str) -> int:
        raise RepoLockSecurityError("repo_lock_security_violation")

    monkeypatch.setattr(repo_lock, "_open_leaf", reject_leaf)

    def fail_close(fd: int) -> None:
        raise OSError(hostile)

    monkeypatch.setattr(os, "close", fail_close)
    with pytest.raises(RepoLockSecurityError) as captured:
        repo_lock._open_and_lock("identity", blocking=False, pending=pending)
    _assert_constant_safe_failure(
        captured.value,
        RepoLockSecurityError,
        "repo_lock_security_violation",
        hostile,
        "/private/root",
    )


@pytest.mark.parametrize(
    ("flock_errno", "expected_type", "expected_text"),
    [
        (errno.EWOULDBLOCK, RepoLockBusy, "repo_lock_busy"),
        (errno.EIO, RepoLockOperationError, "repo_lock_operation_failed"),
    ],
)
def test_flock_failure_cleanup_error_preserves_typed_constant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flock_errno: int,
    expected_type: type[BaseException],
    expected_text: str,
) -> None:
    hostile = f"hostile-flock-cleanup:{tmp_path / 'private-lock'}"
    pending = repo_lock._PendingLock(os.getpid())
    original_open_leaf = repo_lock._open_leaf
    original_close = os.close
    leaf_fd: int | None = None

    def capture_leaf(root_fd: int, name: str) -> int:
        nonlocal leaf_fd
        leaf_fd = original_open_leaf(root_fd, name)
        return leaf_fd

    def fail_flock(fd: int, operation: int) -> None:
        raise OSError(flock_errno, hostile)

    def fail_leaf_close(fd: int) -> None:
        original_close(fd)
        if fd == leaf_fd:
            raise OSError(hostile)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(repo_lock, "_open_leaf", capture_leaf)
    monkeypatch.setattr(repo_lock.fcntl, "flock", fail_flock)
    monkeypatch.setattr(os, "close", fail_leaf_close)
    with pytest.raises(expected_type) as captured:
        repo_lock._open_and_lock("identity", blocking=False, pending=pending)
    _assert_constant_safe_failure(
        captured.value,
        expected_type,
        expected_text,
        hostile,
        str(tmp_path / "private-lock"),
    )
    assert pending.fd is None


def test_metadata_failure_cleanup_errors_preserve_operation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = f"hostile-metadata-cleanup:{tmp_path / 'private-lock'}"
    pending = repo_lock._PendingLock(os.getpid())
    original_open_leaf = repo_lock._open_leaf
    original_flock = repo_lock.fcntl.flock
    original_close = os.close
    leaf_fd: int | None = None

    def capture_leaf(root_fd: int, name: str) -> int:
        nonlocal leaf_fd
        leaf_fd = original_open_leaf(root_fd, name)
        return leaf_fd

    def fail_metadata(fd: int, length: int) -> None:
        raise OSError(hostile)

    def fail_cleanup_unlock(fd: int, operation: int) -> None:
        original_flock(fd, operation)
        if operation == repo_lock.fcntl.LOCK_UN:
            raise OSError(hostile)

    def fail_cleanup_close(fd: int) -> None:
        original_close(fd)
        if fd == leaf_fd:
            raise OSError(hostile)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(repo_lock, "_open_leaf", capture_leaf)
    monkeypatch.setattr(os, "ftruncate", fail_metadata)
    monkeypatch.setattr(repo_lock.fcntl, "flock", fail_cleanup_unlock)
    monkeypatch.setattr(os, "close", fail_cleanup_close)
    with pytest.raises(RepoLockOperationError) as captured:
        repo_lock._open_and_lock("identity", blocking=True, pending=pending)
    _assert_constant_safe_failure(
        captured.value,
        RepoLockOperationError,
        "repo_lock_operation_failed",
        hostile,
        str(tmp_path / "private-lock"),
    )
    assert pending.fd is None


def test_success_path_root_close_failure_is_normalized_and_cleans_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = f"hostile-root-cleanup:{tmp_path / 'private-root'}"
    pending = repo_lock._PendingLock(os.getpid())
    original_open_root = repo_lock._open_root
    original_open_leaf = repo_lock._open_leaf
    original_close = os.close
    root_fd: int | None = None
    leaf_fd: int | None = None

    def capture_root(root: Path) -> int:
        nonlocal root_fd
        root_fd = original_open_root(root)
        return root_fd

    def capture_leaf(open_root_fd: int, name: str) -> int:
        nonlocal leaf_fd
        leaf_fd = original_open_leaf(open_root_fd, name)
        return leaf_fd

    def fail_cleanup_close(fd: int) -> None:
        original_close(fd)
        if fd in (root_fd, leaf_fd):
            raise OSError(hostile)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(repo_lock, "_open_root", capture_root)
    monkeypatch.setattr(repo_lock, "_open_leaf", capture_leaf)
    monkeypatch.setattr(os, "close", fail_cleanup_close)
    with pytest.raises(RepoLockOperationError) as captured:
        repo_lock._open_and_lock("identity", blocking=True, pending=pending)
    _assert_constant_safe_failure(
        captured.value,
        RepoLockOperationError,
        "repo_lock_operation_failed",
        hostile,
        str(tmp_path / "private-root"),
    )
    assert pending.fd is None
    assert leaf_fd is not None
    with pytest.raises(OSError) as closed:
        os.fstat(leaf_fd)
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("failure_boundary", ["unlock", "close"])
def test_release_cleanup_failure_is_normalized_safe_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    repository = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    lock = RepoWriteLock(repository).acquire()
    key = lock._key
    fd = lock.fileno()
    hostile = f"hostile-release-{failure_boundary}:{tmp_path / 'private-lock'}"
    original_flock = repo_lock.fcntl.flock
    original_close = os.close

    def injected_flock(target_fd: int, operation: int) -> None:
        original_flock(target_fd, operation)
        if failure_boundary == "unlock" and operation == repo_lock.fcntl.LOCK_UN:
            raise OSError(hostile)

    def injected_close(target_fd: int) -> None:
        original_close(target_fd)
        if failure_boundary == "close" and target_fd == fd:
            raise OSError(hostile)

    monkeypatch.setattr(repo_lock.fcntl, "flock", injected_flock)
    monkeypatch.setattr(os, "close", injected_close)
    with pytest.raises(RepoLockOperationError) as captured:
        lock.release()
    _assert_constant_safe_failure(
        captured.value,
        RepoLockOperationError,
        "repo_lock_operation_failed",
        hostile,
        str(tmp_path / "private-lock"),
    )
    assert not lock.locked
    assert (lock._key, lock._fd, lock._pid, lock._depth) == (None, None, None, 0)
    assert key not in repo_lock._registry
    lock.release()


def test_context_cleanup_failure_does_not_mask_selected_typed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    lock = RepoWriteLock(repository)
    hostile = f"hostile-context-cleanup:{tmp_path / 'private-lock'}"
    original_flock = repo_lock.fcntl.flock

    def fail_unlock(fd: int, operation: int) -> None:
        original_flock(fd, operation)
        if operation == repo_lock.fcntl.LOCK_UN:
            raise OSError(hostile)

    with pytest.raises(RepoLockSecurityError) as captured:
        with lock:
            monkeypatch.setattr(repo_lock.fcntl, "flock", fail_unlock)
            raise RepoLockSecurityError("repo_lock_security_violation")
    _assert_constant_safe_failure(
        captured.value,
        RepoLockSecurityError,
        "repo_lock_security_violation",
        hostile,
        str(tmp_path / "private-lock"),
    )
    assert not lock.locked


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_during_pending_acquisition_closes_descriptor_before_parent_death(
    tmp_path: Path,
) -> None:
    repository = _make_repo(tmp_path / "repo")
    hermes_root = tmp_path / "hermes"
    code = textwrap.dedent(
        """
        import errno, os, signal, sys, threading
        import hermes_cli.repo_write_lock as repo_lock

        original_open_and_lock = repo_lock._open_and_lock
        leaf_locked = threading.Event()
        finish_acquire = threading.Event()
        captured_fd = None

        def paused_open_and_lock(*args, **kwargs):
            global captured_fd
            captured_fd = original_open_and_lock(*args, **kwargs)
            leaf_locked.set()
            finish_acquire.wait()
            return captured_fd

        repo_lock._open_and_lock = paused_open_and_lock
        lock = repo_lock.RepoWriteLock(sys.argv[1])
        worker = threading.Thread(target=lock.acquire)
        worker.start()
        assert leaf_locked.wait(timeout=10)

        status_read, status_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(status_read)
            try:
                os.fstat(captured_fd)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    marker = b"pending-fd-closed"
                else:
                    marker = b"pending-fd-error"
            else:
                marker = b"pending-fd-inherited"
            os.write(status_write, marker)
            os.close(status_write)
            while True:
                signal.pause()
        else:
            os.close(status_write)
            marker = os.read(status_read, 64).decode("ascii")
            os.close(status_read)
            finish_acquire.set()
            worker.join(timeout=10)
            assert not worker.is_alive()
            print(f"ready {os.getpid()} {child} {marker}", flush=True)
            signal.pause()
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(repository)],
        cwd=Path(__file__).parents[2],
        env=_lock_env(hermes_root),
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        assert process.stdout is not None
        marker, owner_text, child_text, fd_marker = process.stdout.readline().split()
        assert marker == "ready"
        assert int(owner_text) == process.pid
        child_pid = int(child_text)
        assert _contender(repository, hermes_root).returncode == 23

        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        os.kill(child_pid, 0)
        contender = _contender(repository, hermes_root)
        assert (fd_marker, contender.returncode, contender.stdout.strip()) == (
            "pending-fd-closed",
            0,
            "acquired",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_repo_guard_baseexception_acquire_unwinds_every_attempted_lock_and_reraises_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type,
) -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    repository = _make_repo(tmp_path / "guard-baseexception-repo")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    release_order: list[str] = []
    original = interrupt_type("original acquisition interruption")

    class FakeLock:
        def __init__(self, name: str, *, acquire_error=None, release_error=None):
            self.name = name
            self.identity = f"fake-{name}"
            self.acquire_error = acquire_error
            self.release_error = release_error
            self.held = False
            self.release_calls = 0

        def acquire(self) -> None:
            self.held = True
            if self.acquire_error is not None:
                raise self.acquire_error

        def release(self) -> None:
            self.release_calls += 1
            release_order.append(self.name)
            self.held = False
            if self.release_error is not None:
                raise self.release_error

    real_prior = RepoWriteLock(repository, blocking=False)
    fake_prior = FakeLock("prior")
    faulting = FakeLock(
        "faulting",
        acquire_error=original,
        release_error=KeyboardInterrupt("release must not mask original"),
    )
    never_attempted = FakeLock("never")
    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = (real_prior, fake_prior, faulting, never_attempted)  # type: ignore[assignment]
    guard._frames = []

    with pytest.raises(interrupt_type) as raised:
        guard.acquire()

    assert raised.value is original
    assert release_order == ["faulting", "prior"]
    assert not real_prior.locked
    assert not fake_prior.held
    assert not faulting.held
    assert not never_attempted.held
    assert fake_prior.release_calls == 1
    assert faulting.release_calls == 1
    assert never_attempted.release_calls == 0
    assert guard._frames == []
    with RepoWriteLock(repository, blocking=False):
        pass


def test_repo_guard_success_frame_releases_each_lock_exactly_once() -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    class FakeLock:
        identity = "fake"

        def __init__(self) -> None:
            self.acquire_calls = 0
            self.release_calls = 0

        def acquire(self) -> None:
            self.acquire_calls += 1

        def release(self) -> None:
            self.release_calls += 1

    first = FakeLock()
    second = FakeLock()
    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = (first, second)  # type: ignore[assignment]
    guard._frames = []

    guard.acquire()
    guard.release()
    guard.release()

    assert (first.acquire_calls, first.release_calls) == (1, 1)
    assert (second.acquire_calls, second.release_calls) == (1, 1)


@pytest.mark.parametrize(
    "interrupt_type", [KeyboardInterrupt, SystemExit, GeneratorExit]
)
@pytest.mark.parametrize("failure_index", [0, 1, 2], ids=["first", "middle", "last"])
def test_repo_guard_release_continues_through_control_flow_and_reraises_exact(
    interrupt_type,
    failure_index: int,
) -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    release_order: list[str] = []
    original = interrupt_type("original release interruption")

    class FakeLock:
        def __init__(self, name: str, release_error: BaseException | None = None):
            self.name = name
            self.release_error = release_error
            self.release_calls = 0

        def release(self) -> None:
            self.release_calls += 1
            release_order.append(self.name)
            if self.release_error is not None:
                raise self.release_error

    locks = [FakeLock(name) for name in ("first", "middle", "last")]
    locks[failure_index].release_error = original
    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = tuple(locks)  # type: ignore[assignment]
    guard._frames = [[
        repo_guard._FrameItem(
            lock=lock,  # type: ignore[arg-type]
            kernel_attempted=True,
            kernel_acquired=True,
        )
        for lock in locks
    ]]  # type: ignore[assignment]

    with pytest.raises(interrupt_type) as raised:
        guard.release()

    assert raised.value is original
    assert release_order == ["last", "middle", "first"]
    assert [lock.release_calls for lock in locks] == [1, 1, 1]
    assert guard._frames == []


@pytest.mark.parametrize(
    "control_first", [False, True], ids=["typed-first", "control-first"]
)
def test_repo_guard_release_control_flow_precedes_mapped_failure_after_cleanup(
    control_first: bool,
) -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    release_order: list[str] = []
    control_failure = KeyboardInterrupt("selected control failure")
    typed_source = RepoLockSecurityError("repo_lock_security_violation")

    class FakeLock:
        def __init__(self, name: str, release_error: BaseException | None = None):
            self.name = name
            self.release_error = release_error

        def release(self) -> None:
            release_order.append(self.name)
            if self.release_error is not None:
                raise self.release_error

    reverse_failures = (
        [control_failure, typed_source]
        if control_first
        else [typed_source, control_failure]
    )
    locks = [
        FakeLock("first", reverse_failures[1]),
        FakeLock("middle", reverse_failures[0]),
        FakeLock("last"),
    ]
    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = tuple(locks)  # type: ignore[assignment]
    guard._frames = [[
        repo_guard._FrameItem(
            lock=lock,  # type: ignore[arg-type]
            kernel_attempted=True,
            kernel_acquired=True,
        )
        for lock in locks
    ]]  # type: ignore[assignment]

    with pytest.raises(KeyboardInterrupt) as raised:
        guard.release()

    assert raised.value is control_failure
    assert release_order == ["last", "middle", "first"]
    assert guard._frames == []


def test_repo_guard_release_first_control_flow_in_reverse_order_wins() -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    first_encountered = SystemExit("first encountered")
    later_encountered = GeneratorExit("later encountered")

    class FakeLock:
        def __init__(self, release_error: BaseException | None = None):
            self.release_error = release_error
            self.release_calls = 0

        def release(self) -> None:
            self.release_calls += 1
            if self.release_error is not None:
                raise self.release_error

    locks = [FakeLock(later_encountered), FakeLock(), FakeLock(first_encountered)]
    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = tuple(locks)  # type: ignore[assignment]
    guard._frames = [[
        repo_guard._FrameItem(
            lock=lock,  # type: ignore[arg-type]
            kernel_attempted=True,
            kernel_acquired=True,
        )
        for lock in locks
    ]]  # type: ignore[assignment]

    with pytest.raises(SystemExit) as raised:
        guard.release()

    assert raised.value is first_encountered
    assert [lock.release_calls for lock in locks] == [1, 1, 1]
    assert guard._frames == []


def test_repo_guard_release_empty_frame_stack_is_noop() -> None:
    from hermes_cli.repo_write_guard import RepoWriteGuard

    guard = RepoWriteGuard.__new__(RepoWriteGuard)
    guard._locks = ()
    guard._frames = []

    guard.release()

    assert guard._frames == []


def _arm_one_shot_opcode_interrupt(
    function,
    error: BaseException,
    *,
    opname: str | None = None,
    argval: object | None = None,
    occurrence: int = 0,
    after: bool = True,
    target_offset: int | None = None,
) -> None:
    """Raise once immediately before, or immediately after, a real opcode."""
    instructions = list(dis.get_instructions(function))
    if target_offset is not None:
        target = next(item for item in instructions if item.offset == target_offset)
    elif opname is None:
        candidates = [item for item in instructions if item.opname != "RESUME"]
        target = candidates[0]
    else:
        candidates = [
            item
            for item in instructions
            if item.opname == opname and (argval is None or item.argval == argval)
        ]
        selected = candidates[occurrence]
        target = (
            instructions[instructions.index(selected) + 1]
            if after
            else selected
        )
    armed = True

    def trace(frame, event, _arg):
        nonlocal armed
        if frame.f_code is function.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target.offset and armed:
                armed = False
                sys.settrace(None)
                raise error
        return trace

    sys.settrace(trace)


def _opcode_after_named_call(function, name: str, occurrence: int = 0) -> int:
    instructions = list(dis.get_instructions(function))
    named = [
        index
        for index, item in enumerate(instructions)
        if item.opname in {"LOAD_GLOBAL", "LOAD_METHOD"} and item.argval == name
    ][occurrence]
    call = next(
        index
        for index in range(named + 1, len(instructions))
        if instructions[index].opname == "CALL"
    )
    return instructions[call + 1].offset


def _opcode_immediately_before_return(function, occurrence: int = -1) -> int:
    instructions = list(dis.get_instructions(function))
    returns = [
        index for index, item in enumerate(instructions) if item.opname == "RETURN_VALUE"
    ]
    return instructions[returns[occurrence] - 1].offset


@pytest.mark.parametrize(
    "boundary",
    ["pending-publication", "open-return", "held-transition", "attach", "return"],
)
def test_acquire_transaction_opcode_interrupt_unwinds_exact_new_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    repository = _make_repo(tmp_path / f"opcode-{boundary}")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    original_open_and_lock = repo_lock._open_and_lock
    captured_pending: list[repo_lock._PendingLock] = []
    captured_fds: list[int] = []

    def capture_open_and_lock(
        identity: str, *, blocking: bool, pending: repo_lock._PendingLock
    ) -> int:
        fd = original_open_and_lock(identity, blocking=blocking, pending=pending)
        captured_pending.append(pending)
        captured_fds.append(fd)
        return fd

    monkeypatch.setattr(repo_lock, "_open_and_lock", capture_open_and_lock)
    lock = RepoWriteLock(repository, blocking=False)
    key = (os.fspath(repo_lock._lock_root()), lock.identity)
    interruption = KeyboardInterrupt(f"opcode-{boundary}")

    if boundary == "pending-publication":
        _arm_one_shot_opcode_interrupt(
            RepoWriteLock._acquire, interruption, opname="STORE_SUBSCR", occurrence=0
        )
    elif boundary == "open-return":
        _arm_one_shot_opcode_interrupt(
            RepoWriteLock._acquire,
            interruption,
            target_offset=_opcode_after_named_call(
                RepoWriteLock._acquire, "_open_and_lock"
            ),
        )
    elif boundary == "held-transition":
        _arm_one_shot_opcode_interrupt(
            RepoWriteLock._acquire, interruption, opname="STORE_SUBSCR", occurrence=1
        )
    elif boundary == "attach":
        _arm_one_shot_opcode_interrupt(
            RepoWriteLock._acquire,
            interruption,
            target_offset=_opcode_after_named_call(
                RepoWriteLock._acquire, "_attach", occurrence=1
            ),
        )
    else:
        _arm_one_shot_opcode_interrupt(
            RepoWriteLock._acquire,
            interruption,
            target_offset=_opcode_immediately_before_return(RepoWriteLock._acquire),
        )

    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            lock.acquire()
    finally:
        sys.settrace(None)

    assert raised.value is interruption
    assert not lock.locked
    assert (lock._key, lock._fd, lock._pid, lock._depth) == (None, None, None, 0)
    assert key not in repo_lock._registry
    for pending in captured_pending:
        assert pending.fd is None
    for fd in captured_fds:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF

    with RepoWriteLock(repository, blocking=False):
        pass
    contender = _contender(repository, hermes_root)
    assert (contender.returncode, contender.stdout.strip()) == (0, "acquired")


def test_reentrant_second_handle_return_interrupt_preserves_original_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _make_repo(tmp_path / "reentrant-return-interrupt")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    owner = RepoWriteLock(repository, blocking=False).acquire()
    second = RepoWriteLock(repository, blocking=False)
    key = owner._key
    assert key is not None
    held = repo_lock._registry[key]
    assert isinstance(held, repo_lock._HeldLock)
    assert held.references == 1
    interruption = SystemExit("reentrant-return-interruption")
    _arm_one_shot_opcode_interrupt(
        RepoWriteLock._acquire,
        interruption,
        target_offset=_opcode_immediately_before_return(
            RepoWriteLock._acquire, occurrence=1
        ),
    )

    try:
        with pytest.raises(SystemExit) as raised:
            second.acquire()
    finally:
        sys.settrace(None)

    try:
        assert raised.value is interruption
        assert not second.locked
        assert (second._key, second._fd, second._pid, second._depth) == (
            None,
            None,
            None,
            0,
        )
        assert repo_lock._registry[key] is held
        assert held.references == 1
        assert owner.locked
        assert _contender(repository, hermes_root).returncode == 23
        with RepoWriteLock(repository, blocking=False):
            assert held.references == 2
        assert held.references == 1
        assert _contender(repository, hermes_root).returncode == 23
    finally:
        if second.locked:
            second.release()
        if owner.locked:
            owner.release()

    assert key not in repo_lock._registry
    contender = _contender(repository, hermes_root)
    assert (contender.returncode, contender.stdout.strip()) == (0, "acquired")


def test_lane_reservation_opcode_interrupt_after_registry_insert_unwinds_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = "reserve-opcode-interrupt"
    original = KeyboardInterrupt("reserve opcode interruption")

    class FakeLock:
        def __init__(self) -> None:
            self.identity = identity
            self.release_calls = 0

        def acquire(self) -> None:
            raise AssertionError("kernel acquire reached after reserve interruption")

        def release(self) -> None:
            self.release_calls += 1

    lock = FakeLock()
    guard = repo_guard.RepoWriteGuard.__new__(repo_guard.RepoWriteGuard)
    guard._locks = (lock,)  # type: ignore[assignment]
    guard._frames = []
    captured: list[repo_guard._FrameItem] = []
    captured_lanes: list[repo_guard._Lane | None] = []
    original_reserve = repo_guard._reserve_lane

    def capture_reserve(selected_identity, item):
        captured.append(item)
        try:
            original_reserve(selected_identity, item)
        finally:
            captured_lanes.append(item.lane)

    monkeypatch.setattr(repo_guard, "_reserve_lane", capture_reserve)
    _arm_one_shot_opcode_interrupt(
        original_reserve,
        original,
        opname="STORE_SUBSCR",
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        guard.acquire()

    assert raised.value is original
    lane = captured_lanes[0]
    assert lane is not None
    assert (lane.owner_thread_id, lane.depth, lane.users) == (None, 0, 0)
    assert identity not in repo_guard._lane_registry
    assert guard._frames == []
    assert lock.release_calls == 0


def test_lane_claim_opcode_interrupt_after_owner_mutation_unwinds_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = "claim-opcode-interrupt"
    original = SystemExit("claim opcode interruption")

    class FakeLock:
        def __init__(self) -> None:
            self.identity = identity

        def acquire(self) -> None:
            raise AssertionError("kernel acquire reached after claim interruption")

        def release(self) -> None:
            raise AssertionError("kernel release reached before kernel attempt")

    guard = repo_guard.RepoWriteGuard.__new__(repo_guard.RepoWriteGuard)
    guard._locks = (FakeLock(),)  # type: ignore[assignment]
    guard._frames = []
    captured: list[repo_guard._FrameItem] = []
    captured_lanes: list[repo_guard._Lane | None] = []
    original_reserve = repo_guard._reserve_lane

    def capture_reserve(selected_identity, item):
        captured.append(item)
        try:
            original_reserve(selected_identity, item)
        finally:
            captured_lanes.append(item.lane)

    monkeypatch.setattr(repo_guard, "_reserve_lane", capture_reserve)
    _arm_one_shot_opcode_interrupt(
        repo_guard._recompute_lane_owner,
        original,
        opname="STORE_ATTR",
        argval="owner_thread_id",
    )

    with pytest.raises(SystemExit) as raised:
        guard.acquire()

    assert raised.value is original
    lane = captured_lanes[0]
    assert lane is not None
    assert (lane.owner_thread_id, lane.depth, lane.users) == (None, 0, 0)
    assert identity not in repo_guard._lane_registry
    assert guard._frames == []


def test_lane_release_control_after_claim_discard_retries_and_prunes_exact() -> None:
    identity = "lane-release-opcode-interrupt"
    original = GeneratorExit("lane release opcode interruption")

    class FakeLock:
        def __init__(self) -> None:
            self.identity = identity
            self.held = False
            self.release_calls = 0

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.release_calls += 1
            self.held = False

    lock = FakeLock()
    guard = repo_guard.RepoWriteGuard.__new__(repo_guard.RepoWriteGuard)
    guard._locks = (lock,)  # type: ignore[assignment]
    guard._frames = []
    guard.acquire()
    lane = repo_guard._lane_registry[identity]
    _arm_one_shot_opcode_interrupt(repo_guard._recompute_lane_owner, original)

    with pytest.raises(GeneratorExit) as raised:
        guard.release()

    assert raised.value is original
    assert (lane.owner_thread_id, lane.depth, lane.users) == (None, 0, 0)
    assert identity not in repo_guard._lane_registry
    assert guard._frames == []
    assert lock.release_calls == 1


def test_actual_kernel_release_entry_control_retries_unlock_and_reraises_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _make_repo(tmp_path / "kernel-release-entry")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    guard = repo_guard.RepoWriteGuard([repository / "target.txt"])
    guard.acquire()
    lock = guard._locks[0]
    original = KeyboardInterrupt("kernel release entry interruption")
    _arm_one_shot_opcode_interrupt(RepoWriteLock.release, original)

    with pytest.raises(KeyboardInterrupt) as raised:
        guard.release()

    assert raised.value is original
    assert not lock.locked
    assert _contender(repository, hermes_root).returncode == 0
    assert guard._frames == []


def test_actual_kernel_release_post_call_control_does_not_double_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _make_repo(tmp_path / "kernel-release-post-call")
    hermes_root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_root))
    guard = repo_guard.RepoWriteGuard([repository / "target.txt"])
    guard.acquire()
    lock = guard._locks[0]
    original = SystemExit("kernel release completed interruption")
    _arm_one_shot_opcode_interrupt(
        RepoWriteLock.release,
        original,
        opname="CALL",
        occurrence=-1,
    )

    with pytest.raises(SystemExit) as raised:
        guard.release()

    assert raised.value is original
    assert not lock.locked
    assert _contender(repository, hermes_root).returncode == 0
    assert guard._frames == []


def test_acquire_ordinary_failure_yields_to_cleanup_control_after_full_unwind() -> None:
    identity = "acquire-cleanup-control-precedence"
    original = RepoLockSecurityError("repo_lock_security_violation")
    cleanup_control = GeneratorExit("cleanup control precedence")

    class FakeLock:
        def __init__(self) -> None:
            self.identity = identity
            self.held = False
            self.release_calls = 0

        def acquire(self) -> None:
            self.held = True
            raise original

        def release(self) -> None:
            self.release_calls += 1
            if self.release_calls == 1:
                raise cleanup_control
            self.held = False

    lock = FakeLock()
    guard = repo_guard.RepoWriteGuard.__new__(repo_guard.RepoWriteGuard)
    guard._locks = (lock,)  # type: ignore[assignment]
    guard._frames = []

    with pytest.raises(GeneratorExit) as raised:
        guard.acquire()

    assert raised.value is cleanup_control
    assert lock.release_calls == 2
    assert not lock.held
    assert identity not in repo_guard._lane_registry
    assert guard._frames == []
