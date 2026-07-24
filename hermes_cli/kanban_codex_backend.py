"""Hardened direct Codex execution adapter for managed Kanban attempts.

The adapter owns only the volatile host boundary: exact Codex argv/environment,
a one-stage pre-exec release bootstrap, bounded startup parsing, and dedicated
POSIX process-session observation/cleanup.  Kanban lifecycle and persistence
remain outside this module.
"""

from __future__ import annotations

import json
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import psutil

from hermes_cli.kanban_execution import (
    PILOT_BACKEND,
    ApprovedExecutionSpec,
    ExecutionHandle,
    ExecutionObservation,
    ExecutionState,
    TerminationObservation,
    TerminationState,
)
from tools.process_registry import capture_host_identity, terminate_host_identity


STARTUP_TIMEOUT_SECONDS = 5.0
_STARTUP_OUTPUT_LIMIT = 8192
_PREPARE_TIMEOUT_SECONDS = 5.0
_TREE_TERM_GRACE_SECONDS = 0.5
_GROUP_TERM_GRACE_SECONDS = 0.5
_GROUP_KILL_GRACE_SECONDS = 0.75
_ZERO_READBACK_SECONDS = 0.5
_EXPECTED_STARTUP_LINE = b"sandbox: workspace-write [workdir]"
_RELEASE_BYTES = b"GO\n"
_UNKNOWN_EXIT_CODE = -1
_NATIVE_MAGICS = {
    b"\x7fELF",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}

_BOOTSTRAP = r"""
import json
import os
import sys


def start_identity(pid):
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8") as stream:
            return int(stream.read().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass
    try:
        import psutil
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def main():
    release_fd = int(sys.argv[1])
    status_fd = int(sys.argv[2])
    pinned_path = sys.argv[3]
    expected_identity = json.loads(sys.argv[4])
    target = sys.argv[5:]
    info = os.lstat(pinned_path)
    actual_identity = [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
    ]
    if actual_identity != expected_identity or os.path.islink(pinned_path):
        return 70
    pid = os.getpid()
    report = {
        "kernel_start_time": start_identity(pid),
        "pgid": os.getpgrp(),
        "pid": pid,
    }
    os.write(
        status_fd,
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"),
    )
    os.close(status_fd)

    release = b""
    while True:
        chunk = os.read(release_fd, 4)
        if not chunk:
            break
        release += chunk
        if len(release) > len(b"GO\n"):
            break
    os.close(release_fd)
    if release != b"GO\n":
        return 0
    info = os.lstat(pinned_path)
    actual_identity = [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
    ]
    if actual_identity != expected_identity or os.path.islink(pinned_path):
        return 70
    os.execve(pinned_path, target, os.environ)
    return 70


try:
    _exit_code = main()
except BaseException:
    os._exit(70)
raise SystemExit(_exit_code)
"""


@dataclass(frozen=True)
class _ExecutableIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _PinnedExecutable:
    path: Path
    directory: Path
    identity: _ExecutableIdentity


@dataclass
class _Attempt:
    process: subprocess.Popen
    release_fd: int | None
    worktree: Path
    pinned_executable: _PinnedExecutable
    condition: threading.Condition = field(default_factory=threading.Condition)
    output: bytearray = field(default_factory=bytearray)
    output_overflow: bool = False
    output_eof: bool = False
    released: bool = False
    startup_valid: bool = False
    reader: threading.Thread | None = None


class CodexDirectExecutionBackend:
    """One fixed, absolute Codex executable behind the execution Port."""

    kind = PILOT_BACKEND

    def __init__(self, executable: Path):
        try:
            path, identity = self._validate_executable(executable)
        except Exception:
            raise ValueError("invalid Codex executable") from None
        self._executable = path
        self._executable_identity = identity
        self._attempts: dict[ExecutionHandle, _Attempt] = {}
        self._attempts_lock = threading.Lock()

    @staticmethod
    def _validate_executable(executable: Path) -> tuple[Path, _ExecutableIdentity]:
        if not isinstance(executable, Path):
            raise ValueError
        if not executable.is_absolute() or executable.is_symlink():
            raise ValueError
        resolved = executable.resolve(strict=True)
        if resolved != executable:
            raise ValueError
        info = executable.stat()
        if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
            raise ValueError
        identity = _ExecutableIdentity(
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
        )
        return executable, identity

    def _revalidate_executable(self) -> None:
        path, identity = self._validate_executable(self._executable)
        if path != self._executable or identity != self._executable_identity:
            raise ValueError

    def _open_pinned_executable(self) -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise ValueError
        fd = os.open(self._executable, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            identity = _ExecutableIdentity(
                device=info.st_dev,
                inode=info.st_ino,
                mode=info.st_mode,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
            )
            if identity != self._executable_identity:
                raise ValueError
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _validate_worktree(spec: ApprovedExecutionSpec, worktree: Path) -> Path:
        if type(spec) is not ApprovedExecutionSpec or not isinstance(worktree, Path):
            raise ValueError
        if not worktree.is_absolute() or worktree.is_symlink() or not worktree.is_dir():
            raise ValueError
        if worktree.resolve(strict=True) != worktree:
            raise ValueError
        if worktree != Path(spec.worktree_path):
            raise ValueError
        if "\x00" in spec.instructions:
            raise ValueError
        return worktree

    @staticmethod
    def _private_dir(path: Path, worktree: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path:
            raise ValueError
        if not path.is_relative_to(worktree):
            raise ValueError
        path.chmod(0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise ValueError
        return path

    def _stage_pinned_executable(
        self,
        source_fd: int,
        worktree: Path,
    ) -> _PinnedExecutable:
        os.lseek(source_fd, 0, os.SEEK_SET)
        magic = os.read(source_fd, 4)
        if magic not in _NATIVE_MAGICS and not magic.startswith(b"MZ"):
            raise ValueError
        os.lseek(source_fd, 0, os.SEEK_SET)

        base = self._private_dir(worktree / ".hermes-codex", worktree)
        pins = self._private_dir(base / "pins", worktree)
        directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=pins))
        path = directory / "codex"
        output_fd: int | None = None
        try:
            output_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
            copied = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    if written <= 0:
                        raise OSError
                    copied += written
                    view = view[written:]
            if copied != self._executable_identity.size:
                raise ValueError
            os.fchmod(output_fd, 0o500)
            os.fsync(output_fd)
            info = os.fstat(output_fd)
            identity = _ExecutableIdentity(
                device=info.st_dev,
                inode=info.st_ino,
                mode=info.st_mode,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
            )
            os.close(output_fd)
            output_fd = None
            directory.chmod(0o500)
            return _PinnedExecutable(path=path, directory=directory, identity=identity)
        except Exception:
            if output_fd is not None:
                try:
                    os.close(output_fd)
                except OSError:
                    pass
            self._remove_pinned_path(path, directory)
            raise

    @staticmethod
    def _remove_pinned_path(path: Path, directory: Path) -> None:
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            directory.rmdir()
        except OSError:
            pass

    @classmethod
    def _remove_pinned_executable(cls, pinned: _PinnedExecutable) -> None:
        cls._remove_pinned_path(pinned.path, pinned.directory)

    @classmethod
    def _closed_environment(cls, worktree: Path) -> dict[str, str]:
        base = cls._private_dir(worktree / ".hermes-codex", worktree)
        temp = cls._private_dir(base / "tmp", worktree)
        cache = cls._private_dir(base / "cache", worktree)
        config = cls._private_dir(base / "config", worktree)
        data = cls._private_dir(base / "data", worktree)
        return {
            "HOME": os.environ.get("HOME") or str(Path.home()),
            "LANG": os.environ.get("LANG") or "C.UTF-8",
            "NO_COLOR": "1",
            "PATH": os.environ.get("PATH") or os.defpath,
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
            "XDG_CACHE_HOME": str(cache),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
        }

    def _target_argv(self, spec: ApprovedExecutionSpec, worktree: Path) -> list[str]:
        return [
            str(self._executable),
            "--strict-config",
            "-a",
            "never",
            "-s",
            "workspace-write",
            "-C",
            str(worktree),
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            spec.instructions,
        ]

    @staticmethod
    def _read_status_report(fd: int) -> dict[str, int]:
        deadline = time.monotonic() + _PREPARE_TIMEOUT_SECONDS
        raw = bytearray()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not readable:
                break
            chunk = os.read(fd, 513 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 512:
                raise ValueError
            if b"\n" in raw:
                break
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ValueError
        payload = json.loads(bytes(raw[:-1]).decode("ascii"))
        if type(payload) is not dict or set(payload) != {"kernel_start_time", "pgid", "pid"}:
            raise ValueError
        if any(type(payload[key]) is not int or payload[key] <= 0 for key in payload):
            raise ValueError
        return {key: int(payload[key]) for key in ("kernel_start_time", "pgid", "pid")}

    @staticmethod
    def _read_output(attempt: _Attempt, stream: BinaryIO) -> None:
        try:
            raw_reader = getattr(stream, "read1", None)
            while True:
                chunk = raw_reader(4096) if raw_reader is not None else stream.read(4096)
                if not chunk:
                    break
                with attempt.condition:
                    remaining = _STARTUP_OUTPUT_LIMIT - len(attempt.output)
                    if remaining > 0:
                        attempt.output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        attempt.output_overflow = True
                    attempt.condition.notify_all()
        except Exception:
            pass
        finally:
            with attempt.condition:
                attempt.output_eof = True
                attempt.condition.notify_all()

    @staticmethod
    def _close_release(attempt: _Attempt) -> None:
        with attempt.condition:
            fd = attempt.release_fd
            attempt.release_fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    @staticmethod
    def _hard_cleanup_process(process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass

    def prepare(self, spec: ApprovedExecutionSpec, worktree: Path) -> ExecutionHandle:
        process: subprocess.Popen | None = None
        executable_fd: int | None = None
        pinned_executable: _PinnedExecutable | None = None
        release_w: int | None = None
        status_r: int | None = None
        try:
            if os.name != "posix":
                raise ValueError
            self._revalidate_executable()
            executable_fd = self._open_pinned_executable()
            worktree = self._validate_worktree(spec, worktree)
            pinned_executable = self._stage_pinned_executable(executable_fd, worktree)
            environment = self._closed_environment(worktree)
            target_argv = self._target_argv(spec, worktree)

            release_r, release_w = os.pipe()
            status_r, status_w = os.pipe()
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _BOOTSTRAP,
                        str(release_r),
                        str(status_w),
                        str(pinned_executable.path),
                        json.dumps(
                            [
                                pinned_executable.identity.device,
                                pinned_executable.identity.inode,
                                pinned_executable.identity.mode,
                                pinned_executable.identity.size,
                                pinned_executable.identity.mtime_ns,
                            ],
                            separators=(",", ":"),
                        ),
                        *target_argv,
                    ],
                    cwd=str(worktree),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    pass_fds=(release_r, status_w),
                    start_new_session=True,
                    shell=False,
                )
            finally:
                os.close(release_r)
                os.close(status_w)
                if executable_fd is not None:
                    os.close(executable_fd)
                    executable_fd = None

            if process.stdout is None:
                raise ValueError
            attempt = _Attempt(
                process=process,
                release_fd=release_w,
                worktree=worktree,
                pinned_executable=pinned_executable,
            )
            reader = threading.Thread(
                target=self._read_output,
                args=(attempt, process.stdout),
                daemon=True,
                name=f"codex-direct-output-{process.pid}",
            )
            attempt.reader = reader
            reader.start()

            report = self._read_status_report(status_r)
            os.close(status_r)
            status_r = None
            kernel_start = capture_host_identity(process.pid)
            if (
                kernel_start is None
                or report["pid"] != process.pid
                or report["pgid"] != os.getpgid(process.pid)
                or report["pgid"] != process.pid
                or report["kernel_start_time"] != kernel_start
            ):
                raise ValueError
            handle = ExecutionHandle(
                backend=self.kind,
                pid=process.pid,
                pgid=int(report["pgid"]),
                kernel_start_time=kernel_start,
            )
            with self._attempts_lock:
                self._attempts[handle] = attempt
            release_w = None
            return handle
        except Exception:
            if executable_fd is not None:
                try:
                    os.close(executable_fd)
                except OSError:
                    pass
            if status_r is not None:
                try:
                    os.close(status_r)
                except OSError:
                    pass
            if release_w is not None:
                try:
                    os.close(release_w)
                except OSError:
                    pass
            if process is not None:
                self._hard_cleanup_process(process)
            if pinned_executable is not None:
                self._remove_pinned_executable(pinned_executable)
            raise RuntimeError("Codex prepare failed") from None

    def release(self, handle: ExecutionHandle) -> None:
        try:
            with self._attempts_lock:
                attempt = self._attempts.get(handle)
            if attempt is None:
                raise ValueError
            with attempt.condition:
                if attempt.released:
                    if attempt.startup_valid:
                        return
                    raise ValueError
                fd = attempt.release_fd
                if fd is None:
                    raise ValueError
                written = os.write(fd, _RELEASE_BYTES)
                if written != len(_RELEASE_BYTES):
                    raise ValueError
                self._close_release(attempt)
                attempt.released = True

                deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
                while True:
                    lines = bytes(attempt.output).splitlines()
                    if attempt.output_overflow:
                        raise ValueError
                    sandbox_lines = [line for line in lines if line.startswith(b"sandbox:")]
                    if any(line != _EXPECTED_STARTUP_LINE for line in sandbox_lines):
                        raise ValueError
                    if len(sandbox_lines) > 1:
                        raise ValueError
                    if sandbox_lines == [_EXPECTED_STARTUP_LINE]:
                        attempt.startup_valid = True
                        return
                    if attempt.process.poll() is not None:
                        raise ValueError
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError
                    attempt.condition.wait(timeout=min(remaining, 0.1))
        except Exception:
            self.terminate(handle)
            raise RuntimeError("Codex release failed") from None

    def observe(self, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            if type(handle) is not ExecutionHandle or handle.backend != self.kind:
                raise ValueError
            with self._attempts_lock:
                attempt = self._attempts.get(handle)
            if attempt is not None:
                exit_code = attempt.process.poll()
                if exit_code is not None:
                    return ExecutionObservation(ExecutionState.EXITED, int(exit_code))

            actual_start = capture_host_identity(handle.pid)
            if actual_start is not None:
                if actual_start != handle.kernel_start_time:
                    return ExecutionObservation(ExecutionState.IDENTITY_MISMATCH, None)
                return ExecutionObservation(ExecutionState.RUNNING, None)
            if psutil.pid_exists(handle.pid):
                return ExecutionObservation(ExecutionState.IDENTITY_MISMATCH, None)
            return ExecutionObservation(ExecutionState.EXITED, _UNKNOWN_EXIT_CODE)
        except Exception:
            return ExecutionObservation(ExecutionState.IDENTITY_MISMATCH, None)

    @staticmethod
    def _group_members(pgid: int) -> tuple[list[psutil.Process], bool]:
        members: list[psutil.Process] = []
        uncertain = False
        for process in psutil.process_iter(["pid"]):
            try:
                if os.getpgid(process.pid) != pgid:
                    continue
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (PermissionError, psutil.AccessDenied, OSError):
                uncertain = True
                continue
            try:
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
                members.append(process)
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (psutil.AccessDenied, OSError):
                uncertain = True
        return members, uncertain

    @staticmethod
    def _verify_members(
        handle: ExecutionHandle, members: list[psutil.Process], uncertain: bool
    ) -> bool:
        if uncertain:
            return False
        for process in members:
            start = capture_host_identity(process.pid)
            if start is None:
                if psutil.pid_exists(process.pid):
                    return False
                continue
            if process.pid == handle.pid:
                if start != handle.kernel_start_time:
                    return False
            elif start < handle.kernel_start_time:
                return False
            try:
                if os.getsid(process.pid) != handle.pgid:
                    return False
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (psutil.AccessDenied, OSError):
                return False
        return True

    @classmethod
    def _verified_group(cls, handle: ExecutionHandle) -> list[psutil.Process] | None:
        members, uncertain = cls._group_members(handle.pgid)
        if not cls._verify_members(handle, members, uncertain):
            return None
        return members

    @classmethod
    def _wait_group(
        cls, handle: ExecutionHandle, timeout: float
    ) -> tuple[list[psutil.Process] | None, bool]:
        deadline = time.monotonic() + timeout
        while True:
            members = cls._verified_group(handle)
            if members is None:
                return None, False
            if not members:
                return [], True
            if time.monotonic() >= deadline:
                return members, False
            time.sleep(0.025)

    @classmethod
    def _zero_member_readback(cls, handle: ExecutionHandle) -> TerminationState:
        deadline = time.monotonic() + _ZERO_READBACK_SECONDS
        consecutive_empty = 0
        while time.monotonic() < deadline:
            members = cls._verified_group(handle)
            if members is None:
                return TerminationState.IDENTITY_MISMATCH
            if members:
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    return TerminationState.DEAD
            time.sleep(0.025)
        return TerminationState.SURVIVOR

    def _finalize_termination(
        self,
        handle: ExecutionHandle,
        state: TerminationState,
    ) -> TerminationObservation:
        if state is not TerminationState.DEAD:
            return TerminationObservation(state)
        with self._attempts_lock:
            attempt = self._attempts.pop(handle, None)
        if attempt is not None:
            self._close_release(attempt)
            reader = attempt.reader
            if reader is not None and reader.is_alive():
                reader.join(timeout=0.2)
            if not (reader is not None and reader.is_alive()) and attempt.process.stdout is not None:
                try:
                    attempt.process.stdout.close()
                except OSError:
                    pass
            self._remove_pinned_executable(attempt.pinned_executable)
        return TerminationObservation(TerminationState.DEAD)

    def terminate(self, handle: ExecutionHandle) -> TerminationObservation:
        try:
            if type(handle) is not ExecutionHandle or handle.backend != self.kind:
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
            with self._attempts_lock:
                attempt = self._attempts.get(handle)
            if attempt is not None:
                self._close_release(attempt)

            root_start = capture_host_identity(handle.pid)
            if root_start is not None and root_start != handle.kernel_start_time:
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
            if root_start is None and psutil.pid_exists(handle.pid):
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)

            members = self._verified_group(handle)
            if members is None:
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
            if not members:
                if attempt is not None:
                    attempt.process.poll()
                return self._finalize_termination(
                    handle,
                    self._zero_member_readback(handle),
                )

            if root_start == handle.kernel_start_time:
                accepted = terminate_host_identity(
                    handle.pid,
                    handle.kernel_start_time,
                    grace_seconds=_TREE_TERM_GRACE_SECONDS,
                )
                if not accepted:
                    current = capture_host_identity(handle.pid)
                    if current is not None and current != handle.kernel_start_time:
                        return TerminationObservation(TerminationState.IDENTITY_MISMATCH)

            members = self._verified_group(handle)
            if members is None:
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
            for process in sorted(members, key=lambda item: item.pid == handle.pid):
                try:
                    process.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    pass

            members, empty = self._wait_group(handle, _GROUP_TERM_GRACE_SECONDS)
            if members is None:
                return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
            if not empty:
                for process in sorted(members, key=lambda item: item.pid == handle.pid):
                    try:
                        process.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        pass
                members, empty = self._wait_group(handle, _GROUP_KILL_GRACE_SECONDS)
                if members is None:
                    return TerminationObservation(TerminationState.IDENTITY_MISMATCH)
                if not empty:
                    return TerminationObservation(TerminationState.SURVIVOR)

            if attempt is not None:
                try:
                    attempt.process.wait(timeout=0.2)
                except Exception:
                    pass
            return self._finalize_termination(
                handle,
                self._zero_member_readback(handle),
            )
        except Exception:
            return TerminationObservation(TerminationState.SURVIVOR)
