"""Offline host integration for the one-shot Codex-direct Kanban lane.

Evidence from this module is deliberately limited to the real host adapter over
a temporary native fake executable.  It is not evidence that a real Codex
sandbox, OAuth flow, credential resolver, provider, or network transport works.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

import psutil
import pytest

from gateway.kanban_watchers import (
    _RecoveryOnlyCodexBackend,
    _compose_codex_backend_registry,
)
from hermes_cli import kanban_db as kb
from hermes_cli import projects_db
from hermes_cli.kanban_codex_backend import CodexDirectExecutionBackend
from hermes_cli.kanban_execution import (
    PILOT_BACKEND,
    PILOT_BOARD,
    ExecutionHandle,
    ExecutionState,
    TerminationState,
)
from tools.process_registry import capture_host_identity


pytestmark = pytest.mark.skipif(os.name != "posix", reason="dedicated POSIX sessions")

_FAKE_MARKER_NAME = "task5-fake-executor.json"
_FAKE_PROCESS_SENTINEL = "TASK5_NATIVE_FAKE_SENTINEL_6D96F269"
_CREDENTIAL_KEYS = (
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "CODEX_HOME",
    "GOOGLE_API_KEY",
    "HERMES_HOME",
    "OPENAI_API_KEY",
)
_PROTECTED_PRODUCTION_PATHS = (
    "hermes_cli/kanban_db.py",
    "hermes_cli/kanban.py",
    "gateway/kanban_watchers.py",
    "tools/process_registry.py",
    "hermes_cli/config.py",
    "cli-config.yaml.example",
)
_EXPECTED_ARGV_SUFFIX = (
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
    None,
)

_FAKE_PAYLOAD = r'''
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROCESS_SENTINEL = "TASK5_NATIVE_FAKE_SENTINEL_6D96F269"
reported_argv = sys.argv[1:]
arguments = reported_argv[1:]
worktree = Path(arguments[arguments.index("-C") + 1])
mode = arguments[-1]
child = None
if mode in {"timeout", "controller-recovery", "pid-mismatch"}:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
marker = {
    "argv": reported_argv,
    "child_pid": child.pid if child is not None else None,
    "credential_keys_present": sorted(
        key for key in (
            "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "CODEX_HOME",
            "GOOGLE_API_KEY",
            "HERMES_HOME",
            "OPENAI_API_KEY",
        ) if key in os.environ
    ),
    "cwd": os.getcwd(),
    "mode": mode,
    "pgid": os.getpgrp(),
    "pid": os.getpid(),
    "process_sentinel": PROCESS_SENTINEL,
}
(worktree / "task5-fake-executor.json").write_text(
    json.dumps(marker, ensure_ascii=False, sort_keys=True),
    encoding="utf-8",
)
print("sandbox: workspace-write [workdir]", flush=True)
if mode == "exit-nonzero":
    raise SystemExit(17)
if mode in {"timeout", "controller-recovery", "pid-mismatch"}:
    while True:
        time.sleep(1)
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in paths}


def _wait_for(predicate: Callable[[], bool], *, description: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError(f"timed out waiting for {description}")


def _non_zombie_group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(process.pid) == pgid:
                members.append(process.pid)
        except (
            ProcessLookupError,
            PermissionError,
            SystemError,
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            OSError,
        ):
            continue
    return sorted(members)


def _residual_fake_processes(executable: Path) -> list[int]:
    residual: list[int] = []
    executable_text = str(executable)
    for process in psutil.process_iter(["pid", "cmdline", "status"]):
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                continue
            argv = process.cmdline()
            if executable_text in argv or any(_FAKE_PROCESS_SENTINEL in arg for arg in argv):
                residual.append(process.pid)
        except (
            PermissionError,
            SystemError,
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            OSError,
        ):
            continue
    return sorted(residual)


def _handle_from_db(conn, task_id: str) -> ExecutionHandle:
    row = conn.execute(
        "SELECT r.metadata FROM task_runs r "
        "JOIN tasks t ON t.current_run_id=r.id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    managed = json.loads(row["metadata"])["managed_execution"]
    return ExecutionHandle(
        backend=managed["backend"],
        pid=managed["pid"],
        pgid=managed["pgid"],
        kernel_start_time=managed["kernel_start_time"],
    )


def _initialize_git_repository(repository: Path) -> None:
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / "seed.txt").write_text("task5 seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "add", "seed.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Task5 Test",
            "-c",
            "user.email=task5@example.invalid",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def native_fake_executable(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile one cached native launcher; its payload never calls Codex or a provider."""
    build_root = tmp_path_factory.mktemp("task5-native-fake")
    source_path = build_root / "fake_codex.c"
    executable = build_root / "fake-codex"
    launcher_source = f"""
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {{
    char **next = calloc((size_t)argc + 4, sizeof(char *));
    if (next == NULL) return 70;
    next[0] = {json.dumps(sys.executable)};
    next[1] = "-c";
    next[2] = {json.dumps(_FAKE_PAYLOAD)};
    for (int i = 0; i < argc; ++i) next[i + 3] = argv[i];
    next[argc + 3] = NULL;
    execv(next[0], next);
    return 70;
}}
"""
    source_path.write_text(launcher_source, encoding="utf-8")
    subprocess.run(
        ["cc", "-O2", "-o", str(executable), str(source_path)],
        check=True,
        capture_output=True,
    )
    executable.chmod(0o700)
    return executable.resolve(strict=True)


@pytest.mark.parametrize(
    "case",
    ["exit-zero", "exit-nonzero", "timeout", "controller-recovery", "pid-mismatch"],
)
def test_codex_direct_retained_slice_is_one_shot_and_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_fake_executable: Path,
    case: str,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    production_paths = tuple(repo_root / relative for relative in _PROTECTED_PRODUCTION_PATHS)
    production_hashes_before = _hashes(production_paths)

    home = tmp_path / "disposable-home"
    hermes_home = home / ".hermes"
    home.mkdir()
    hermes_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    for key in _CREDENTIAL_KEYS:
        if key not in {"HERMES_HOME"}:
            monkeypatch.setenv(key, f"task5-must-not-leak-{key.lower()}")

    forbidden_calls = {"network": 0, "provider": 0, "credential": 0, "live": 0}

    def deny_network(*_args, **_kwargs):
        forbidden_calls["network"] += 1
        raise AssertionError("offline integration attempted network access")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", deny_network)

    original_which = shutil.which

    def guarded_which(name: str, *args, **kwargs):
        if name == "codex":
            forbidden_calls["provider"] += 1
            raise AssertionError("offline integration attempted real Codex resolution")
        return original_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", guarded_which)

    repository = (tmp_path / "temporary-repository").resolve()
    _initialize_git_repository(repository)
    outside_canaries = (
        tmp_path / "outside-worktree.canary",
        repository / "outside-main-checkout.canary",
        home / "no-live-state.canary",
    )
    for index, canary in enumerate(outside_canaries):
        canary.write_text(f"task5-outside-canary-{index}\n", encoding="utf-8")
    outside_hashes_before = _hashes(outside_canaries)

    kb.create_board(PILOT_BOARD)
    with projects_db.connect_closing() as project_conn:
        project_id = projects_db.create_project(
            project_conn,
            name=f"Task5 {case}",
            slug=f"task5-{case}",
            primary_path=str(repository),
            board_slug=PILOT_BOARD,
        )

    backend = CodexDirectExecutionBackend(native_fake_executable)
    original_handle: ExecutionHandle | None = None
    marker: dict[str, object] | None = None
    worktree: Path | None = None
    task_id = ""
    try:
        with kb.connect(board=PILOT_BOARD) as conn:
            kb.migrate_managed_execution_schema(conn)
            task_id = kb.create_task(
                conn,
                title=f"Task5 offline {case}",
                body=case,
                initial_status="blocked",
                project_id=project_id,
                board=PILOT_BOARD,
            )
            spec = kb.approve_managed_execution(
                conn,
                board=PILOT_BOARD,
                task_id=task_id,
                repository_root=repository,
            )
            worktree = Path(spec.worktree_path)
            factory_calls: list[str] = []

            def launch_factory():
                factory_calls.append("native-fake")
                return backend

            registry = _compose_codex_backend_registry(
                {"kanban": {"codex_direct": {"enabled": True}}},
                PILOT_BOARD,
                connection=conn,
                managed_schema_ready=True,
                backend_factory=launch_factory,
                startup_recovery=False,
            )
            first = kb.dispatch_once(
                conn,
                board=PILOT_BOARD,
                backend_registry=registry,
            )
            assert first.managed_started == [task_id]
            assert factory_calls == ["native-fake"]
            original_handle = _handle_from_db(conn, task_id)

            marker_path = worktree / _FAKE_MARKER_NAME
            _wait_for(marker_path.exists, description=f"{case} fake marker")
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            expected_argv = list(_EXPECTED_ARGV_SUFFIX)
            expected_argv[6] = str(worktree)
            expected_argv[-1] = case
            assert marker["argv"] == [str(native_fake_executable), *expected_argv]
            assert marker["cwd"] == str(worktree)
            assert marker["mode"] == case
            assert marker["pid"] == original_handle.pid
            assert marker["pgid"] == original_handle.pgid
            assert marker["credential_keys_present"] == []
            assert marker["process_sentinel"] == _FAKE_PROCESS_SENTINEL
            assert {path.name for path in worktree.iterdir()} == {
                ".git",
                ".hermes-codex",
                "seed.txt",
                _FAKE_MARKER_NAME,
            }

            if case in {"exit-zero", "exit-nonzero"}:
                _wait_for(
                    lambda: backend.observe(original_handle).state is ExecutionState.EXITED,
                    description=f"{case} process exit",
                )
                reconciliation_registry = registry
            elif case == "timeout":
                expired = int(time.time()) - 1801
                conn.execute(
                    "UPDATE tasks SET started_at=? WHERE id=?",
                    (expired, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at=? WHERE task_id=?",
                    (expired, task_id),
                )
                conn.commit()
                reconciliation_registry = registry
            elif case == "controller-recovery":
                recovered_backend = CodexDirectExecutionBackend(native_fake_executable)
                recovery_registry = _compose_codex_backend_registry(
                    {"kanban": {"codex_direct": {"enabled": False}}},
                    PILOT_BOARD,
                    connection=conn,
                    managed_schema_ready=True,
                    backend_factory=lambda: recovered_backend,
                    startup_recovery=True,
                )
                assert isinstance(
                    recovery_registry[PILOT_BACKEND],
                    _RecoveryOnlyCodexBackend,
                )
                reconciliation_registry = recovery_registry
            else:
                row = conn.execute(
                    "SELECT id, metadata FROM task_runs WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                metadata = json.loads(row["metadata"])
                metadata["managed_execution"]["kernel_start_time"] += 1
                conn.execute(
                    "UPDATE task_runs SET metadata=? WHERE id=?",
                    (json.dumps(metadata, sort_keys=True), row["id"]),
                )
                conn.commit()
                reconciliation_registry = registry

            reconciled = kb.dispatch_once(
                conn,
                board=PILOT_BOARD,
                backend_registry=reconciliation_registry,
            )
            task = kb.get_task(conn, task_id)
            run = conn.execute(
                "SELECT status, ended_at, metadata FROM task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            attempts = conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            review_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='managed_review_required'",
                (task_id,),
            ).fetchone()[0]
            safety_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='managed_execution_safety_stopped'",
                (task_id,),
            ).fetchone()[0]

            assert task.status == "blocked"
            assert attempts == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id=? AND status='done'",
                (task_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id=? AND status='ready'",
                (task_id,),
            ).fetchone()[0] == 0

            if case == "pid-mismatch":
                assert reconciled.managed_safety_stopped == [task_id]
                assert reconciled.managed_reconciled == [task_id]
                assert review_events == 0
                assert safety_events == 1
                assert task.current_run_id is not None
                assert run["status"] == "running"
                assert run["ended_at"] is None
                assert capture_host_identity(original_handle.pid) == (
                    original_handle.kernel_start_time
                )
            else:
                assert reconciled.managed_reconciled == [task_id]
                assert reconciled.managed_safety_stopped == []
                assert review_events == 1
                assert safety_events == 0
                assert task.current_run_id is None
                assert run["ended_at"] is not None

            replay = kb.dispatch_once(
                conn,
                board=PILOT_BOARD,
                backend_registry=reconciliation_registry,
            )
            assert replay.managed_started == []
            assert conn.execute(
                "SELECT COUNT(*) FROM task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='managed_review_required'",
                (task_id,),
            ).fetchone()[0] == review_events
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? "
                "AND kind='managed_execution_safety_stopped'",
                (task_id,),
            ).fetchone()[0] == safety_events
    finally:
        if original_handle is not None:
            termination = backend.terminate(original_handle)
            if termination.state is not TerminationState.DEAD:
                actual = capture_host_identity(original_handle.pid)
                if actual == original_handle.kernel_start_time:
                    try:
                        os.killpg(original_handle.pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
        if original_handle is not None:
            _wait_for(
                lambda: not _non_zombie_group_members(original_handle.pgid),
                description=f"{case} zero process-session members",
            )
        _wait_for(
            lambda: not _residual_fake_processes(native_fake_executable),
            description=f"{case} zero residual fake processes",
        )

    assert marker is not None
    assert worktree is not None
    assert _hashes(outside_canaries) == outside_hashes_before
    assert _hashes(production_paths) == production_hashes_before
    assert forbidden_calls == {
        "network": 0,
        "provider": 0,
        "credential": 0,
        "live": 0,
    }


def test_registry_absent_preserves_legacy_profile_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "legacy-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    spawned: list[tuple[str, Path]] = []

    kb.create_board("task5-legacy")
    with kb.connect(board="task5-legacy") as conn:
        task_id = kb.create_task(
            conn,
            title="Unmanaged legacy profile task",
            assignee="legacy-profile",
            initial_status="blocked",
            board="task5-legacy",
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        conn.commit()
        result = kb.dispatch_once(
            conn,
            board="task5-legacy",
            backend_registry=None,
            spawn_fn=lambda task, workspace: spawned.append((task.id, workspace)),
        )

        assert result.managed_started == []
        assert result.managed_reconciled == []
        assert result.managed_safety_stopped == []
        assert [item[0] for item in spawned] == [task_id]
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='kanban_execution_specs'"
        ).fetchone()[0] == 0
