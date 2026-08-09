"""Tests: kanban worker spawn pins TERMINAL_CWD to the task workspace.

Regression coverage for #34619 and #41312 (same root cause): ``_default_spawn``
launched the worker subprocess with ``cwd=workspace`` and set
``HERMES_KANBAN_WORKSPACE``, but did NOT set ``TERMINAL_CWD``. Because
``TERMINAL_CWD`` takes precedence over the process cwd in both
``tools/file_tools.py::_resolve_base_dir`` (relative ``write_file`` paths) and
``agent_init``'s context-file loader (``AGENTS.md`` discovery), workers inherited
the dispatching gateway's cwd — relative writes landed in the gateway user's
home (#41312) and the wrong profile's ``AGENTS.md`` was loaded (#34619).
Pinning ``TERMINAL_CWD`` to the workspace fixes both.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_TERMINAL_GUARD_CONTRACT_MISSING = "repo_terminal_guard_contract_missing"


def _make_task(
    kb,
    *,
    assignee: str = "w",
    workspace_kind: str = "dir",
    current_run_id: int | None = 1,
    claim_lock: str | None = "lock",
    repo_identity: str | None = None,
):
    return kb.Task(
        id="t_cwd",
        title="cwd pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind=workspace_kind,
        workspace_path=None,
        claim_lock=claim_lock,
        claim_expires=None,
        tenant=None,
        current_run_id=current_run_id,
        repo_identity=repo_identity,
    )


def _capture_spawn_env(kb, monkeypatch, workspace: str, *, task=None) -> dict:
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(task or _make_task(kb), workspace)
    return captured


def test_terminal_cwd_pinned_to_workspace(monkeypatch, tmp_path):
    """A real, absolute workspace dir is pinned as TERMINAL_CWD."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))

    assert captured["env"]["TERMINAL_CWD"] == str(workspace)
    # The subprocess cwd and TERMINAL_CWD must agree — both anchor the workspace.
    assert captured["cwd"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace)


def test_terminal_cwd_not_pinned_for_nonexistent_workspace(monkeypatch, tmp_path):
    """A non-directory workspace must NOT clobber the inherited TERMINAL_CWD.

    file_tools rejects relative / sentinel TERMINAL_CWD values, so writing a
    meaningless (nonexistent) path would be worse than leaving the inherited
    one. The guard requires an existing absolute dir.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TERMINAL_CWD", "/pre/existing/anchor")

    from hermes_cli import kanban_db as kb

    missing = tmp_path / "does-not-exist"

    captured = _capture_spawn_env(kb, monkeypatch, str(missing))

    # Inherited value is preserved (not overwritten with a bogus path).
    assert captured["env"]["TERMINAL_CWD"] == "/pre/existing/anchor"


def test_dispatcher_worker_gets_exact_one_shot_repo_lock_marker(monkeypatch, tmp_path):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text(
        "kanban:\n  repo_writer_mode: off\n", encoding="utf-8"
    )
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()
    captured = _capture_spawn_env(
        kb,
        monkeypatch,
        str(workspace),
        task=_make_task(kb, repo_identity="a" * 64),
    )

    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_cwd"
    assert captured["env"]["HERMES_KANBAN_RUN_ID"] == "1"
    assert captured["env"]["HERMES_KANBAN_CLAIM_LOCK"] == "lock"
    assert captured["env"]["HERMES_KANBAN_REPO_LOCK_BOOTSTRAP"] == "1", (
        repo_worker_early_lock_contract_missing
    )


def _running_repo_task(
    kb,
    conn,
    repo: Path,
    *,
    suffix: str,
    assignee: str = "w",
):
    from hermes_cli.repo_write_lock import repo_identity

    identity = repo_identity(repo)
    task_id = kb.create_task(
        conn,
        title=f"early worker {suffix}",
        assignee=assignee,
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    claim = f"claim-{suffix}"
    task = kb.claim_task(conn, task_id, claimer=claim)
    assert task is not None and task.current_run_id is not None
    conn.execute(
        "UPDATE tasks SET repo_identity=? WHERE id=?",
        (identity, task_id),
    )
    conn.commit()
    return task_id, task.current_run_id, claim, identity


def _bootstrap_env(root: Path, db_path: Path, exact: tuple[str, int, str, str]):
    task_id, run_id, claim, _identity = exact
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(root),
            "HERMES_KANBAN_DB": str(db_path),
            "HERMES_KANBAN_TASK": task_id,
            "HERMES_KANBAN_RUN_ID": str(run_id),
            "HERMES_KANBAN_CLAIM_LOCK": claim,
            "HERMES_KANBAN_REPO_LOCK_BOOTSTRAP": "1",
            "HERMES_KANBAN_WORKSPACE": str(root / "spoof-workspace"),
            "HERMES_KANBAN_REPO_LOCK_ROOT": str(root / "spoof-lock-root"),
            "HERMES_KANBAN_REPO_LOCK_FILE": str(root / "spoof.lock"),
        }
    )
    return env


def _launch_bootstrap(env: dict[str, str], *, profile: str = "w"):
    parent, child = socket.socketpair()
    env = dict(env)
    env["HERMES_KANBAN_REPO_LOCK_BOOTSTRAP_TEST_FD"] = str(child.fileno())
    proc = subprocess.Popen(
        [sys.executable, "-c", "import hermes_cli.main", "-p", profile],
        cwd=Path(__file__).parents[2],
        env=env,
        pass_fds=(child.fileno(),),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child.close()
    parent.settimeout(15)
    return proc, parent


def _await_ready(proc, control):
    ready = control.recv(1)
    assert ready == b"R", proc.communicate(timeout=5)


def _clean_exit(proc, control):
    control.sendall(b"X")
    control.close()
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0, (stdout, stderr)


def _assert_denied(proc, control, expected_code):
    assert control.recv(1) == b""
    control.close()
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == expected_code, (stdout, stderr)
    assert b"repo_lock_bootstrap_denied" in stderr
    assert b"Traceback" not in stderr


def test_repo_worker_early_lock_contract_missing_real_subprocess_lifetime(tmp_path):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: off\n", encoding="utf-8"
    )
    repo = tmp_path / "repo"
    spoof_repo = root / "spoof-workspace"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "init", "-q", str(spoof_repo)], check=True)

    from hermes_cli import kanban_db as kb

    db_path = root / "kanban.db"
    with kb.connect(db_path) as conn:
        owner = _running_repo_task(kb, conn, repo, suffix="owner")
        contender = _running_repo_task(kb, conn, repo, suffix="contender")
        contender_task = kb.get_task(conn, contender[0])
        assert contender_task is not None
        before_failures = contender_task.consecutive_failures

    owner_proc, owner_control = _launch_bootstrap(_bootstrap_env(root, db_path, owner))
    _await_ready(owner_proc, owner_control)

    descendant_env = _bootstrap_env(root, db_path, owner)
    descendant_env.pop("HERMES_KANBAN_REPO_LOCK_BOOTSTRAP")
    descendant, descendant_control = _launch_bootstrap(descendant_env)
    _await_ready(descendant, descendant_control)
    _clean_exit(descendant, descendant_control)

    busy, busy_control = _launch_bootstrap(_bootstrap_env(root, db_path, contender))
    assert busy_control.recv(1) == b""
    busy_control.close()
    busy_out, busy_err = busy.communicate(timeout=15)
    assert busy.returncode == kb.KANBAN_REPO_BUSY_EXIT_CODE, (
        repo_worker_early_lock_contract_missing,
        busy_out,
        busy_err,
    )
    assert b"Traceback" not in busy_err
    assert not (root / "spoof-lock-root").exists()
    assert not (root / "spoof.lock").exists()
    with kb.connect(db_path) as conn:
        released = kb.get_task(conn, contender[0])
        assert released is not None
        assert released.status == "ready"
        assert released.current_run_id is None
        assert released.consecutive_failures == before_failures
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
            (contender[1],),
        ).fetchone()
        assert tuple(run[:2]) == ("repo_busy", "repo_busy")
        assert run["ended_at"] is not None

    with kb.connect(db_path) as conn:
        rows_before = (
            tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (owner[0],)).fetchone()),
            tuple(conn.execute("SELECT * FROM task_runs WHERE id=?", (owner[1],)).fetchone()),
        )
    malformed_envs = []
    bad_marker = _bootstrap_env(root, db_path, owner)
    bad_marker["HERMES_KANBAN_REPO_LOCK_BOOTSTRAP"] = "0"
    malformed_envs.append(bad_marker)
    partial = _bootstrap_env(root, db_path, owner)
    partial.pop("HERMES_KANBAN_CLAIM_LOCK")
    malformed_envs.append(partial)
    malformed = _bootstrap_env(root, db_path, owner)
    malformed["HERMES_KANBAN_RUN_ID"] = "+1"
    malformed_envs.append(malformed)
    stale = _bootstrap_env(root, db_path, owner)
    stale["HERMES_KANBAN_CLAIM_LOCK"] = owner[2] + "-stale"
    malformed_envs.append(stale)
    foreign = _bootstrap_env(root, db_path, owner)
    foreign["HERMES_KANBAN_TASK"] = contender[0]
    malformed_envs.append(foreign)
    for denied_env in malformed_envs:
        denied, denied_control = _launch_bootstrap(denied_env)
        _assert_denied(denied, denied_control, kb.KANBAN_REPO_BOOTSTRAP_DENIED_EXIT_CODE)
    with kb.connect(db_path) as conn:
        assert rows_before == (
            tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (owner[0],)).fetchone()),
            tuple(conn.execute("SELECT * FROM task_runs WHERE id=?", (owner[1],)).fetchone()),
        )

    _clean_exit(owner_proc, owner_control)
    clean, clean_control = _launch_bootstrap(_bootstrap_env(root, db_path, owner))
    _await_ready(clean, clean_control)
    _clean_exit(clean, clean_control)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        victim, victim_control = _launch_bootstrap(_bootstrap_env(root, db_path, owner))
        _await_ready(victim, victim_control)
        victim.send_signal(sig)
        victim_control.close()
        victim.communicate(timeout=15)
        assert victim.returncode == -sig
        reacquired, reacquired_control = _launch_bootstrap(
            _bootstrap_env(root, db_path, owner)
        )
        _await_ready(reacquired, reacquired_control)
        _clean_exit(reacquired, reacquired_control)


def test_repo_worker_early_lock_contract_missing_profile_mode_off_cannot_cancel_marker(
    tmp_path,
):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: off\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(root),
            "HERMES_KANBAN_REPO_LOCK_BOOTSTRAP": "1",
            "HERMES_KANBAN_TASK": "malformed-must-deny-even-when-profile-off",
        }
    )
    proc, control = _launch_bootstrap(env)
    _assert_denied(proc, control, 77)


def _spawn_import_via_default(kb, monkeypatch, task, workspace: Path) -> int:
    monkeypatch.setattr(
        kb,
        "_resolve_hermes_argv",
        lambda: [sys.executable, "-c", "import hermes_cli.main"],
    )
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    pid = kb._default_spawn(task, str(workspace))
    waited_pid, status = os.waitpid(pid, 0)
    assert waited_pid == pid
    return os.waitstatus_to_exitcode(status)


@pytest.mark.parametrize(
    "assignee_config",
    [None, "kanban:\n  repo_writer_mode: off\n"],
    ids=["missing", "off"],
)
def test_repo_worker_early_lock_contract_missing_dispatcher_marker_survives_profile(
    monkeypatch,
    tmp_path,
    assignee_config,
):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    holder_profile = root / "profiles" / "holder"
    assignee_profile = root / "profiles" / "w"
    holder_profile.mkdir(parents=True)
    assignee_profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    holder_profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    if assignee_config is not None:
        assignee_profile.joinpath("config.yaml").write_text(
            assignee_config, encoding="utf-8"
        )
    monkeypatch.setenv("HERMES_HOME", str(root))

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    from hermes_cli import kanban_db as kb

    db_path = root / "kanban.db"
    with kb.connect(db_path) as conn:
        owner = _running_repo_task(
            kb, conn, repo, suffix="owner", assignee="holder"
        )
        contender = _running_repo_task(kb, conn, repo, suffix="contender")
        contender_task = kb.get_task(conn, contender[0])
        assert contender_task is not None
        failures_before = contender_task.consecutive_failures

    holder, holder_control = _launch_bootstrap(
        _bootstrap_env(root, db_path, owner), profile="holder"
    )
    _await_ready(holder, holder_control)
    try:
        returncode = _spawn_import_via_default(
            kb, monkeypatch, contender_task, repo
        )
        assert returncode == kb.KANBAN_REPO_BUSY_EXIT_CODE, (
            repo_worker_early_lock_contract_missing
        )
        with kb.connect(db_path) as conn:
            released = kb.get_task(conn, contender[0])
            assert released is not None
            assert released.status == "ready"
            assert released.current_run_id is None
            assert released.consecutive_failures == failures_before
            run = conn.execute(
                "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
                (contender[1],),
            ).fetchone()
            assert tuple(run[:2]) == ("repo_busy", "repo_busy")
            assert run["ended_at"] is not None
            events = conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id=? AND run_id=? AND kind='repo_busy'",
                (contender[0], contender[1]),
            ).fetchone()[0]
            assert events == 1
    finally:
        _clean_exit(holder, holder_control)


def test_repo_worker_early_lock_contract_missing_dispatcher_off_profile_on_is_noop(
    monkeypatch,
    tmp_path,
):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: off\n", encoding="utf-8"
    )
    profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    from hermes_cli import kanban_db as kb

    with kb.connect(root / "kanban.db") as conn:
        exact = _running_repo_task(kb, conn, repo, suffix="mode-off")
        task = kb.get_task(conn, exact[0])
        assert task is not None

    assert _spawn_import_via_default(kb, monkeypatch, task, repo) == 0
    assert not (root / "kanban" / "repo-locks").exists(), (
        repo_worker_early_lock_contract_missing
    )


def test_repo_worker_early_lock_contract_missing_dispatcher_single_writer_scratch_is_noop(
    monkeypatch,
    tmp_path,
):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    with kb.connect(root / "kanban.db") as conn:
        task_id = kb.create_task(
            conn,
            title="scratch stays unlocked",
            assignee="w",
            workspace_kind="scratch",
        )
        task = kb.claim_task(conn, task_id, claimer="scratch-claim")
        assert task is not None

    workspace = tmp_path / "scratch"
    workspace.mkdir()
    assert _spawn_import_via_default(kb, monkeypatch, task, workspace) == 0
    assert not (root / "kanban" / "repo-locks").exists(), (
        repo_worker_early_lock_contract_missing
    )


@pytest.mark.parametrize(
    "task",
    [
        {"current_run_id": None, "repo_identity": "a" * 64},
        {"current_run_id": 0, "repo_identity": "a" * 64},
        {"claim_lock": None, "repo_identity": "a" * 64},
        {"claim_lock": " malformed", "repo_identity": "a" * 64},
        {"repo_identity": None},
        {"repo_identity": "A" * 64},
        {"repo_identity": "a" * 63},
    ],
)
def test_repo_worker_early_lock_contract_missing_dispatcher_fails_closed_before_popen(
    monkeypatch,
    tmp_path,
    task,
):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    (root / "profiles" / "w").mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    popen_called = False

    def forbidden_popen(*_args, **_kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with pytest.raises(RuntimeError) as exc_info:
        kb._default_spawn(_make_task(kb, **task), str(workspace))
    assert str(exc_info.value) == "repo_worker_bootstrap_authority_invalid", (
        repo_worker_early_lock_contract_missing
    )
    assert not popen_called


def test_repo_worker_early_lock_contract_missing_absent_marker_profile_on_is_noop(
    tmp_path,
):
    repo_worker_early_lock_contract_missing = "repo_worker_early_lock_contract_missing"
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(root),
            "HERMES_KANBAN_TASK": "ambient-task-is-not-authority",
            "HERMES_KANBAN_RUN_ID": "1",
            "HERMES_KANBAN_CLAIM_LOCK": "ambient-claim",
        }
    )
    proc, control = _launch_bootstrap(env)
    _await_ready(proc, control)
    assert not (root / "kanban" / "repo-locks").exists(), (
        repo_worker_early_lock_contract_missing
    )
    _clean_exit(proc, control)


def test_repo_writer_tool_capability_contract_missing_lock_survives_dotenv_override(
    tmp_path,
):
    repo_writer_tool_capability_contract_missing = (
        "repo_writer_tool_capability_contract_missing"
    )
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text(
        "kanban:\n  repo_writer_mode: single_writer\n", encoding="utf-8"
    )
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath(".env").write_text(
        "HERMES_KANBAN_REPO_WRITER=0\n", encoding="utf-8"
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    from hermes_cli import kanban_db as kb

    db_path = root / "kanban.db"
    with kb.connect(db_path) as conn:
        exact = _running_repo_task(kb, conn, repo, suffix="dotenv-owner")

    env = _bootstrap_env(root, db_path, exact)
    env["HERMES_KANBAN_REPO_WRITER"] = "1"
    env.pop("HERMES_KANBAN_REPO_LOCK_BOOTSTRAP_TEST_FD", None)
    parent, child = socket.socketpair()
    env["TASK5_RESULT_FD"] = str(child.fileno())
    script = """
import json
import os
import hermes_cli.main as main
fd = int(os.environ.pop("TASK5_RESULT_FD"))
result = {
    "marker": os.environ.get("HERMES_KANBAN_REPO_WRITER"),
    "lock_held": main._KANBAN_REPO_LOCK_LIFETIME_OWNER is not None,
}
os.write(fd, (json.dumps(result, sort_keys=True) + "\\n").encode())
if os.read(fd, 1) != b"X":
    raise SystemExit(91)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "-p", "w"],
        cwd=Path(__file__).parents[2],
        env=env,
        pass_fds=(child.fileno(),),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child.close()
    parent.settimeout(20)
    payload = b""
    stdout = stderr = b""
    try:
        while not payload.endswith(b"\n"):
            chunk = parent.recv(4096)
            assert chunk, repo_writer_tool_capability_contract_missing
            payload += chunk
        result = json.loads(payload)
        assert result == {"lock_held": True, "marker": "1"}, (
            repo_writer_tool_capability_contract_missing
        )
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from hermes_cli.repo_write_lock import RepoLockBusy, RepoWriteLock
try:
    handle = RepoWriteLock(__import__("sys").argv[1], blocking=False)
    handle.acquire()
except RepoLockBusy:
    raise SystemExit(0)
handle.release()
raise SystemExit(1)
""",
                str(repo),
            ],
            cwd=Path(__file__).parents[2],
            env={**os.environ, "HERMES_HOME": str(root)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        assert contender.returncode == 0, (
            repo_writer_tool_capability_contract_missing,
            contender.stdout,
            contender.stderr,
        )
    finally:
        try:
            parent.sendall(b"X")
        except OSError:
            pass
        parent.close()
        try:
            stdout, stderr = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            raise AssertionError(repo_writer_tool_capability_contract_missing)
    assert proc.returncode == 0, (stdout, stderr)


def test_repo_worker_bootstrap_source_order_is_before_dotenv_logging_and_plugins():
    repo_writer_tool_capability_contract_missing = (
        "repo_writer_tool_capability_contract_missing"
    )
    source = (Path(__file__).parents[2] / "hermes_cli" / "main.py").read_text(
        encoding="utf-8"
    )
    profile = source.index("_apply_profile_override()", source.index("def _apply_profile_override"))
    capture = source.index(
        "from hermes_cli import repo_writer_context as _repo_writer_context", profile
    )
    bootstrap = source.index("_bootstrap_kanban_repo_lock()", profile)
    dotenv_import = source.index("from hermes_cli.env_loader import", profile)
    dotenv_call = source.index("load_hermes_dotenv(", dotenv_import)
    logging_setup = source.index("from hermes_logging import setup_logging", dotenv_call)
    owner_source = (
        Path(__file__).parents[2]
        / "hermes_cli"
        / "repo_writer_context.py"
    ).read_text(encoding="utf-8")
    assert "_set_repo_writer_context_trusted" not in source
    assert "_KANBAN_REPO_WRITER_CONTEXT_TRUSTED" not in source
    assert owner_source.count(
        "os.environ.get(REPO_WRITER_CONTEXT_ENV) == \"1\""
    ) == 1, repo_writer_tool_capability_contract_missing
    assert (
        profile
        < capture
        < bootstrap
        < dotenv_import
        < dotenv_call
        < logging_setup
    ), (
        repo_writer_tool_capability_contract_missing
    )


def _task7_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _task7_contender(repo: Path, hermes_home: Path, *, busy_expected: bool):
    script = """
import sys
from hermes_cli.repo_write_lock import RepoLockBusy, RepoWriteLock
try:
    lock = RepoWriteLock(sys.argv[1], blocking=False)
    lock.acquire()
except RepoLockBusy:
    raise SystemExit(23)
lock.release()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(repo)],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "HERMES_HOME": str(hermes_home)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    expected = 23 if busy_expected else 0
    assert completed.returncode == expected, (
        REPO_TERMINAL_GUARD_CONTRACT_MISSING,
        completed.stdout,
        completed.stderr,
    )


def _task7_fake_local_config(repo: Path) -> dict:
    return {
        "env_type": "local",
        "cwd": str(repo),
        "timeout": 30,
        "lifetime_seconds": 300,
        "local_persistent": False,
        "host_cwd": None,
    }


def _task7_isolate_terminal(monkeypatch, terminal_module, repo: Path):
    monkeypatch.setattr(
        terminal_module, "_get_env_config", lambda: _task7_fake_local_config(repo)
    )
    monkeypatch.setattr(terminal_module, "resolve_task_overrides", lambda _task: {})
    monkeypatch.setattr(terminal_module, "_resolve_container_task_id", lambda task: task or "default")
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_module, "_active_environments", {})
    monkeypatch.setattr(terminal_module, "_last_activity", {})
    monkeypatch.setattr(terminal_module, "_creation_locks", {})
    monkeypatch.setattr(terminal_module, "record_session_cwd", lambda *_a, **_kw: None)


def test_foreground_guard_pins_relative_workdir_to_actual_local_base(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "relative-repo")
    subdir = repo / "nested"
    subdir.mkdir()
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    observed: dict[str, str] = {}

    class FakeEnv:
        cwd = str(repo)
        env = {}

        def execute(self, command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            return {"output": "ok", "returncode": 0}

    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    result = json.loads(
        terminal_module.terminal_tool(
            "printf ok",
            workdir="nested",
            task_id="task7-relative",
            force=True,
        )
    )

    assert result["exit_code"] == 0
    assert observed == {
        "command": "printf ok",
        "cwd": str(subdir.resolve(strict=True)),
    }, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    _task7_contender(repo, hermes_home, busy_expected=False)


def test_missing_requested_subdir_under_externally_locked_repo_denies_before_env(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "missing-locked-repo")
    requested = repo / "virtual" / "missing"
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **_kw: (_ for _ in ()).throw(
            AssertionError("environment created before fallback repo guard")
        ),
    )
    owner_script = """
import sys
from hermes_cli.repo_write_lock import RepoWriteLock
lock = RepoWriteLock(sys.argv[1], blocking=False)
lock.acquire()
print("ready", flush=True)
sys.stdin.read(1)
lock.release()
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(repo)],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "HERMES_HOME": str(hermes_home)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "ready"
        result = json.loads(
            terminal_module.terminal_tool(
                "printf blocked",
                workdir=str(requested),
                task_id="task7-missing-locked",
                force=True,
            )
        )
        assert result == {
            "status": "blocked",
            "error": "repo_terminal_guard_denied",
            "code": "repo_busy",
        }, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    finally:
        assert owner.stdin is not None
        owner.stdin.write("x")
        owner.stdin.close()
        owner.wait(timeout=15)
        assert owner.returncode == 0, owner.stderr.read() if owner.stderr else ""


def test_missing_requested_subdir_holds_fallback_repo_guard_but_keeps_raw_cwd(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "missing-owner-repo")
    requested = repo / "virtual" / "missing"
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    observed: dict[str, str] = {}

    class FakeEnv:
        cwd = str(repo)
        env = {}

        def execute(self, command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            _task7_contender(repo, hermes_home, busy_expected=True)
            return {"output": "owner", "returncode": 0}

    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    result = json.loads(
        terminal_module.terminal_tool(
            "printf owner",
            workdir=str(requested),
            task_id="task7-missing-owner",
            force=True,
        )
    )

    assert result["exit_code"] == 0
    assert observed == {
        "command": "printf owner",
        "cwd": str(requested),
    }, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    _task7_contender(repo, hermes_home, busy_expected=False)


def test_missing_virtual_workspace_outside_repo_keeps_legacy_cwd_and_no_lock(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    outside = tmp_path / "outside-repository"
    outside.mkdir()
    requested = Path("/workspace") / f"task7-virtual-missing-{tmp_path.name}"
    assert not requested.exists()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _task7_isolate_terminal(monkeypatch, terminal_module, outside)
    observed: dict[str, str] = {}

    class FakeOutsideEnv:
        cwd = str(outside)
        env = {}

        def execute(self, command, **kwargs):
            observed["command"] = command
            observed["cwd"] = kwargs["cwd"]
            return {"output": "legacy", "returncode": 0}

    monkeypatch.setattr(
        terminal_module, "_create_environment", lambda **_kw: FakeOutsideEnv()
    )
    result = json.loads(
        terminal_module.terminal_tool(
            "printf legacy",
            workdir=str(requested),
            task_id="task7-virtual-outside",
            force=True,
        )
    )

    assert result["exit_code"] == 0
    assert observed == {
        "command": "printf legacy",
        "cwd": str(requested),
    }, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    assert not (hermes_home / "kanban" / "repo-locks").exists()


def test_foreground_guard_owner_lifetime_includes_execute_and_response_hooks(
    tmp_path, monkeypatch
):
    import agent.verification_evidence as evidence_module
    import hermes_cli.plugins as plugins_module
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "lifetime-repo")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    execute_entered = threading.Event()
    allow_execute = threading.Event()
    hook_entered = threading.Event()
    allow_hook = threading.Event()
    outcome: list[str] = []

    class FakeEnv:
        cwd = str(repo)
        env = {}

        def execute(self, _command, **kwargs):
            assert kwargs["cwd"] == str(repo.resolve(strict=True))
            execute_entered.set()
            assert allow_execute.wait(timeout=10)
            return {"output": "owner", "returncode": 0}

    def blocking_hook(*_args, **_kwargs):
        hook_entered.set()
        assert allow_hook.wait(timeout=10)
        return []

    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    monkeypatch.setattr(plugins_module, "invoke_hook", blocking_hook)
    monkeypatch.setattr(evidence_module, "record_terminal_result", lambda **_kw: None)

    worker = threading.Thread(
        target=lambda: outcome.append(
            terminal_module.terminal_tool(
                "printf owner", workdir=str(repo), task_id="task7-owner", force=True
            )
        )
    )
    worker.start()
    assert execute_entered.wait(timeout=10)
    _task7_contender(repo, hermes_home, busy_expected=True)
    allow_execute.set()
    assert hook_entered.wait(timeout=10)
    _task7_contender(repo, hermes_home, busy_expected=True)
    allow_hook.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert json.loads(outcome[0])["exit_code"] == 0
    _task7_contender(repo, hermes_home, busy_expected=False)


def test_real_local_foreground_command_holds_lock_until_terminal_returns(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "real-owner-repo")
    ready = tmp_path / "command-ready"
    release = tmp_path / "command-release"
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    outcome: list[str] = []
    script = (
        "import pathlib,time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        f"release=pathlib.Path({str(release)!r}); "
        "deadline=time.monotonic()+10; "
        "exec(\"while not release.exists() and time.monotonic() < deadline:\\n time.sleep(0.02)\"); "
        "assert release.exists()"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    worker = threading.Thread(
        target=lambda: outcome.append(
            terminal_module.terminal_tool(
                command,
                workdir=str(repo),
                task_id="task7-real-owner",
                force=True,
                timeout=15,
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), REPO_TERMINAL_GUARD_CONTRACT_MISSING
    _task7_contender(repo, hermes_home, busy_expected=True)
    release.write_text("release", encoding="utf-8")
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert json.loads(outcome[0])["exit_code"] == 0
    _task7_contender(repo, hermes_home, busy_expected=False)


@pytest.mark.parametrize("cleanup_outcome", ["timeout", "interrupt"])
def test_foreground_guard_survives_blocked_timeout_and_interrupt_cleanup(
    tmp_path, monkeypatch, cleanup_outcome
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, f"cleanup-{cleanup_outcome}")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    finished = threading.Event()

    class CleanupBlockingEnv:
        cwd = str(repo)
        env = {}

        def execute(self, _command, **_kwargs):
            cleanup_entered.set()
            assert allow_cleanup.wait(timeout=10)
            if cleanup_outcome == "timeout":
                raise RuntimeError("timeout after process-group cleanup")
            raise KeyboardInterrupt("interrupt after process-group cleanup")

    monkeypatch.setattr(
        terminal_module, "_create_environment", lambda **_kw: CleanupBlockingEnv()
    )

    def run_terminal():
        try:
            terminal_module.terminal_tool(
                "printf cleanup",
                workdir=str(repo),
                task_id=f"task7-cleanup-{cleanup_outcome}",
                force=True,
            )
        except KeyboardInterrupt:
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=run_terminal)
    worker.start()
    assert cleanup_entered.wait(timeout=10)
    _task7_contender(repo, hermes_home, busy_expected=True)
    allow_cleanup.set()
    assert finished.wait(timeout=10)
    worker.join(timeout=10)
    assert not worker.is_alive()
    _task7_contender(repo, hermes_home, busy_expected=False)


@pytest.mark.parametrize(
    "phase",
    ["creation", "approval", "retry", "timeout", "interrupt", "hook", "evidence"],
)
def test_foreground_guard_releases_on_all_managed_exit_paths(
    tmp_path, monkeypatch, phase
):
    import agent.verification_evidence as evidence_module
    import hermes_cli.plugins as plugins_module
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, f"release-{phase}")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    monkeypatch.setattr(terminal_module.time, "sleep", lambda _seconds: None)

    class FakeEnv:
        cwd = str(repo)
        env = {}
        calls = 0

        def execute(self, _command, **_kwargs):
            self.calls += 1
            if phase == "retry":
                raise RuntimeError("transient")
            if phase == "timeout":
                raise RuntimeError("timeout while cleanup completed")
            if phase == "interrupt":
                raise KeyboardInterrupt("interrupt after cleanup")
            return {"output": "ok", "returncode": 0}

    if phase == "creation":
        monkeypatch.setattr(
            terminal_module,
            "_create_environment",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("create failed")),
        )
    else:
        monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    if phase == "approval":
        monkeypatch.setattr(
            terminal_module,
            "_check_all_guards",
            lambda *_a, **_kw: {"approved": False, "message": "denied"},
        )
    if phase == "hook":
        monkeypatch.setattr(
            plugins_module,
            "invoke_hook",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("hook failed")),
        )
    if phase == "evidence":
        monkeypatch.setattr(
            evidence_module,
            "record_terminal_result",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("evidence failed")),
        )

    raw = ""
    try:
        raw = terminal_module.terminal_tool(
            "printf release",
            workdir=str(repo),
            task_id=f"task7-release-{phase}",
            force=phase != "approval",
        )
    except KeyboardInterrupt:
        pass

    if phase == "retry":
        assert json.loads(raw)["exit_code"] == -1
        assert terminal_module._active_environments[
            f"task7-release-{phase}"
        ].calls == 1
    _task7_contender(repo, hermes_home, busy_expected=False)


def test_nonlocal_foreground_never_constructs_host_repo_guard(tmp_path, monkeypatch):
    import tools.terminal_tool as terminal_module

    repo = _task7_git_repo(tmp_path, "remote-repo")
    config = _task7_fake_local_config(repo)
    config["env_type"] = "ssh"
    monkeypatch.setattr(terminal_module, "_get_env_config", lambda: config)
    monkeypatch.setattr(terminal_module, "resolve_task_overrides", lambda _task: {})
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_module, "_active_environments", {})
    monkeypatch.setattr(terminal_module, "_last_activity", {})
    monkeypatch.setattr(terminal_module, "_creation_locks", {})
    monkeypatch.setattr(
        terminal_module,
        "RepoWriteGuard",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("nonlocal backend constructed host guard")
        ),
    )

    class RemoteEnv:
        cwd = str(repo)

        def execute(self, _command, **_kwargs):
            return {"output": "remote", "returncode": 0}

    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: RemoteEnv())
    assert json.loads(
        terminal_module.terminal_tool(
            "printf remote", task_id="task7-remote", force=True
        )
    )["output"] == "remote"


def test_outside_repo_local_foreground_preserves_legacy_result_and_no_lock_leaf(
    tmp_path, monkeypatch
):
    import agent.verification_evidence as evidence_module
    import hermes_cli.plugins as plugins_module
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    outside = tmp_path / "outside-repository"
    outside.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _task7_isolate_terminal(monkeypatch, terminal_module, outside)

    class FakeOutsideEnv:
        cwd = str(outside)
        env = {}

        def execute(self, _command, **_kwargs):
            return {"output": "outside", "returncode": 0}

    monkeypatch.setattr(
        terminal_module, "_create_environment", lambda **_kw: FakeOutsideEnv()
    )
    monkeypatch.setattr(plugins_module, "invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setattr(evidence_module, "record_terminal_result", lambda **_kw: None)
    raw = terminal_module.terminal_tool(
        "printf outside",
        workdir=str(outside),
        task_id="task7-outside",
        force=True,
    )

    assert raw == '{"output": "outside", "exit_code": 0, "error": null}'
    assert not (hermes_home / "kanban" / "repo-locks").exists()


def test_foreground_guard_release_failure_is_constant_logged_and_suppressed(
    tmp_path, monkeypatch, caplog
):
    import tools.terminal_tool as terminal_module
    from hermes_cli.repo_write_guard import RepoWriteGuardOperationError

    repo = _task7_git_repo(tmp_path, "release-error-repo")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)

    class FailingReleaseGuard:
        identities = ("a" * 64,)

        def __init__(self, endpoints, *, directories):
            assert endpoints == [repo.resolve(strict=True)]
            assert directories is True

        def acquire(self):
            return self

        def release(self):
            raise RepoWriteGuardOperationError()

    class FakeEnv:
        cwd = str(repo)
        env = {}

        def execute(self, _command, **_kwargs):
            return {"output": "ran", "returncode": 0}

    monkeypatch.setattr(terminal_module, "RepoWriteGuard", FailingReleaseGuard)
    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    raw = terminal_module.terminal_tool(
        "printf TOP-SECRET-command",
        workdir=str(repo),
        task_id="task7-release-error",
        force=True,
    )

    assert json.loads(raw)["output"] == "ran"
    messages = [record.getMessage() for record in caplog.records]
    assert "repo_terminal_guard_release_failed code=repo_operation" in messages
    assert all("TOP-SECRET" not in message for message in messages)
    assert all(str(repo) not in message for message in messages)


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s' /tmp/absolute-path",
        "cd nested && printf internal-cd",
        "git -C . status --short",
        "setsid true",
    ],
)
def test_repo_guard_documents_command_grammar_non_guarantees(
    tmp_path, monkeypatch, command
):
    import tools.terminal_tool as terminal_module

    repo = _task7_git_repo(tmp_path, "grammar-repo")
    (repo / "nested").mkdir()
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    observed: list[str] = []

    class FakeEnv:
        cwd = str(repo)
        env = {}

        def execute(self, actual, **_kwargs):
            observed.append(actual)
            return {"output": "bounded", "returncode": 0}

    monkeypatch.setattr(terminal_module, "_create_environment", lambda **_kw: FakeEnv())
    result = json.loads(
        terminal_module.terminal_tool(
            command, workdir=str(repo), task_id=f"task7-grammar-{len(command)}", force=True
        )
    )
    assert result["exit_code"] == 0, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    assert observed == [command]


def test_new_local_environment_uses_guarded_workdir_for_init_and_actual_popen(
    tmp_path, monkeypatch
):
    import tools.environments.local as local_environment
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo_a = _task7_git_repo(tmp_path, "config-repo-a")
    repo_b = _task7_git_repo(tmp_path, "workdir-repo-b")
    marker = repo_b / "actual-popen-marker"
    _task7_isolate_terminal(monkeypatch, terminal_module, repo_a)
    real_create = terminal_module._create_environment
    real_popen = local_environment.subprocess.Popen
    created = []
    popen_cwds: list[str] = []

    def recording_popen(*args, **kwargs):
        popen_cwds.append(kwargs["cwd"])
        return real_popen(*args, **kwargs)

    def recording_create(**kwargs):
        assert kwargs["cwd"] == str(repo_b.resolve(strict=True))
        environment = real_create(**kwargs)
        created.append(environment)
        assert environment.cwd == str(repo_b.resolve(strict=True))
        return environment

    monkeypatch.setattr(local_environment.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(terminal_module, "_create_environment", recording_create)
    try:
        raw = terminal_module.terminal_tool(
            f"pwd && touch {shlex.quote(marker.name)}",
            workdir=str(repo_b),
            task_id="task7-real-new-env-repo-b",
            force=True,
        )
    finally:
        for environment in created:
            environment.cleanup()

    result = json.loads(raw)
    assert result["exit_code"] == 0
    assert result["output"].strip() == str(repo_b)
    assert marker.exists()
    assert popen_cwds
    assert {str(cwd) for cwd in popen_cwds} == {str(repo_b.resolve(strict=True))}
    assert not (repo_a / marker.name).exists()


@pytest.mark.parametrize("seam", ["cleanup", "approval"])
@pytest.mark.parametrize("requested_kind", ["exact", "missing"])
def test_foreground_guard_revalidates_identity_after_deterministic_path_swap(
    tmp_path, monkeypatch, seam, requested_kind
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    guarded_repo = _task7_git_repo(tmp_path, f"guarded-{seam}")
    replacement_repo = _task7_git_repo(tmp_path, f"replacement-{seam}")
    displaced_repo = tmp_path / f"displaced-{seam}"
    marker = replacement_repo / "must-not-run"
    requested = (
        guarded_repo
        if requested_kind == "exact"
        else guarded_repo / "virtual" / "missing"
    )
    _task7_isolate_terminal(monkeypatch, terminal_module, guarded_repo)
    create_calls = 0
    execute_calls = 0

    class FakeEnv:
        cwd = str(guarded_repo)
        env = {}

        def execute(self, _command, **_kwargs):
            nonlocal execute_calls
            execute_calls += 1
            marker.write_text("ran", encoding="utf-8")
            return {"output": "ran", "returncode": 0}

    def create_environment(**_kwargs):
        nonlocal create_calls
        create_calls += 1
        return FakeEnv()

    swapped = False

    def swap_path() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        guarded_repo.rename(displaced_repo)
        guarded_repo.symlink_to(replacement_repo, target_is_directory=True)

    monkeypatch.setattr(terminal_module, "_create_environment", create_environment)
    if seam == "cleanup":
        monkeypatch.setattr(terminal_module, "_start_cleanup_thread", swap_path)
    else:
        monkeypatch.setattr(
            terminal_module,
            "_check_all_guards",
            lambda *_args, **_kwargs: (swap_path() or {"approved": True}),
        )

    raw = terminal_module.terminal_tool(
        f"touch {shlex.quote(marker.name)}",
        workdir=str(requested),
        task_id=f"task7-swap-{seam}-{requested_kind}",
        force=seam != "approval",
    )

    assert json.loads(raw) == {
        "status": "blocked",
        "error": "repo_terminal_guard_denied",
        "code": "repo_operation",
    }
    assert execute_calls == 0
    assert not marker.exists()
    if seam == "cleanup":
        assert create_calls == 0


def test_foreground_guard_does_not_retry_after_execute_mutates_and_fails(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    guarded_repo = _task7_git_repo(tmp_path, "guarded-retry")
    replacement_repo = _task7_git_repo(tmp_path, "replacement-retry")
    displaced_repo = tmp_path / "displaced-retry"
    marker = replacement_repo / "must-not-run-twice"
    _task7_isolate_terminal(monkeypatch, terminal_module, guarded_repo)
    monkeypatch.setattr(terminal_module.time, "sleep", lambda _seconds: None)
    execute_calls = 0

    class SwappingEnv:
        cwd = str(guarded_repo)
        env = {}

        def execute(self, _command, **_kwargs):
            nonlocal execute_calls
            execute_calls += 1
            guarded_repo.rename(displaced_repo)
            guarded_repo.symlink_to(replacement_repo, target_is_directory=True)
            raise RuntimeError("transient after first execute attempt")

    monkeypatch.setattr(
        terminal_module, "_create_environment", lambda **_kwargs: SwappingEnv()
    )
    raw = terminal_module.terminal_tool(
        f"touch {shlex.quote(marker.name)}",
        workdir=str(guarded_repo),
        task_id="task7-swap-retry",
        force=True,
    )

    result = json.loads(raw)
    assert result["exit_code"] == -1
    assert "transient after first execute attempt" in result["error"]
    assert execute_calls == 1
    assert not marker.exists()


def test_real_same_repo_terminal_threads_are_serial_and_never_lose_update(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo = _task7_git_repo(tmp_path, "same-repo-terminal-lane")
    counter = repo / "counter.txt"
    ready = repo / "first-ready"
    release = repo / "first-release"
    second_marker = repo / "second-ran"
    counter.write_text("0", encoding="utf-8")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    first_results: list[str] = []

    first_script = (
        "import pathlib,time; "
        f"counter=pathlib.Path({str(counter)!r}); "
        "value=int(counter.read_text()); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); "
        f"release=pathlib.Path({str(release)!r}); "
        "deadline=time.monotonic()+10; "
        "exec(\"while not release.exists() and time.monotonic() < deadline:\\n time.sleep(0.02)\"); "
        "assert release.exists(); counter.write_text(str(value+1))"
    )
    second_script = (
        "import pathlib; "
        f"counter=pathlib.Path({str(counter)!r}); "
        f"pathlib.Path({str(second_marker)!r}).write_text('ran'); "
        "counter.write_text(str(int(counter.read_text())+1))"
    )
    first_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(first_script)}"
    second_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(second_script)}"

    first = threading.Thread(
        target=lambda: first_results.append(
            terminal_module.terminal_tool(
                first_command,
                workdir=str(repo),
                task_id="task7-same-repo-first",
                force=True,
                timeout=15,
            )
        )
    )
    first.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()

    blocked = json.loads(
        terminal_module.terminal_tool(
            second_command,
            workdir=str(repo),
            task_id="task7-same-repo-blocked",
            force=True,
        )
    )
    assert blocked == {
        "status": "blocked",
        "error": "repo_terminal_guard_denied",
        "code": "repo_busy",
    }
    assert not second_marker.exists()
    assert counter.read_text(encoding="utf-8") == "0"

    release.write_text("release", encoding="utf-8")
    first.join(timeout=15)
    assert not first.is_alive()
    assert json.loads(first_results[0])["exit_code"] == 0
    assert counter.read_text(encoding="utf-8") == "1"

    successor = json.loads(
        terminal_module.terminal_tool(
            second_command,
            workdir=str(repo),
            task_id="task7-same-repo-successor",
            force=True,
        )
    )
    assert successor["exit_code"] == 0
    assert second_marker.read_text(encoding="utf-8") == "ran"
    assert counter.read_text(encoding="utf-8") == "2"


def test_different_repo_terminal_threads_can_execute_concurrently(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    repo_a = _task7_git_repo(tmp_path, "parallel-repo-a")
    repo_b = _task7_git_repo(tmp_path, "parallel-repo-b")
    _task7_isolate_terminal(monkeypatch, terminal_module, repo_a)
    both_entered = threading.Barrier(3)
    allow_exit = threading.Event()
    outcomes: list[str] = []

    class ParallelEnv:
        env = {}

        def __init__(self, cwd):
            self.cwd = cwd

        def execute(self, _command, **_kwargs):
            both_entered.wait(timeout=10)
            assert allow_exit.wait(timeout=10)
            return {"output": "parallel", "returncode": 0}

    monkeypatch.setattr(
        terminal_module,
        "_create_environment",
        lambda **kwargs: ParallelEnv(kwargs["cwd"]),
    )

    def run(repo, task):
        outcomes.append(
            terminal_module.terminal_tool(
                "printf parallel", workdir=str(repo), task_id=task, force=True
            )
        )

    first = threading.Thread(target=run, args=(repo_a, "task7-parallel-a"))
    second = threading.Thread(target=run, args=(repo_b, "task7-parallel-b"))
    first.start()
    second.start()
    both_entered.wait(timeout=10)
    allow_exit.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(json.loads(raw)["output"] for raw in outcomes) == [
        "parallel",
        "parallel",
    ]


@pytest.mark.parametrize("environment_kind", ["fake", "real"])
def test_guarded_terminal_postprocess_failure_never_replays_mutation(
    tmp_path, monkeypatch, environment_kind
):
    import tools.terminal_tool as terminal_module
    from tools.environments.local import LocalEnvironment

    repo = _task7_git_repo(tmp_path, f"postprocess-{environment_kind}")
    marker = repo / "mutation-marker"
    _task7_isolate_terminal(monkeypatch, terminal_module, repo)
    calls = 0

    if environment_kind == "fake":

        class PostprocessFailureEnv:
            cwd = str(repo)
            env = {}

            def execute(self, _command, **_kwargs):
                nonlocal calls
                calls += 1
                with marker.open("a", encoding="utf-8") as stream:
                    stream.write("mutation\n")
                raise RuntimeError("postprocess failed after mutation")

        environment = PostprocessFailureEnv()
    else:
        environment = LocalEnvironment(cwd=str(repo), timeout=15)
        real_execute = environment.execute

        def execute_then_fail(command, **kwargs):
            nonlocal calls
            calls += 1
            execution_result = real_execute(command, **kwargs)
            assert execution_result["returncode"] == 0
            raise RuntimeError("postprocess failed after real mutation")

        monkeypatch.setattr(environment, "execute", execute_then_fail)

    monkeypatch.setattr(
        terminal_module, "_create_environment", lambda **_kwargs: environment
    )
    command = (
        "printf 'mutation\\n' >> " + shlex.quote(str(marker))
        if environment_kind == "real"
        else "printf fake"
    )
    result = json.loads(
        terminal_module.terminal_tool(
            command,
            workdir=str(repo),
            task_id=f"task7-postprocess-{environment_kind}",
            force=True,
        )
    )

    assert result["exit_code"] == -1
    assert "postprocess failed" in result["error"]
    assert calls == 1
    assert marker.read_text(encoding="utf-8") == "mutation\n"
