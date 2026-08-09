#!/usr/bin/env python3
"""Offline proof for default-off and temp-only repository single-writer mode."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_POPEN_SIGNATURE = inspect.signature(subprocess.Popen)

CONTRACT = {
    "absent_config_mode_off": True,
    "canonical_temp_root": True,
    "config_file_absent_before_enable": True,
    "explicit_temp_config_single_writer": True,
    "first_task_fake_spawned": True,
    "gateway_restarted": False,
    "graphify_run": False,
    "git_isolation_enforced": True,
    "hostile_git_hook_suppressed": True,
    "imports_created_artifacts": False,
    "imports_spawned_process": False,
    "kanban_env_isolated": True,
    "live_board_mutated": False,
    "live_config_mutated": False,
    "lock_authority_free_after_release": True,
    "lock_contender_acquired_after_release": True,
    "lock_contender_busy_while_held": True,
    "lock_leaf_persists_after_release": True,
    "lock_probe_subprocesses_recorded": True,
    "network_called": False,
    "network_used": False,
    "process_argv_cwd_allowlist_clean": True,
    "reclaim_signal_calls_empty": True,
    "reclaimed_tasks_ready": True,
    "reclaims_returned_true": True,
    "repo_busy_consecutive_failures_zero": True,
    "repo_busy_event_payload_readback": True,
    "repo_writer_marker_cleared": True,
    "second_task_repo_busy": True,
    "staged_committed_pushed": False,
    "temp_board_created_after_enable": True,
    "temp_only_mutation": True,
    "worker_process_spawned": False,
}

REQUIRED_FALSE_KEYS = {
    "gateway_restarted",
    "graphify_run",
    "imports_created_artifacts",
    "imports_spawned_process",
    "live_board_mutated",
    "live_config_mutated",
    "network_called",
    "network_used",
    "staged_committed_pushed",
    "worker_process_spawned",
}
assert {key for key, value in CONTRACT.items() if value is False} == REQUIRED_FALSE_KEYS


def _failure_result() -> dict[str, bool]:
    result = {key: not expected for key, expected in CONTRACT.items()}
    # These actions are never present in this source-only smoke.
    result["gateway_restarted"] = False
    result["graphify_run"] = False
    return result


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, bytes | str | None], ...]:
    """Snapshot path, type/mode, and payload without following symlinks."""
    entries: list[tuple[str, int, bytes | str | None]] = []
    for path in sorted((root, *root.rglob("*")), key=os.fspath):
        info = path.lstat()
        payload: bytes | str | None = None
        if stat.S_ISREG(info.st_mode):
            payload = path.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path)
        entries.append((str(path.relative_to(root)), info.st_mode, payload))
    return tuple(entries)


def _is_canonical_temp_root(temp_root: Path) -> bool:
    """Require an absolute, real path whose existing chain has no symlinks."""
    try:
        if (
            not temp_root.is_absolute()
            or Path(os.path.realpath(temp_root)) != temp_root
        ):
            return False
        return all(
            not stat.S_ISLNK(path.lstat().st_mode)
            for path in (*reversed(temp_root.parents), temp_root)
        )
    except OSError:
        return False


@dataclass(frozen=True)
class ProcessDescriptor:
    argv: tuple[str, ...]
    cwd: Path
    executable: Path
    environment_digest: str
    options: tuple[tuple[str, Any], ...]


ObservedProcess = ProcessDescriptor | None
ExpectedProcess = tuple[str, ProcessDescriptor]


def _environment_digest(environment: Any) -> str | None:
    """Hash a string environment without retaining keys or values."""
    if not isinstance(environment, Mapping):
        return None
    try:
        pairs = tuple(environment.items())
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in pairs
        ):
            return None
        digest = hashlib.sha256()
        digest.update(len(pairs).to_bytes(8, "big"))
        for key, value in sorted(pairs):
            for item in (key, value):
                encoded = item.encode("utf-8", "surrogateescape")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return digest.hexdigest()
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return None


def _canonical_executable(
    requested: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> Path | None:
    """Resolve the executable exactly as the child PATH/cwd would resolve it."""
    if not requested or "\x00" in requested:
        return None
    separators = (os.sep,) if os.altsep is None else (os.sep, os.altsep)
    if any(separator in requested for separator in separators):
        candidates = (Path(requested),)
    else:
        path_value = environment.get("PATH", os.defpath)
        if not isinstance(path_value, str):
            return None
        candidates = tuple(
            (cwd if entry == "" else Path(entry)) / requested
            for entry in path_value.split(os.pathsep)
        )
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _safe_process_options(bound: inspect.BoundArguments) -> tuple[tuple[str, Any], ...] | None:
    """Bind harmless options exactly and reject process-control variation."""
    arguments = bound.arguments
    if (
        arguments["shell"] is not False
        or arguments["preexec_fn"] is not None
        or arguments["startupinfo"] is not None
        or arguments["creationflags"] != 0
        or arguments["start_new_session"] is not False
        or arguments["user"] is not None
        or arguments["group"] is not None
        or arguments["extra_groups"] is not None
        or arguments["process_group"] is not None
    ):
        return None

    integer_options = ("bufsize", "umask", "pipesize")
    if any(
        not isinstance(arguments[name], int) or isinstance(arguments[name], bool)
        for name in integer_options
    ):
        return None
    for name in ("stdin", "stdout", "stderr"):
        value = arguments[name]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            return None
    if (
        not isinstance(arguments["close_fds"], bool)
        or not isinstance(arguments["restore_signals"], bool)
        or (
            arguments["universal_newlines"] is not None
            and not isinstance(arguments["universal_newlines"], bool)
        )
        or (
            arguments["text"] is not None
            and not isinstance(arguments["text"], bool)
        )
    ):
        return None
    pass_fds = arguments["pass_fds"]
    if not isinstance(pass_fds, tuple) or any(
        not isinstance(fd, int) or isinstance(fd, bool) for fd in pass_fds
    ):
        return None
    for name in ("encoding", "errors"):
        if arguments[name] is not None and not isinstance(arguments[name], str):
            return None

    names = (
        "bufsize", "stdin", "stdout", "stderr", "close_fds",
        "universal_newlines", "restore_signals", "pass_fds", "encoding",
        "errors", "text", "umask", "pipesize",
    )
    return tuple((name, arguments[name]) for name in names)


def _minimal_environment(
    source: dict[str, str],
    *,
    overrides: dict[str, str],
) -> dict[str, str]:
    """Build a credential-free environment for the smoke and its children."""
    env = {
        key: source[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
        if source.get(key)
    }
    env.update(overrides)
    return env


def _process_descriptor(
    popen_args: tuple[Any, ...],
    popen_kwargs: dict[str, Any],
) -> ProcessDescriptor | None:
    """Normalize the complete effective process without retaining its environment."""
    try:
        bound = _POPEN_SIGNATURE.bind(*popen_args, **popen_kwargs)
        bound.apply_defaults()
    except TypeError:
        return None
    command = bound.arguments["args"]
    if not isinstance(command, (list, tuple)) or not command:
        return None
    if not all(isinstance(part, str) for part in command):
        return None
    argv = tuple(command)
    cwd_value = bound.arguments["cwd"]
    try:
        cwd = (
            Path.cwd().resolve(strict=True)
            if cwd_value is None
            else Path(cwd_value).resolve(strict=True)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    supplied_environment = bound.arguments["env"]
    effective_environment = (
        os.environ if supplied_environment is None else supplied_environment
    )
    environment_digest = _environment_digest(effective_environment)
    if environment_digest is None or not isinstance(effective_environment, Mapping):
        return None
    executable_value = bound.arguments["executable"]
    requested_executable = argv[0] if executable_value is None else executable_value
    if not isinstance(requested_executable, str):
        return None
    executable = _canonical_executable(
        requested_executable,
        cwd=cwd,
        environment=effective_environment,
    )
    options = _safe_process_options(bound)
    if executable is None or options is None:
        return None
    return ProcessDescriptor(
        argv=argv,
        cwd=cwd,
        executable=executable,
        environment_digest=environment_digest,
        options=options,
    )


def _register_expected(
    expected: list[ExpectedProcess],
    kind: str,
    argv: tuple[str, ...],
    cwd: Path,
    *,
    env: Mapping[str, str],
    executable: str | None = None,
    **process_options: Any,
) -> None:
    descriptor = _process_descriptor(
        (),
        {
            "args": argv,
            "cwd": cwd,
            "env": env,
            "executable": executable,
            **process_options,
        },
    )
    if descriptor is None:
        raise ValueError("invalid expected process descriptor")
    expected.append((kind, descriptor))


def _git_verb(
    descriptor: ProcessDescriptor | None,
    *,
    git_binary: Path,
) -> str | None:
    if descriptor is None:
        return None
    if descriptor.executable != git_binary:
        return None
    argv = descriptor.argv
    index = 1
    while index < len(argv) and argv[index] == "-c":
        index += 2
    return argv[index] if index < len(argv) else None


def _close_process_audit(
    observed: list[ObservedProcess],
    expected: list[ExpectedProcess],
    *,
    git_binary: Path,
    allowed_fixture_descriptors: frozenset[ProcessDescriptor],
) -> tuple[bool, bool, bool]:
    """Require one-to-one exact calls and an independent fixture allowlist."""
    unused = set(range(len(observed)))
    matched_fixture: set[int] = set()
    missing_expected = False
    for kind, descriptor in expected:
        if kind == "fixture-git" and descriptor not in allowed_fixture_descriptors:
            missing_expected = True
            continue
        match = next(
            (index for index in sorted(unused) if observed[index] == descriptor),
            None,
        )
        if match is None:
            missing_expected = True
            continue
        unused.remove(match)
        if kind == "fixture-git":
            matched_fixture.add(match)

    unknown = set(unused)
    mutation_verbs = {
        "add", "commit", "push", "reset", "stash", "checkout", "clean",
    }
    staged_committed_pushed = any(
        index not in matched_fixture
        and _git_verb(descriptor, git_binary=git_binary) in mutation_verbs
        for index, descriptor in enumerate(observed)
    )
    allowlist_clean = not missing_expected and not unknown
    return allowlist_clean, bool(unknown), staged_committed_pushed


_FIXTURE_GIT_RAW_ARGS = (
    ("init", "-q"),
    ("config", "user.name", "Hermes Smoke"),
    ("config", "user.email", "hermes-smoke@example.invalid"),
    ("add", "seed.txt"),
    ("commit", "-qm", "seed"),
)
_IDENTITY_READ_ARGV = (
    "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
)
_EXPECTED_IDENTITY_READ_COUNT = 6


def _fixture_git_command(
    raw_args: tuple[str, ...],
    *,
    git_binary: Path,
    hooks_dir: Path,
    template_dir: Path,
) -> tuple[str, ...]:
    if raw_args not in _FIXTURE_GIT_RAW_ARGS:
        raise ValueError("fixture git arguments not allowed")
    fixture_args = list(raw_args)
    if fixture_args[0] == "init":
        fixture_args.insert(1, f"--template={template_dir}")
    return (
        str(git_binary),
        "-c", f"core.hooksPath={hooks_dir}",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "credential.helper=",
        *fixture_args,
    )


def _allowed_fixture_descriptors(
    *,
    repository: Path,
    git_binary: Path,
    git_env: Mapping[str, str],
    hooks_dir: Path,
    template_dir: Path,
) -> frozenset[ProcessDescriptor]:
    """Build the fixed fixture allowlist independently of observed labels."""
    allowed: list[ExpectedProcess] = []
    for raw_args in _FIXTURE_GIT_RAW_ARGS:
        command = _fixture_git_command(
            raw_args,
            git_binary=git_binary,
            hooks_dir=hooks_dir,
            template_dir=template_dir,
        )
        _register_expected(
            allowed,
            "allowed-fixture-git",
            command,
            repository,
            env=git_env,
            executable=str(git_binary),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return frozenset(descriptor for _kind, descriptor in allowed)


def _git(
    repository: Path,
    *args: str,
    expected: list[ExpectedProcess],
    git_binary: Path,
    git_env: dict[str, str],
    hooks_dir: Path,
    template_dir: Path,
) -> None:
    command = _fixture_git_command(
        tuple(args),
        git_binary=git_binary,
        hooks_dir=hooks_dir,
        template_dir=template_dir,
    )
    _register_expected(
        expected,
        "fixture-git",
        command,
        repository,
        env=git_env,
        executable=str(git_binary),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        command,
        cwd=repository,
        env=git_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _contend(
    source_root: Path,
    repository: Path,
    env: dict[str, str],
    probes: list[tuple[str, ...]],
    expected: list[ExpectedProcess],
) -> str:
    code = """
import sys
from hermes_cli.repo_write_lock import RepoLockBusy, RepoWriteLock
try:
    with RepoWriteLock(sys.argv[1], blocking=False):
        print("acquired")
except RepoLockBusy:
    print("busy")
"""
    command = (sys.executable, "-c", code, str(repository))
    probes.append(command)
    _register_expected(
        expected,
        "lock-probe",
        command,
        source_root,
        env=env,
        executable=sys.executable,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc = subprocess.run(
        command,
        cwd=source_root,
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


def _run_smoke() -> dict[str, bool]:
    source_root = Path(__file__).resolve().parents[1]
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    sys.dont_write_bytecode = True

    ambient_env = dict(os.environ)
    git_found = shutil.which("git", path=ambient_env.get("PATH"))
    if git_found is None:
        raise RuntimeError("git executable unavailable")
    git_binary = Path(git_found).resolve(strict=True)
    network_attempts: list[Any] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection
    original_popen = subprocess.Popen
    observed_processes: list[ObservedProcess] = []
    expected_processes: list[ExpectedProcess] = []

    def deny_connect(*args: Any, **kwargs: Any) -> Any:
        network_attempts.append((args, kwargs))
        raise AssertionError("network denied by no-live smoke")

    socket.socket.connect = deny_connect
    socket.create_connection = deny_connect
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-repo-single-writer-") as td:
            temp_root = Path(td).resolve(strict=True)
            temp_home = temp_root / "home"
            repository = temp_root / "repository"
            git_home = temp_root / "git-home"
            xdg_home = temp_root / "xdg"
            hooks_dir = temp_root / "empty-hooks"
            template_dir = temp_root / "empty-template"
            config_dir = temp_root / "git-config"
            git_global = config_dir / "global"
            git_system = config_dir / "system"
            for directory in (
                temp_home, git_home, xdg_home, hooks_dir, template_dir, config_dir,
            ):
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
            for config_file in (git_global, git_system):
                config_file.write_bytes(b"")
                config_file.chmod(0o600)

            git_overrides = {
                "HOME": str(git_home),
                "XDG_CONFIG_HOME": str(xdg_home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(git_global),
                "GIT_CONFIG_SYSTEM": str(git_system),
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS": "/usr/bin/false",
            }
            runtime_env = _minimal_environment(
                ambient_env,
                overrides={
                    **git_overrides,
                    "HERMES_HOME": str(temp_home),
                    "HERMES_KANBAN_HOME": str(temp_home),
                    "HERMES_MANAGED_DIR": str(temp_root / "managed-absent"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            os.environ.clear()
            os.environ.update(runtime_env)
            git_env = dict(runtime_env)

            isolated = (
                os.environ.get("HERMES_HOME") == str(temp_home)
                and os.environ.get("HERMES_KANBAN_HOME") == str(temp_home)
                and os.environ.get("HOME") == str(git_home)
                and os.environ.get("XDG_CONFIG_HOME") == str(xdg_home)
                and all(
                    name not in os.environ
                    for name in (
                        "HERMES_KANBAN_DB",
                        "HERMES_KANBAN_BOARD",
                        "HERMES_KANBAN_WORKSPACES_ROOT",
                    )
                )
            )
            marker_cleared = "HERMES_KANBAN_REPO_WRITER" not in os.environ
            canonical_temp = _is_canonical_temp_root(temp_root)
            safe_dirs = (git_home, xdg_home, hooks_dir, template_dir, config_dir)
            git_isolation_enforced = (
                git_binary.is_absolute()
                and all(
                    path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700
                    for path in safe_dirs
                )
                and all(
                    path.is_file()
                    and path.read_bytes() == b""
                    and stat.S_IMODE(path.stat().st_mode) == 0o600
                    for path in (git_global, git_system)
                )
                and all(os.environ.get(key) == value for key, value in git_overrides.items())
            )
            hostile_git_hook_suppressed = (
                git_isolation_enforced and not any(hooks_dir.iterdir())
            )

            before_import = _tree_snapshot(temp_root)
            popen_calls: list[Any] = []

            def deny_popen(*args: Any, **kwargs: Any) -> Any:
                popen_calls.append((args, kwargs))
                raise AssertionError("process spawn denied during pre-enable imports")

            subprocess.Popen = deny_popen
            try:
                from hermes_cli import config as hermes_config
                from hermes_cli import kanban_db as kb
                from hermes_cli import profiles
                from hermes_cli import repo_write_lock as rwl
            finally:
                subprocess.Popen = original_popen
            after_import = _tree_snapshot(temp_root)

            def recording_popen(*args: Any, **kwargs: Any) -> Any:
                observed_processes.append(_process_descriptor(args, kwargs))
                return original_popen(*args, **kwargs)

            # Global post-import audit: every later direct child is recorded.
            subprocess.Popen = recording_popen

            default_mode_off = (
                hermes_config.DEFAULT_CONFIG.get("kanban", {}).get(
                    "repo_writer_mode"
                )
                == "off"
            )
            loaded_without_file = hermes_config.load_config()
            absent_mode_off = (
                default_mode_off
                and loaded_without_file.get("kanban", {}).get("repo_writer_mode")
                == "off"
                and kb._repo_writer_mode() == "off"
            )
            config_path = temp_home / "config.yaml"
            board_path = kb.kanban_db_path(board="default")
            lock_root = temp_home / "kanban" / "repo-locks"
            absent_before_enable = (
                not config_path.exists()
                and not board_path.exists()
                and not lock_root.exists()
                and not any(temp_home.rglob("*.db*"))
            )

            config_path.write_text(
                '{"kanban":{"repo_writer_mode":"single_writer",'
                '"dispatch_in_gateway":false}}\n',
                encoding="utf-8",
            )
            explicit_mode = (
                hermes_config.load_config().get("kanban", {}).get(
                    "repo_writer_mode"
                )
                == "single_writer"
                and kb._repo_writer_mode() == "single_writer"
            )

            repository.mkdir()
            git_fixture = {
                "expected": expected_processes,
                "git_binary": git_binary,
                "git_env": git_env,
                "hooks_dir": hooks_dir,
                "template_dir": template_dir,
            }
            allowed_fixture_descriptors = _allowed_fixture_descriptors(
                repository=repository,
                git_binary=git_binary,
                git_env=git_env,
                hooks_dir=hooks_dir,
                template_dir=template_dir,
            )
            _git(repository, "init", "-q", **git_fixture)
            _git(
                repository,
                "config",
                "user.name",
                "Hermes Smoke",
                **git_fixture,
            )
            _git(
                repository,
                "config",
                "user.email",
                "hermes-smoke@example.invalid",
                **git_fixture,
            )
            (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
            _git(repository, "add", "seed.txt", **git_fixture)
            _git(repository, "commit", "-qm", "seed", **git_fixture)

            identity_env = rwl._git_environment()
            for _ in range(_EXPECTED_IDENTITY_READ_COUNT):
                _register_expected(
                    expected_processes,
                    "identity-read",
                    _IDENTITY_READ_ARGV,
                    repository,
                    env=identity_env,
                    executable=str(git_binary),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

            board_absent_until_enable = not board_path.exists()
            kb._INITIALIZED_PATHS.discard(str(board_path.resolve()))
            kb.init_db(db_path=board_path)
            board_created_after_enable = board_absent_until_enable and board_path.is_file()

            original_profile_exists = profiles.profile_exists
            profiles.profile_exists = lambda _name: True
            fake_spawn_calls: list[tuple[str, str, str | None]] = []
            synthetic_pid = 2_000_000_123
            signal_calls: list[tuple[Any, ...]] = []
            reclaim_results: list[bool] = []
            try:
                with kb.connect(db_path=board_path) as conn:
                    first = kb.create_task(
                        conn,
                        title="first temp repository writer",
                        assignee="smoke-worker",
                        workspace_kind="dir",
                        workspace_path=str(repository),
                        priority=2,
                        initial_status="running",
                    )
                    second = kb.create_task(
                        conn,
                        title="second temp repository writer",
                        assignee="smoke-worker",
                        workspace_kind="dir",
                        workspace_path=str(repository),
                        priority=1,
                        initial_status="running",
                    )

                    def recording_signal(*args: Any) -> None:
                        signal_calls.append(args)

                    for task_id in (first, second):
                        claimed = kb.claim_task(
                            conn,
                            task_id,
                            claimer=f"offline-smoke:{task_id}",
                        )
                        assert claimed is not None
                        running = kb.get_task(conn, task_id)
                        assert running is not None
                        assert running.status == "running"
                        assert running.worker_pid is None
                        reclaim_results.append(
                            kb.reclaim_task(
                                conn,
                                task_id,
                                reason="offline smoke queue setup",
                                signal_fn=recording_signal,
                            )
                        )
                    reclaimed_tasks_ready = all(
                        (task := kb.get_task(conn, task_id)) is not None
                        and task.status == "ready"
                        and task.worker_pid is None
                        for task_id in (first, second)
                    )

                    def fake_spawn(task: Any, workspace: str, board: str | None = None) -> int:
                        fake_spawn_calls.append((task.id, workspace, board))
                        return synthetic_pid

                    dispatch = kb.dispatch_once(
                        conn,
                        board="default",
                        spawn_fn=fake_spawn,
                    )
                    identity = rwl.repo_identity(repository)
                    event = conn.execute(
                        "SELECT payload FROM task_events "
                        "WHERE task_id = ? AND kind = 'repo_busy' "
                        "ORDER BY id DESC LIMIT 1",
                        (second,),
                    ).fetchone()
                    second_after = kb.get_task(conn, second)
                    first_fake_spawned = (
                        fake_spawn_calls == [(first, str(repository), "default")]
                        and [item[0] for item in dispatch.spawned] == [first]
                    )
                    second_busy = dispatch.skipped_repo_busy == [(second, identity)]
                    event_readback = (
                        event is not None
                        and json.loads(event["payload"]) == {
                            "repo_identity": identity
                        }
                    )
                    failures_zero = (
                        second_after is not None
                        and second_after.status == "ready"
                        and second_after.claim_lock is None
                        and second_after.consecutive_failures == 0
                    )
            finally:
                profiles.profile_exists = original_profile_exists

            lock = rwl.RepoWriteLock(repository, blocking=False)
            lock.acquire()
            leaf = rwl._lock_root() / f"{lock.identity}.lock"
            contender_env = os.environ.copy()
            lock_probes: list[tuple[str, ...]] = []
            busy_while_held = _contend(
                source_root,
                repository,
                contender_env,
                lock_probes,
                expected_processes,
            ) == "busy"
            lock.release()
            leaf_persists = leaf.is_file()
            acquired_after_release = _contend(
                source_root,
                repository,
                contender_env,
                lock_probes,
                expected_processes,
            ) == "acquired"

            dispatch_leaf = Path(f"{board_path}.dispatch.lock")
            generated_paths = (
                config_path,
                board_path,
                dispatch_leaf,
                leaf,
                repository,
            )
            temp_only = all(
                path.resolve(strict=False).is_relative_to(temp_root)
                for path in generated_paths
            )
            original_path_env_values_unused = all(
                value is None or os.environ.get(name) != value
                for name in (
                    "HOME",
                    "HERMES_HOME",
                    "HERMES_KANBAN_HOME",
                    "HERMES_MANAGED_DIR",
                    "HERMES_KANBAN_DB",
                    "HERMES_KANBAN_BOARD",
                    "HERMES_KANBAN_WORKSPACES_ROOT",
                )
                if (value := ambient_env.get(name)) is not None
            )
            live_config_mutated = not (
                config_path.resolve(strict=False).is_relative_to(temp_root)
                and original_path_env_values_unused
            )
            live_board_mutated = not (
                all(
                    path.resolve(strict=False).is_relative_to(temp_root)
                    for path in (board_path, dispatch_leaf, leaf)
                )
                and original_path_env_values_unused
            )
            network_called = bool(network_attempts)
            (
                process_argv_cwd_allowlist_clean,
                worker_process_spawned,
                staged_committed_pushed,
            ) = _close_process_audit(
                observed_processes,
                expected_processes,
                git_binary=git_binary,
                allowed_fixture_descriptors=allowed_fixture_descriptors,
            )

            return {
                "absent_config_mode_off": absent_mode_off,
                "canonical_temp_root": canonical_temp,
                "config_file_absent_before_enable": absent_before_enable,
                "explicit_temp_config_single_writer": explicit_mode,
                "first_task_fake_spawned": first_fake_spawned,
                "gateway_restarted": False,
                "graphify_run": False,
                "git_isolation_enforced": git_isolation_enforced,
                "hostile_git_hook_suppressed": hostile_git_hook_suppressed,
                "imports_created_artifacts": before_import != after_import,
                "imports_spawned_process": bool(popen_calls),
                "kanban_env_isolated": isolated,
                "live_board_mutated": live_board_mutated,
                "live_config_mutated": live_config_mutated,
                "lock_authority_free_after_release": acquired_after_release,
                "lock_contender_acquired_after_release": acquired_after_release,
                "lock_contender_busy_while_held": busy_while_held,
                "lock_leaf_persists_after_release": leaf_persists,
                "lock_probe_subprocesses_recorded": len(lock_probes) == 2,
                "network_called": network_called,
                "network_used": network_called,
                "process_argv_cwd_allowlist_clean": process_argv_cwd_allowlist_clean,
                "reclaim_signal_calls_empty": signal_calls == [],
                "reclaimed_tasks_ready": reclaimed_tasks_ready,
                "reclaims_returned_true": reclaim_results == [True, True],
                "repo_busy_consecutive_failures_zero": failures_zero,
                "repo_busy_event_payload_readback": event_readback,
                "repo_writer_marker_cleared": marker_cleared,
                "second_task_repo_busy": second_busy,
                "staged_committed_pushed": staged_committed_pushed,
                "temp_board_created_after_enable": board_created_after_enable,
                "temp_only_mutation": temp_only,
                "worker_process_spawned": worker_process_spawned,
            }
    finally:
        subprocess.Popen = original_popen
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection
        os.environ.clear()
        os.environ.update(ambient_env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = _run_smoke()
    except Exception as exc:
        result = _failure_result()
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    passed = result == CONTRACT
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
