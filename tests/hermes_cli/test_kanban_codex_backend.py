"""Fake-executable contracts for the hardened Codex direct backend.

These tests intentionally never resolve or invoke a real ``codex`` binary.
They exercise only the host adapter, its one-stage bootstrap, and dedicated
POSIX process-session cleanup.
"""

from __future__ import annotations

import functools
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli.kanban_execution import (
    APPROVED_SPEC_SCHEMA,
    PILOT_ASSIGNEE,
    PILOT_BACKEND,
    PILOT_BOARD,
    PILOT_TIMEOUT_SECONDS,
    ApprovedExecutionSpec,
    ExecutionHandle,
    ExecutionState,
    TerminationState,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="dedicated POSIX sessions")

_FAKE_SOURCE = r'''#!__PYTHON__
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

reported_argv = sys.argv[1:]
args = reported_argv[1:]
worktree = Path(args[args.index("-C") + 1])
mode = args[-1]
(worktree / "fake-argv.json").write_text(json.dumps(reported_argv, ensure_ascii=False))
(worktree / "fake-env.json").write_text(json.dumps(dict(os.environ), sort_keys=True))
(worktree / "fake-started.json").write_text(json.dumps({
    "pid": os.getpid(),
    "pgid": os.getpgrp(),
}))

if mode == "bad-scope":
    print("sandbox: workspace-write [workdir, /tmp]", flush=True)
elif mode == "overflow":
    print("x" * 20000, flush=True)
elif mode != "silent":
    print("sandbox: workspace-write [workdir]", flush=True)

if mode in {"hold", "root-exit"}:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (worktree / "fake-child.pid").write_text(str(child.pid))
    if mode == "root-exit":
        raise SystemExit(0)
    while True:
        time.sleep(1)
elif mode in {"bad-scope", "overflow", "silent"}:
    while True:
        time.sleep(1)
elif mode == "exit-seven":
    raise SystemExit(7)
'''

_EXPECTED_PREFIX = [
    "--strict-config",
    "-a",
    "never",
    "-s",
    "workspace-write",
    "-C",
    None,
    "-c",
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "-c",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
]


def _api():
    # Deliberately imported at test execution time so the first TDD run collects
    # successfully and fails on the missing adapter rather than at collection.
    from hermes_cli.kanban_codex_backend import CodexDirectExecutionBackend

    return CodexDirectExecutionBackend


@functools.lru_cache(maxsize=2)
def _native_fake_bytes(replacement_marker: bool) -> bytes:
    source = _FAKE_SOURCE.replace("__PYTHON__", sys.executable)
    if replacement_marker:
        source = source.replace(
            'worktree = Path(args[args.index("-C") + 1])',
            'worktree = Path(args[args.index("-C") + 1])\n'
            '(worktree / "replacement-ran").write_text("yes")',
            1,
        )
    launcher_source = f"""
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {{
    char **next = calloc((size_t)argc + 4, sizeof(char *));
    if (next == NULL) return 70;
    next[0] = {json.dumps(sys.executable)};
    next[1] = "-c";
    next[2] = {json.dumps(source)};
    for (int i = 0; i < argc; ++i) next[i + 3] = argv[i];
    next[argc + 3] = NULL;
    execv(next[0], next);
    return 70;
}}
"""
    with tempfile.TemporaryDirectory(prefix="codex-native-fake-") as temp_dir:
        source_path = Path(temp_dir) / "fake.c"
        output_path = Path(temp_dir) / "fake"
        source_path.write_text(launcher_source)
        subprocess.run(
            ["cc", "-O2", "-o", str(output_path), str(source_path)],
            check=True,
            capture_output=True,
        )
        return output_path.read_bytes()


def _write_fake(path: Path, *, replacement_marker: bool = False) -> Path:
    path.write_bytes(_native_fake_bytes(replacement_marker))
    path.chmod(0o700)
    return path


def _spec(tmp_path: Path, *, instructions: str = "exit-zero") -> tuple[ApprovedExecutionSpec, Path]:
    repository = tmp_path / "repo"
    worktree = repository / ".worktrees" / "t_abcd1234"
    worktree.mkdir(parents=True)
    spec = ApprovedExecutionSpec(
        assignee=PILOT_ASSIGNEE,
        backend=PILOT_BACKEND,
        board=PILOT_BOARD,
        branch_name="codex/t_abcd1234",
        instructions=instructions,
        max_runtime_seconds=PILOT_TIMEOUT_SECONDS,
        project_id="fake-project",
        repository_root=str(repository),
        schema=APPROVED_SPEC_SCHEMA,
        task_id="t_abcd1234",
        title="Fake-only adapter test",
        worktree_path=str(worktree),
    )
    return spec, worktree


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for test marker {path.name}")


def _wait_observation(backend, handle: ExecutionHandle, state: ExecutionState, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observation = backend.observe(handle)
        if observation.state is state:
            return observation
        time.sleep(0.02)
    raise AssertionError(f"did not observe {state.value}")


def _non_zombie_group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(process.pid) == pgid:
                members.append(process.pid)
        except (ProcessLookupError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return members


def _force_cleanup(backend, handle: ExecutionHandle | None) -> None:
    if handle is None:
        return
    try:
        backend.terminate(handle)
    except Exception:
        try:
            os.killpg(handle.pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_constructor_requires_absolute_canonical_regular_executable(tmp_path: Path) -> None:
    backend_type = _api()
    valid = _write_fake(tmp_path / "fake-codex")
    directory = tmp_path / "directory"
    directory.mkdir()
    nonexec = tmp_path / "nonexec"
    nonexec.write_text("no")
    symlink = tmp_path / "fake-link"
    symlink.symlink_to(valid)

    invalid = [Path("fake-codex"), tmp_path / "missing", directory, nonexec, symlink]
    for path in invalid:
        with pytest.raises(ValueError, match=r"^invalid Codex executable$"):
            backend_type(path)

    backend_type(valid)


def test_prepare_rejects_script_wrapper_instead_of_native_binary(tmp_path: Path) -> None:
    backend_type = _api()
    wrapper = tmp_path / "codex-wrapper"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o700)
    backend = backend_type(wrapper)
    spec, worktree = _spec(tmp_path)

    with pytest.raises(RuntimeError, match=r"^Codex prepare failed$"):
        backend.prepare(spec, worktree)

    assert not (worktree / ".hermes-codex" / "pins").exists()


def test_prepare_revalidates_pinned_executable_identity(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    backend = backend_type(executable)
    replacement = _write_fake(tmp_path / "replacement")
    os.replace(replacement, executable)
    spec, worktree = _spec(tmp_path)

    with pytest.raises(RuntimeError, match=r"^Codex prepare failed$"):
        backend.prepare(spec, worktree)


def test_prepare_pins_open_executable_across_path_replacement(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    replacement = _write_fake(tmp_path / "replacement", replacement_marker=True)
    os.replace(replacement, executable)

    try:
        backend.release(handle)
        _wait_for(worktree / "fake-started.json")
        assert not (worktree / "replacement-ran").exists()
    finally:
        assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


def test_release_revalidates_staged_executable_after_prepare(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    pinned = backend._attempts[handle].pinned_executable
    replacement = _write_fake(tmp_path / "replacement", replacement_marker=True)
    pinned.directory.chmod(0o700)
    os.replace(replacement, pinned.path)
    pinned.directory.chmod(0o500)

    try:
        with pytest.raises(RuntimeError, match=r"^Codex release failed$"):
            backend.release(handle)
        assert not (worktree / "replacement-ran").exists()
    finally:
        assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


def test_exact_hardened_argv_closed_environment_and_same_exec_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    inherited_markers = {
        "OPENAI_API_KEY": "must-not-leak",
        "CODEX_HOME": "/must/not/leak",
        "HERMES_HOME": "/must/not/leak",
        "HTTP_PROXY": "http://must-not-leak",
        "HTTPS_PROXY": "http://must-not-leak",
        "ALL_PROXY": "http://must-not-leak",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
    }
    for key, value in inherited_markers.items():
        monkeypatch.setenv(key, value)

    backend = backend_type(executable)
    handle = None
    try:
        handle = backend.prepare(spec, worktree)
        assert not (worktree / "fake-started.json").exists()
        assert handle.pid == handle.pgid
        backend.release(handle)
        _wait_for(worktree / "fake-started.json")

        argv = json.loads((worktree / "fake-argv.json").read_text())
        expected = list(_EXPECTED_PREFIX)
        expected[6] = str(worktree)
        assert argv == [str(executable), *expected, spec.instructions]

        child_env = json.loads((worktree / "fake-env.json").read_text())
        expected_keys = {
            "HOME",
            "LANG",
            "NO_COLOR",
            "PATH",
            "TEMP",
            "TMP",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        # macOS may inject this process-local locale hint after execve even
        # when it was absent from the explicit environment passed to Popen.
        platform_injected = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
        assert set(child_env) - platform_injected == expected_keys
        assert set(child_env) - expected_keys <= platform_injected
        assert not (set(inherited_markers) & set(child_env))
        for key in ("TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            path = Path(child_env[key])
            assert path.is_relative_to(worktree)
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700

        started = json.loads((worktree / "fake-started.json").read_text())
        assert started == {"pid": handle.pid, "pgid": handle.pgid}
        from tools.process_registry import capture_host_identity

        assert capture_host_identity(handle.pid) == handle.kernel_start_time
        assert backend.observe(handle).state is ExecutionState.RUNNING
    finally:
        _force_cleanup(backend, handle)
    assert _non_zombie_group_members(handle.pgid) == []


@pytest.mark.parametrize("payload", [b"", b"NO\n"], ids=["eof", "nonexact"])
def test_bootstrap_eof_or_nonexact_release_executes_zero_fake_work(
    tmp_path: Path, payload: bytes
) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)

    # Fault injection at the adapter-local release pipe: public release always
    # emits exact GO; controller loss closes this descriptor via EOF. Accessing
    # the private descriptor here avoids adding a production test-only method.
    attempt = backend._attempts[handle]
    if payload:
        os.write(attempt.release_fd, payload)
    os.close(attempt.release_fd)
    attempt.release_fd = None
    observation = _wait_observation(backend, handle, ExecutionState.EXITED)

    assert observation.exit_code == 0
    assert not (worktree / "fake-started.json").exists()
    assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


@pytest.mark.parametrize("mode", ["bad-scope", "overflow", "silent"])
def test_release_fails_closed_on_invalid_or_unbounded_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    backend_module = __import__("hermes_cli.kanban_codex_backend", fromlist=["*"])
    monkeypatch.setattr(backend_module, "STARTUP_TIMEOUT_SECONDS", 0.25)
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions=mode)
    backend = backend_module.CodexDirectExecutionBackend(executable)
    handle = backend.prepare(spec, worktree)

    with pytest.raises(RuntimeError, match=r"^Codex release failed$") as exc_info:
        backend.release(handle)
    assert mode not in str(exc_info.value)
    assert "sandbox" not in str(exc_info.value)
    assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


@pytest.mark.parametrize(
    ("buffered", "overflow"),
    [
        (
            b"sandbox: workspace-write [workdir]\n"
            b"sandbox: workspace-write [workdir, /tmp]\n",
            False,
        ),
        (b"sandbox: workspace-write [workdir]\n", True),
    ],
    ids=["valid-plus-unsafe-scope", "valid-plus-overflow"],
)
def test_release_rejects_unsafe_state_even_with_valid_marker(
    tmp_path: Path,
    buffered: bytes,
    overflow: bool,
) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="silent")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    attempt = backend._attempts[handle]
    with attempt.condition:
        attempt.output.extend(buffered)
        attempt.output_overflow = overflow

    try:
        with pytest.raises(RuntimeError, match=r"^Codex release failed$"):
            backend.release(handle)
    finally:
        assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


def test_observe_reports_exit_code_and_rejects_recycled_root_identity(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="exit-seven")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    backend.release(handle)

    exited = _wait_observation(backend, handle, ExecutionState.EXITED)
    assert exited.exit_code == 7
    assert backend.terminate(handle).state is TerminationState.DEAD

    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        from tools.process_registry import capture_host_identity

        start = capture_host_identity(sentinel.pid)
        assert start is not None
        mismatched = ExecutionHandle(
            backend=PILOT_BACKEND,
            pid=sentinel.pid,
            pgid=os.getpgid(sentinel.pid),
            kernel_start_time=start + 1,
        )
        assert backend.observe(mismatched).state is ExecutionState.IDENTITY_MISMATCH
        assert backend.terminate(mismatched).state is TerminationState.IDENTITY_MISMATCH
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait(timeout=5)


def test_terminate_live_root_and_child_with_zero_member_readback(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    pinned = backend._attempts[handle].pinned_executable
    try:
        backend.release(handle)
        _wait_for(worktree / "fake-child.pid")
        child_pid = int((worktree / "fake-child.pid").read_text())
        assert psutil.pid_exists(child_pid)
        assert set(_non_zombie_group_members(handle.pgid)) >= {handle.pid, child_pid}
        assert pinned.path.exists()

        result = backend.terminate(handle)
        assert result.state is TerminationState.DEAD
        assert _non_zombie_group_members(handle.pgid) == []
        assert handle not in backend._attempts
        assert not backend._attempts_lock.locked()
        assert not pinned.path.exists()
        assert not pinned.directory.exists()
    finally:
        _force_cleanup(backend, handle)


@pytest.mark.live_system_guard_bypass
def test_terminate_root_first_orphan_group_member(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="root-exit")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    try:
        backend.release(handle)
        _wait_for(worktree / "fake-child.pid")
        exited = _wait_observation(backend, handle, ExecutionState.EXITED)
        assert exited.exit_code == 0
        child_pid = int((worktree / "fake-child.pid").read_text())
        assert handle.pid not in _non_zombie_group_members(handle.pgid)
        assert child_pid in _non_zombie_group_members(handle.pgid)

        result = backend.terminate(handle)
        assert result.state is TerminationState.DEAD
        assert _non_zombie_group_members(handle.pgid) == []
    finally:
        _force_cleanup(backend, handle)


def test_public_host_identity_wrappers_refuse_mismatch_without_signal(tmp_path: Path) -> None:
    # This narrow public surface is the only ProcessRegistry dependency the
    # adapter needs; it must preserve the existing PID-reuse refusal.
    from tools.process_registry import capture_host_identity, terminate_host_identity

    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        start = capture_host_identity(sentinel.pid)
        assert type(start) is int
        assert terminate_host_identity(sentinel.pid, start + 1, grace_seconds=0.1) is False
        assert sentinel.poll() is None
        assert terminate_host_identity(sentinel.pid, start, grace_seconds=0.1) is True
        sentinel.wait(timeout=5)
    finally:
        if sentinel.poll() is None:
            sentinel.kill()
            sentinel.wait(timeout=5)


def test_release_fd_close_serializes_on_attempt_condition(tmp_path: Path) -> None:
    backend_type = _api()
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path, instructions="hold")
    backend = backend_type(executable)
    handle = backend.prepare(spec, worktree)
    attempt = backend._attempts[handle]
    release_fd = attempt.release_fd
    assert release_fd is not None
    closed = threading.Event()

    def close_release() -> None:
        backend._close_release(attempt)
        closed.set()

    closer = threading.Thread(target=close_release)
    try:
        with attempt.condition:
            closer.start()
            assert not closed.wait(timeout=0.1)
            assert attempt.release_fd == release_fd
            os.fstat(release_fd)
        closer.join(timeout=2)
        assert not closer.is_alive()
        assert closed.is_set()
        assert attempt.release_fd is None
        with pytest.raises(OSError):
            os.fstat(release_fd)
    finally:
        if closer.is_alive():
            closer.join(timeout=2)
        assert backend.terminate(handle).state is TerminationState.DEAD
    assert _non_zombie_group_members(handle.pgid) == []


def test_group_enumeration_marks_permission_failure_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_type = _api()
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: [SimpleNamespace(pid=12345)])

    def deny_group_lookup(_pid: int) -> int:
        raise PermissionError("not inspectable")

    monkeypatch.setattr(os, "getpgid", deny_group_lookup)

    members, uncertain = backend_type._group_members(12345)

    assert members == []
    assert uncertain is True


def test_adapter_exceptions_are_constant_and_do_not_leak_host_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend_module = __import__("hermes_cli.kanban_codex_backend", fromlist=["*"])
    executable = _write_fake(tmp_path / "fake-codex")
    spec, worktree = _spec(tmp_path)
    backend = backend_module.CodexDirectExecutionBackend(executable)

    def fail_spawn(*_args, **_kwargs):
        raise OSError("dynamic-secret-marker")

    monkeypatch.setattr(backend_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(RuntimeError, match=r"^Codex prepare failed$") as exc_info:
        backend.prepare(spec, worktree)
    assert "dynamic-secret-marker" not in str(exc_info.value)
