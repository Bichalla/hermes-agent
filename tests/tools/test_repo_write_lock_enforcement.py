"""Task 5 contracts for repository-writer tool capability closure."""

from __future__ import annotations

import dis
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_tools
from hermes_cli import repo_writer_context
from toolsets import TOOLSETS


REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING = (
    "repo_writer_tool_capability_contract_missing"
)
_REPO_WRITER_ENV = "HERMES_KANBAN_REPO_WRITER"
_BOOTSTRAP_ENV = "HERMES_KANBAN_REPO_LOCK_BOOTSTRAP"
_BLOCKED_TOOL_NAMES = {"computer_use", "execute_code"}
_MAIN_IMPORT_RESULT_PREFIX = "TASK5_MAIN_IMPORT_RESULT="


def _isolated_main_package(tmp_path: Path) -> Path:
    """Copy only main's package shell so its real project dotenv is temporary."""
    source_root = Path(__file__).parents[2]
    project_root = tmp_path / "project"
    package = project_root / "hermes_cli"
    package.mkdir(parents=True)
    init_source = source_root / "hermes_cli" / "__init__.py"
    package.joinpath("__init__.py").write_text(
        init_source.read_text(encoding="utf-8")
        + f"\n__path__.append({str(source_root / 'hermes_cli')!r})\n",
        encoding="utf-8",
    )
    package.joinpath("main.py").write_text(
        source_root.joinpath("hermes_cli", "main.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return project_root


def _run_main_import(
    tmp_path: Path,
    *,
    dotenv_source: str,
    dotenv_value: str,
    inherited_writer: str | None,
    check_capabilities: bool = False,
    repeat_imports: bool = False,
) -> dict:
    root = tmp_path / "hermes-home"
    root.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    argv: list[str] = []
    cwd = Path(__file__).parents[2]

    if dotenv_source == "profile":
        profile = root / "profiles" / "writer"
        profile.mkdir(parents=True)
        profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
        dotenv_path = profile / ".env"
        argv = ["-p", "writer"]
    elif dotenv_source == "root":
        dotenv_path = root / ".env"
    elif dotenv_source == "managed":
        managed = tmp_path / "managed"
        managed.mkdir()
        dotenv_path = managed / ".env"
    elif dotenv_source == "project":
        cwd = _isolated_main_package(tmp_path)
        dotenv_path = cwd / ".env"
    else:
        raise AssertionError(f"unknown dotenv source: {dotenv_source}")

    dotenv_path.write_text(f"{_REPO_WRITER_ENV}={dotenv_value}\n", encoding="utf-8")
    source_root = Path(__file__).parents[2]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(root)
    env.pop(_BOOTSTRAP_ENV, None)
    env.pop("HERMES_MANAGED_DIR", None)
    if inherited_writer is None:
        env.pop(_REPO_WRITER_ENV, None)
    else:
        env[_REPO_WRITER_ENV] = inherited_writer
    if dotenv_source == "managed":
        env["HERMES_MANAGED_DIR"] = str(dotenv_path.parent)
    if dotenv_source == "project":
        env["PYTHONPATH"] = os.pathsep.join(
            [str(cwd), str(source_root), env.get("PYTHONPATH", "")]
        )

    capability_probe = ""
    if check_capabilities:
        capability_probe = f"""
import model_tools
from tools.terminal_tool import terminal_tool
names = {{item[\"function\"][\"name\"] for item in model_tools.get_tool_definitions(
    enabled_toolsets=[\"hermes-cli\", \"kanban\"], quiet_mode=False
)}}
result[\"blocked_model_tools\"] = sorted(names & {_BLOCKED_TOOL_NAMES!r})
result[\"background\"] = json.loads(terminal_tool(
    \"printf should-not-run\", background=True
))
"""
    repeated_import_probe = ""
    if repeat_imports:
        repeated_import_probe = f"""
result[\"markers_after_imports\"] = [os.environ.get({_REPO_WRITER_ENV!r})]
import cli
result[\"markers_after_imports\"].append(os.environ.get({_REPO_WRITER_ENV!r}))
import run_agent
result[\"markers_after_imports\"].append(os.environ.get({_REPO_WRITER_ENV!r}))
"""
    script = f"""
import json
import os
import hermes_cli.main
result = {{\"marker\": os.environ.get({_REPO_WRITER_ENV!r})}}
{capability_probe}
{repeated_import_probe}
print({_MAIN_IMPORT_RESULT_PREFIX!r} + json.dumps(result, sort_keys=True), flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING,
        completed.stdout,
        completed.stderr,
    )
    result_lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith(_MAIN_IMPORT_RESULT_PREFIX)
    ]
    assert len(result_lines) == 1, (completed.stdout, completed.stderr)
    return json.loads(result_lines[0][len(_MAIN_IMPORT_RESULT_PREFIX) :])


@pytest.fixture(autouse=True)
def _isolate_writer_context(monkeypatch):
    monkeypatch.delenv(_REPO_WRITER_ENV, raising=False)
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", False)
    model_tools._clear_tool_defs_cache()
    yield
    model_tools._clear_tool_defs_cache()


@pytest.mark.parametrize("dotenv_value", ["0", ""], ids=["zero", "empty"])
def test_trusted_writer_survives_selected_profile_dotenv_override(
    tmp_path, dotenv_value
):
    result = _run_main_import(
        tmp_path,
        dotenv_source="profile",
        dotenv_value=dotenv_value,
        inherited_writer="1",
    )

    assert result["marker"] == "1", REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING


@pytest.mark.parametrize(
    ("dotenv_source", "dotenv_value"),
    [("root", "0"), ("project", ""), ("managed", "0")],
)
def test_trusted_writer_survives_other_real_dotenv_precedence_sources(
    tmp_path, dotenv_source, dotenv_value
):
    result = _run_main_import(
        tmp_path,
        dotenv_source=dotenv_source,
        dotenv_value=dotenv_value,
        inherited_writer="1",
    )

    assert result["marker"] == "1", REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING


@pytest.mark.parametrize(
    ("dotenv_source", "inherited_writer"),
    [
        ("profile", None),
        ("root", "true"),
        ("project", None),
        ("managed", "01"),
    ],
)
def test_dotenv_cannot_activate_absent_or_nonexact_writer_context(
    tmp_path, dotenv_source, inherited_writer
):
    result = _run_main_import(
        tmp_path,
        dotenv_source=dotenv_source,
        dotenv_value="1",
        inherited_writer=inherited_writer,
    )

    assert result["marker"] is None, REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING


def test_nested_writer_descendant_survives_profile_dotenv_and_keeps_restrictions(
    tmp_path,
):
    result = _run_main_import(
        tmp_path,
        dotenv_source="profile",
        dotenv_value="0",
        inherited_writer="1",
        check_capabilities=True,
    )

    assert result["marker"] == "1", REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    assert result["blocked_model_tools"] == [], (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert result["background"]["status"] == "blocked", (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )


@pytest.mark.parametrize(
    ("inherited_writer", "dotenv_value", "expected"),
    [("1", "0", "1"), (None, "1", None), ("nonexact", "1", None)],
)
def test_repeated_entrypoint_imports_preserve_only_trusted_writer_context(
    tmp_path, inherited_writer, dotenv_value, expected
):
    result = _run_main_import(
        tmp_path,
        dotenv_source="profile",
        dotenv_value=dotenv_value,
        inherited_writer=inherited_writer,
        repeat_imports=True,
    )

    assert result["markers_after_imports"] == [expected, expected, expected], (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )


def _make_task(
    kb,
    *,
    workspace_kind: str = "dir",
    current_run_id: int | None = 1,
    claim_lock: str | None = "claim",
    repo_identity: str | None = None,
):
    return kb.Task(
        id="t_writer_tools",
        title="writer tools",
        body=None,
        assignee="writer",
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


def _capture_spawn_env(
    monkeypatch,
    tmp_path,
    *,
    mode: str,
    workspace_kind: str = "dir",
    current_run_id: int | None = 1,
    claim_lock: str | None = "claim",
    repo_identity: str | None = None,
):
    from hermes_cli import kanban_db as kb

    root = tmp_path / "hermes-home"
    profile = root / "profiles" / "writer"
    profile.mkdir(parents=True, exist_ok=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: mode)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: root / "kanban.db")
    monkeypatch.setattr(
        kb, "workspaces_root", lambda board=None: root / "workspaces"
    )
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: root / "logs")

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / f"workspace-{workspace_kind}"
    workspace.mkdir()
    task = _make_task(
        kb,
        workspace_kind=workspace_kind,
        current_run_id=current_run_id,
        claim_lock=claim_lock,
        repo_identity=repo_identity,
    )
    kb._default_spawn(task, str(workspace))
    return captured["env"]


def test_default_spawn_sets_persistent_writer_context_only_with_full_authority(
    monkeypatch, tmp_path
):
    env = _capture_spawn_env(
        monkeypatch,
        tmp_path,
        mode="single_writer",
        repo_identity="a" * 64,
    )

    assert env[_BOOTSTRAP_ENV] == "1", (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert env[_REPO_WRITER_ENV] == "1", (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )


@pytest.mark.parametrize(
    ("mode", "workspace_kind"),
    [("off", "dir"), ("single_writer", "scratch")],
)
def test_default_spawn_clears_spoofed_writer_context_when_not_repo_writer(
    monkeypatch, tmp_path, mode, workspace_kind
):
    monkeypatch.setenv(_REPO_WRITER_ENV, "1")
    monkeypatch.setenv(_BOOTSTRAP_ENV, "1")

    env = _capture_spawn_env(
        monkeypatch,
        tmp_path,
        mode=mode,
        workspace_kind=workspace_kind,
        repo_identity="b" * 64,
    )

    assert _REPO_WRITER_ENV not in env, REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    assert _BOOTSTRAP_ENV not in env, REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING


def test_default_spawn_keeps_malformed_bootstrap_fail_closed(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    root = tmp_path / "hermes-home"
    (root / "profiles" / "writer").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kb, "_repo_writer_mode", lambda: "single_writer")

    with pytest.raises(RuntimeError, match="repo_worker_bootstrap_authority_invalid"):
        kb._default_spawn(
            _make_task(
                kb,
                current_run_id=None,
                repo_identity="c" * 64,
            ),
            str(tmp_path),
        )


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"test schema for {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _install_schema_stubs(monkeypatch):
    surface = {
        "computer_use",
        "execute_code",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "kanban_show",
    }
    observed: list[set[str]] = []

    def fake_get_definitions(tool_names, quiet=False):
        observed.append(set(tool_names))
        return [_tool_schema(name) for name in sorted(set(tool_names) & surface)]

    monkeypatch.setattr(model_tools.registry, "get_definitions", fake_get_definitions)
    from tools import code_execution_tool

    monkeypatch.setattr(
        code_execution_tool,
        "build_execute_code_schema",
        lambda _enabled, mode=None: _tool_schema("execute_code")["function"],
    )
    monkeypatch.setattr(code_execution_tool, "_get_execution_mode", lambda: "stateful")
    return observed


def _tool_names(definitions) -> set[str]:
    return {tool["function"]["name"] for tool in definitions}


@pytest.mark.parametrize(
    "enabled_toolsets",
    [None, ["hermes-cli", "kanban"], ["writer-capability-composite"]],
)
def test_writer_model_schema_excludes_blocked_exact_names_before_registry(
    monkeypatch, enabled_toolsets
):
    observed = _install_schema_stubs(monkeypatch)
    TOOLSETS["writer-capability-composite"] = {
        "description": "test-only writer capability composite",
        "tools": [],
        "includes": ["terminal", "file", "kanban", "computer_use", "code_execution"],
    }
    try:
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=enabled_toolsets,
            quiet_mode=False,
        )
    finally:
        TOOLSETS.pop("writer-capability-composite", None)

    names = _tool_names(definitions)
    assert not (names & _BLOCKED_TOOL_NAMES), (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert not (observed[-1] & _BLOCKED_TOOL_NAMES), (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert {"terminal", "read_file", "kanban_show"}.issubset(names)


def test_normal_model_schema_retains_available_blocked_tools(monkeypatch):
    _install_schema_stubs(monkeypatch)

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli", "kanban"],
        quiet_mode=False,
    )

    assert _BLOCKED_TOOL_NAMES.issubset(_tool_names(definitions))


def test_non_exact_writer_context_value_does_not_restrict_model_schema(monkeypatch):
    _install_schema_stubs(monkeypatch)
    monkeypatch.setenv(_REPO_WRITER_ENV, "true")

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli", "kanban"],
        quiet_mode=False,
    )

    assert _BLOCKED_TOOL_NAMES.issubset(_tool_names(definitions))


def test_quiet_schema_cache_isolated_across_writer_context_toggle(monkeypatch):
    _install_schema_stubs(monkeypatch)

    normal_before = _tool_names(
        model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-cli", "kanban"], quiet_mode=True
        )
    )
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    writer = _tool_names(
        model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-cli", "kanban"], quiet_mode=True
        )
    )
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", False)
    normal_after = _tool_names(
        model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-cli", "kanban"], quiet_mode=True
        )
    )

    assert _BLOCKED_TOOL_NAMES.issubset(normal_before)
    assert not (writer & _BLOCKED_TOOL_NAMES), (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert _BLOCKED_TOOL_NAMES.issubset(normal_after)


def test_writer_final_emitted_name_filter_precedes_lazy_catalog(monkeypatch):
    from tools import schema_sanitizer, tool_search

    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    monkeypatch.setattr(
        model_tools.registry,
        "get_definitions",
        lambda _names, quiet=False: [
            _tool_schema("terminal"),
            _tool_schema("computer_use"),
            _tool_schema("execute_code"),
            {"type": "function", "function": {"description": "malformed"}},
        ],
    )
    monkeypatch.setattr(schema_sanitizer, "sanitize_tool_schemas", lambda defs: defs)
    monkeypatch.setattr(
        tool_search,
        "load_config",
        lambda: SimpleNamespace(enabled="auto"),
    )
    seen_by_lazy_catalog = []

    def assemble(definitions, **_kwargs):
        seen_by_lazy_catalog.extend(_tool_names(definitions))
        return SimpleNamespace(
            activated=False,
            tool_defs=list(definitions),
            deferred_count=0,
            deferred_tokens=0,
            threshold_tokens=0,
        )

    monkeypatch.setattr(tool_search, "assemble_tool_defs", assemble)

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["terminal"],
        quiet_mode=False,
    )

    assert _tool_names(definitions) == {"terminal"}
    assert seen_by_lazy_catalog == ["terminal"]


def test_writer_fake_agent_final_surface_drops_memory_and_context_names(monkeypatch):
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    agent = SimpleNamespace(
        tools=[
            _tool_schema("read_file"),
            _tool_schema("computer_use"),
            _tool_schema("execute_code"),
            {"type": "function", "function": {"description": "malformed"}},
        ],
        valid_tool_names={"read_file", "computer_use", "execute_code"},
        _context_engine_tool_names={"execute_code"},
    )

    repo_writer_context.filter_agent_tool_surface(agent)

    assert _tool_names(agent.tools) == {"read_file"}
    assert agent.valid_tool_names == {"read_file"}
    assert agent._context_engine_tool_names == set()


def test_agent_init_final_guard_follows_all_post_build_injections():
    source = (
        Path(__file__).parents[2] / "agent" / "agent_init.py"
    ).read_text(encoding="utf-8")
    memory_injection = source.index("_inject_memory_provider_tools(agent)")
    context_injection = source.index("agent._context_engine_tool_names.add(_tname)")
    final_guard = source.index("filter_agent_tool_surface(agent)")
    usable_surface = source.index("context_compressor.on_session_start", final_guard)

    assert memory_injection < context_injection < final_guard < usable_surface, (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"notify_on_complete": True},
        {"watch_patterns": ["ready"]},
        {"pty": True},
        {"notify_on_complete": True, "pty": True},
    ],
)
def test_writer_background_terminal_denied_before_any_runtime_state(
    monkeypatch, kwargs
):
    import tools.terminal_tool as terminal_module
    from tools.process_registry import process_registry

    touched: list[str] = []

    def must_not_run(*_args, **_kwargs):
        touched.append("runtime")
        raise AssertionError("writer background denial ran too late")

    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    monkeypatch.setattr(terminal_module, "_get_env_config", must_not_run)
    monkeypatch.setattr(terminal_module, "resolve_task_overrides", must_not_run)
    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", must_not_run)
    monkeypatch.setattr(terminal_module, "_create_environment", must_not_run)
    monkeypatch.setattr(terminal_module, "_check_all_guards", must_not_run)
    monkeypatch.setattr(terminal_module.subprocess, "Popen", must_not_run)
    monkeypatch.setattr(process_registry, "spawn_local", must_not_run)
    monkeypatch.setattr(process_registry, "spawn_via_env", must_not_run)

    secret_command = "run --token TOP-SECRET /private/repository"
    result_text = terminal_module.terminal_tool(
        secret_command,
        background=True,
        workdir="/private/repository",
        **kwargs,
    )
    result = json.loads(result_text)

    assert result["status"] == "blocked", (
        REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
    )
    assert result["exit_code"] == -1
    assert touched == []
    assert secret_command not in result_text
    assert "/private/repository" not in result_text
    assert "TOP-SECRET" not in result_text


def test_writer_foreground_terminal_reaches_normal_path(monkeypatch):
    import tools.terminal_tool as terminal_module

    calls = 0

    def reached():
        nonlocal calls
        calls += 1
        raise RuntimeError("foreground-normal-path")

    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    monkeypatch.setattr(terminal_module, "_get_env_config", reached)

    terminal_module.terminal_tool("printf foreground", background=False)

    assert calls == 1


def test_nonwriter_background_terminal_reaches_normal_path(monkeypatch):
    import tools.terminal_tool as terminal_module

    calls = 0

    def reached():
        nonlocal calls
        calls += 1
        raise RuntimeError("background-normal-path")

    monkeypatch.setattr(terminal_module, "_get_env_config", reached)

    terminal_module.terminal_tool("printf background", background=True)

    assert calls == 1


REPO_FILE_MUTATION_GUARD_CONTRACT_MISSING = (
    "repo_file_mutation_guard_contract_missing"
)
REPO_TERMINAL_GUARD_CONTRACT_MISSING = (
    "repo_terminal_guard_contract_missing"
)


def test_repo_file_mutation_guard_contract_exists():
    try:
        from hermes_cli.repo_write_guard import RepoWriteGuard, RepoWriteGuardCode
    except ImportError:
        pytest.fail(REPO_FILE_MUTATION_GUARD_CONTRACT_MISSING)

    assert RepoWriteGuard is not None, REPO_FILE_MUTATION_GUARD_CONTRACT_MISSING
    assert {code.value for code in RepoWriteGuardCode} >= {
        "repo_busy",
        "repo_security",
        "repo_unsupported",
        "repo_operation",
        "repo_path",
    }, REPO_FILE_MUTATION_GUARD_CONTRACT_MISSING


def _task6_git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return repo


@contextmanager
def _task6_external_repo_holder(repo: Path):
    script = """
import sys
from hermes_cli.repo_write_lock import RepoWriteLock
lock = RepoWriteLock(sys.argv[1], blocking=False)
lock.acquire()
print("READY", flush=True)
sys.stdin.readline()
lock.release()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(repo)],
        cwd=Path(__file__).parents[2],
        env=dict(os.environ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        yield proc
    finally:
        if proc.stdin is not None:
            proc.stdin.write("release\n")
            proc.stdin.flush()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)


def _task6_repo_payload_snapshot(repo: Path) -> dict[str, tuple]:
    snapshot: dict[str, tuple] = {}
    for candidate in sorted(repo.rglob("*")):
        relative = candidate.relative_to(repo)
        if relative.parts and relative.parts[0] == ".git":
            continue
        info = candidate.lstat()
        payload = candidate.read_bytes() if candidate.is_file() else None
        snapshot[str(relative)] = (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            payload,
        )
    return snapshot


def _task6_local_file_tool(monkeypatch):
    import tools.file_tools as file_tools

    monkeypatch.setattr(file_tools, "_terminal_env_type_for_task", lambda _task: "local")
    return file_tools


def test_repo_guard_skips_existing_nonrepository_directory(tmp_path):
    from hermes_cli.repo_write_guard import RepoWriteGuard

    outside_git = tmp_path / "outside-git"
    outside_git.mkdir()
    target = outside_git / "nested" / "file.txt"

    guard = RepoWriteGuard([target.resolve()])
    assert guard.identities == ()
    with guard:
        pass


def test_repo_guard_supports_strict_existing_directory_endpoints(tmp_path, monkeypatch):
    from hermes_cli.repo_write_guard import RepoWriteGuard, RepoWriteGuardPathError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    repo = _task6_git_repo(tmp_path, "directory-endpoint-repo")
    subdir = repo / "nested"
    subdir.mkdir()

    guard = RepoWriteGuard([subdir], directories=True)
    assert len(guard.identities) == 1, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    with guard:
        pass

    for malformed in (Path("relative"), repo / "missing", repo / ".git" / "HEAD"):
        with pytest.raises(RepoWriteGuardPathError, match="^repo_path$"):
            RepoWriteGuard([malformed], directories=True)


@pytest.mark.parametrize("workdir_kind", ["root", "subdir", "symlink"])
def test_foreground_local_repo_contention_denies_before_runtime_state(
    tmp_path, monkeypatch, workdir_kind
):
    import tools.terminal_tool as terminal_module
    from tools.process_registry import process_registry

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    repo = _task6_git_repo(tmp_path, "terminal-held-repo")
    subdir = repo / "nested"
    subdir.mkdir()
    alias = tmp_path / "terminal-held-alias"
    alias.symlink_to(repo, target_is_directory=True)
    workdir = {
        "root": repo,
        "subdir": subdir,
        "symlink": alias / "nested",
    }[workdir_kind]
    marker = repo / "must-not-exist"
    touched: list[str] = []

    def must_not_run(*_args, **_kwargs):
        touched.append("runtime")
        raise AssertionError("repo terminal denial reached managed runtime state")

    class ExplodingActiveEnvironments(dict):
        def __contains__(self, _key):
            must_not_run()

        def get(self, _key, _default=None):
            must_not_run()

    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", must_not_run)
    monkeypatch.setattr(terminal_module, "_create_environment", must_not_run)
    monkeypatch.setattr(
        terminal_module, "_active_environments", ExplodingActiveEnvironments()
    )
    monkeypatch.setattr(process_registry, "spawn_local", must_not_run)
    monkeypatch.setattr(process_registry, "spawn_via_env", must_not_run)

    with _task6_external_repo_holder(repo):
        raw = terminal_module.terminal_tool(
            f"touch {marker}",
            workdir=str(workdir),
            task_id=f"task7-held-{workdir_kind}",
            force=True,
        )

    assert json.loads(raw) == {
        "status": "blocked",
        "error": "repo_terminal_guard_denied",
        "code": "repo_busy",
    }, REPO_TERMINAL_GUARD_CONTRACT_MISSING
    assert touched == []
    assert not marker.exists()
    assert str(repo) not in raw
    assert "touch" not in raw


def test_foreground_repo_contention_has_no_hidden_retry(tmp_path, monkeypatch):
    import hermes_cli.repo_write_guard as guard_module
    import tools.terminal_tool as terminal_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    repo = _task6_git_repo(tmp_path, "terminal-no-retry-repo")
    acquire_calls = 0
    original_acquire = guard_module.RepoWriteGuard.acquire

    def counted_acquire(self):
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(self)

    monkeypatch.setattr(guard_module.RepoWriteGuard, "acquire", counted_acquire)
    monkeypatch.setattr(
        terminal_module,
        "_start_cleanup_thread",
        lambda: (_ for _ in ()).throw(AssertionError("contention retried too late")),
    )
    with _task6_external_repo_holder(repo):
        result = json.loads(
            terminal_module.terminal_tool(
                "true", workdir=str(repo), task_id="task7-no-retry", force=True
            )
        )

    assert result["code"] == "repo_busy"
    assert acquire_calls == 1, REPO_TERMINAL_GUARD_CONTRACT_MISSING


def test_foreground_local_unusable_popen_fallback_is_constant_before_runtime(
    tmp_path, monkeypatch
):
    import tools.environments.local as local_environment
    import tools.terminal_tool as terminal_module

    repo = _task6_git_repo(tmp_path, "terminal-path-repo")
    requested = repo / "TOP-SECRET-missing"
    unusable_fallback = repo / "TOP-SECRET-still-missing"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    monkeypatch.setattr(
        local_environment,
        "_resolve_safe_cwd",
        lambda _cwd: str(unusable_fallback),
    )
    touched: list[str] = []

    def must_not_run(*_args, **_kwargs):
        touched.append("runtime")
        raise AssertionError("unusable local fallback reached managed runtime")

    monkeypatch.setattr(terminal_module, "_start_cleanup_thread", must_not_run)
    monkeypatch.setattr(terminal_module, "_create_environment", must_not_run)
    raw = terminal_module.terminal_tool(
        "printf TOP-SECRET-command",
        workdir=str(requested),
        task_id="task7-path-unusable-fallback",
        force=True,
    )

    assert json.loads(raw) == {
        "status": "blocked",
        "error": "repo_terminal_guard_denied",
        "code": "repo_path",
    }
    assert touched == []
    assert "TOP-SECRET" not in raw
    assert "traceback" not in raw.casefold()


@pytest.mark.parametrize(
    ("denial_name", "expected_code"),
    [
        ("RepoWriteGuardBusy", "repo_busy"),
        ("RepoWriteGuardSecurityError", "repo_security"),
        ("RepoWriteGuardUnsupported", "repo_unsupported"),
        ("RepoWriteGuardOperationError", "repo_operation"),
        ("RepoWriteGuardPathError", "repo_path"),
    ],
)
def test_foreground_terminal_maps_every_typed_guard_denial_to_constant_json(
    tmp_path, monkeypatch, denial_name, expected_code
):
    import hermes_cli.repo_write_guard as guard_module
    import tools.terminal_tool as terminal_module

    repo = _task6_git_repo(tmp_path, f"typed-terminal-{expected_code}")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(repo))
    denial_type = getattr(guard_module, denial_name)

    class DeniedGuard:
        def __init__(self, *_args, **_kwargs):
            raise denial_type()

    monkeypatch.setattr(terminal_module, "RepoWriteGuard", DeniedGuard)
    monkeypatch.setattr(
        terminal_module,
        "_start_cleanup_thread",
        lambda: (_ for _ in ()).throw(AssertionError("typed denial ran too late")),
    )
    raw = terminal_module.terminal_tool(
        "printf TOP-SECRET-command",
        workdir=str(repo),
        task_id=f"task7-typed-{expected_code}",
        force=True,
    )

    assert json.loads(raw) == {
        "status": "blocked",
        "error": "repo_terminal_guard_denied",
        "code": expected_code,
    }
    assert "TOP-SECRET" not in raw
    assert str(repo) not in raw


def test_repo_guard_current_pid_reentrant_and_alias_deduplicated(tmp_path, monkeypatch):
    from hermes_cli.repo_write_guard import RepoWriteGuard
    from hermes_cli.repo_write_lock import RepoWriteLock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    repo = _task6_git_repo(tmp_path, "repo")
    target = repo / "tracked.txt"
    target.write_text("old\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    alias_target = (alias / "tracked.txt").resolve()

    owner = RepoWriteLock(repo, blocking=False)
    owner.acquire()
    try:
        guard = RepoWriteGuard([target.resolve(), alias_target])
        assert len(guard.identities) == 1
        with guard:
            with guard:
                assert guard.identities == tuple(sorted(guard.identities))
    finally:
        owner.release()


def test_same_thread_distinct_guards_share_reentrant_lane_and_prune(tmp_path, monkeypatch):
    import hermes_cli.repo_write_guard as guard_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    repo = _task6_git_repo(tmp_path, "distinct-nested-guards")
    first = guard_module.RepoWriteGuard([repo / "first.txt"])
    second = guard_module.RepoWriteGuard([repo / "second.txt"])
    identity = first.identities[0]

    with first:
        lane = guard_module._lane_registry[identity]
        assert (lane.owner_thread_id, lane.depth, lane.users) == (
            threading.get_ident(),
            1,
            1,
        )
        with second:
            assert guard_module._lane_registry[identity] is lane
            assert (lane.owner_thread_id, lane.depth, lane.users) == (
                threading.get_ident(),
                2,
                2,
            )
        assert guard_module._lane_registry[identity] is lane
        assert (lane.owner_thread_id, lane.depth, lane.users) == (
            threading.get_ident(),
            1,
            1,
        )
    assert identity not in guard_module._lane_registry


def test_lane_reservation_prevents_entry_split_and_busy_unwind_preserves_owner():
    import hermes_cli.repo_write_guard as guard_module

    identity = "reservation-race"
    owner_item = guard_module._FrameItem(
        lock=SimpleNamespace(identity=identity)  # type: ignore[arg-type]
    )
    guard_module._reserve_lane(identity, owner_item)
    guard_module._acquire_lane(identity, owner_item)
    owner_lane = owner_item.lane
    assert owner_lane is not None
    outcome: list[object] = []

    def contend() -> None:
        item = guard_module._FrameItem(
            lock=SimpleNamespace(identity=identity)  # type: ignore[arg-type]
        )
        guard_module._reserve_lane(identity, item)
        outcome.append(item.lane)
        try:
            guard_module._acquire_lane(identity, item)
        except guard_module.RepoWriteGuardBusy as error:
            outcome.append(error)
        finally:
            guard_module._release_lane(identity, item)

    worker = threading.Thread(target=contend, name="lane-reservation-contender")
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert outcome[0] is owner_lane
    assert isinstance(outcome[1], guard_module.RepoWriteGuardBusy)
    assert (
        owner_lane.owner_thread_id,
        owner_lane.depth,
        owner_lane.users,
    ) == (threading.get_ident(), 1, 1)
    assert guard_module._lane_registry[identity] is owner_lane

    guard_module._release_lane(identity, owner_item)
    assert identity not in guard_module._lane_registry


def test_repo_guard_stable_multi_repo_order_and_partial_unwind(tmp_path, monkeypatch):
    from hermes_cli.repo_write_guard import RepoWriteGuard, RepoWriteGuardBusy
    from hermes_cli.repo_write_lock import repo_identity

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    repos = [_task6_git_repo(tmp_path, "repo-a"), _task6_git_repo(tmp_path, "repo-b")]
    ordered = sorted(repos, key=repo_identity)
    first_target = ordered[0] / "first.txt"
    second_target = ordered[1] / "second.txt"

    with _task6_external_repo_holder(ordered[1]):
        guard = RepoWriteGuard([second_target, first_target])
        assert guard.identities == tuple(sorted(guard.identities))
        with pytest.raises(RepoWriteGuardBusy) as denied:
            guard.acquire()
        assert str(denied.value) == "repo_busy"

        probe_script = """
import sys
from hermes_cli.repo_write_lock import RepoWriteLock
lock = RepoWriteLock(sys.argv[1], blocking=False)
lock.acquire()
lock.release()
"""
        probe = subprocess.run(
            [sys.executable, "-c", probe_script, str(ordered[0])],
            cwd=Path(__file__).parents[2],
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr


def test_file_tool_symlink_alias_resolves_to_held_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, "alias-repo")
    target = repo / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    alias = tmp_path / "alias-repo-link"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda _task: (_ for _ in ()).throw(
            AssertionError("alias guard denial reached file operations")
        ),
    )

    with _task6_external_repo_holder(repo):
        result = json.loads(
            file_tools.write_file_tool(
                str(alias / "target.txt"), "new\n", task_id="task6"
            )
        )

    assert result["code"] == "repo_busy"
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("operation", ["write", "replace", "add", "update", "delete", "move"])
def test_external_holder_blocks_every_file_mutation_endpoint_without_bytes_or_metadata(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, f"repo-{operation}")
    source = repo / "source.txt"
    source.write_text("old\n", encoding="utf-8")
    destination = repo / "nested" / "destination.txt"
    touched: list[str] = []

    def must_not_get_file_ops(_task_id):
        touched.append("file_ops")
        raise AssertionError("guard denial reached file operations")

    def must_not_enter_path_lock(_path):
        touched.append("path_lock")
        raise AssertionError("guard denial reached per-path lock")

    monkeypatch.setattr(file_tools, "_get_file_ops", must_not_get_file_ops)
    monkeypatch.setattr(file_tools.file_state, "lock_path", must_not_enter_path_lock)
    before = _task6_repo_payload_snapshot(repo)

    with _task6_external_repo_holder(repo):
        if operation == "write":
            raw = file_tools.write_file_tool(str(source), "new\n", task_id="task6")
        elif operation == "replace":
            raw = file_tools.patch_tool(
                mode="replace",
                path=str(source),
                old_string="old",
                new_string="new",
                task_id="task6",
            )
        else:
            if operation == "add":
                header = f"*** Add File: {destination}"
                body = "+new"
            elif operation == "update":
                header = f"*** Update File: {source}"
                body = "@@ @@\n-old\n+new"
            elif operation == "delete":
                header = f"*** Delete File: {source}"
                body = ""
            else:
                header = f"*** Move File: {source} -> {destination}"
                body = ""
            raw = file_tools.patch_tool(
                mode="patch",
                patch=f"*** Begin Patch\n{header}\n{body}\n*** End Patch\n",
                task_id="task6",
            )

    result = json.loads(raw)
    assert result == {
        "status": "blocked",
        "error": "repo_write_guard_denied",
        "code": "repo_busy",
    }, REPO_FILE_MUTATION_GUARD_CONTRACT_MISSING
    assert touched == []
    assert _task6_repo_payload_snapshot(repo) == before


def test_cross_repo_move_guards_source_and_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    source_repo = _task6_git_repo(tmp_path, "move-source")
    destination_repo = _task6_git_repo(tmp_path, "move-destination")
    source = source_repo / "source.txt"
    source.write_text("old\n", encoding="utf-8")
    destination = destination_repo / "nested" / "destination.txt"
    before = (
        _task6_repo_payload_snapshot(source_repo),
        _task6_repo_payload_snapshot(destination_repo),
    )
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda _task: (_ for _ in ()).throw(
            AssertionError("cross-repo denial reached file operations")
        ),
    )

    with _task6_external_repo_holder(destination_repo):
        result = json.loads(
            file_tools.patch_tool(
                mode="patch",
                patch=(
                    "*** Begin Patch\n"
                    f"*** Move File: {source} -> {destination}\n"
                    "*** End Patch\n"
                ),
                task_id="task6",
            )
        )

    assert result["code"] == "repo_busy"
    assert (
        _task6_repo_payload_snapshot(source_repo),
        _task6_repo_payload_snapshot(destination_repo),
    ) == before


def test_local_v4a_applies_same_parsed_operations_to_exact_absolute_endpoints(
    tmp_path, monkeypatch
):
    from tools.file_operations import ReadResult, WriteResult

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    source_repo = _task6_git_repo(tmp_path, "source-repo")
    destination_repo = _task6_git_repo(tmp_path, "destination-repo")
    source = source_repo / "source.txt"
    source.write_text("old\n", encoding="utf-8")
    destination = destination_repo / "nested" / "destination.txt"
    observed: list[tuple[str, str]] = []
    lint_calls: list[str] = []

    class ExactOps:
        def patch_v4a(self, _patch):
            raise AssertionError("local repository V4A must not be reparsed")

        def read_file_raw(self, path):
            endpoint = Path(path)
            if not endpoint.exists():
                return ReadResult(error=f"File not found: {path}")
            return ReadResult(content=endpoint.read_text(encoding="utf-8"))

        def move_file(self, source_path, destination_path):
            observed.append((source_path, destination_path))
            Path(destination_path).parent.mkdir(parents=True, exist_ok=True)
            Path(source_path).replace(destination_path)
            return WriteResult()

        def _check_lint(self, path):
            lint_calls.append(path)
            return SimpleNamespace(to_dict=lambda: {"status": "ok"})

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: ExactOps())
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)
    result = json.loads(
        file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                f"*** Move File: {source} -> {destination}\n"
                "*** End Patch\n"
            ),
            task_id="task6",
        )
    )

    exact_source = str(source.resolve())
    exact_destination = str(destination.resolve())
    assert "error" not in result
    assert observed == [(exact_source, exact_destination)]
    assert result["files_created"] == [exact_destination]
    assert result["files_deleted"] == [exact_source]
    assert "files_modified" not in result
    assert lint_calls == [exact_destination]
    assert source.exists() is False
    assert destination.read_bytes() == b"old\n"


def test_locked_local_v4a_update_then_move_creates_nested_parent_and_keeps_bytes(
    tmp_path, monkeypatch
):
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, "update-move-repo")
    source = repo / "source.py"
    source.write_bytes(b'value = "old"\n')
    destination = repo / "missing" / "a" / "b" / "destination.py"
    file_ops = ShellFileOperations(LocalEnvironment(cwd=str(repo)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: file_ops)
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)

    result = json.loads(
        file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                f"*** Update File: {source}\n"
                "@@ @@\n"
                '-value = "old"\n'
                '+value = "updated"\n'
                f"*** Move File: {source} -> {destination}\n"
                "*** End Patch\n"
            ),
            task_id="task6",
        )
    )

    exact_source = str(source.resolve())
    exact_destination = str(destination.resolve())
    assert "error" not in result
    assert source.exists() is False
    assert destination.parent.is_dir()
    assert destination.read_bytes() == b'value = "updated"\n'
    assert result["files_deleted"] == [exact_source]
    assert result["files_created"] == [exact_destination]
    assert list(result["lint"]) == [exact_destination]
    assert result["lint"][exact_destination]["status"] == "ok"


@pytest.mark.parametrize(
    ("lock_error", "expected_code"),
    [
        ("busy", "repo_busy"),
        ("security", "repo_security"),
        ("unsupported", "repo_unsupported"),
        ("operation", "repo_operation"),
    ],
)
def test_typed_guard_denials_are_constant_and_collapse_secret_details(
    tmp_path, monkeypatch, lock_error, expected_code
):
    import hermes_cli.repo_write_guard as guard_module
    from hermes_cli.repo_write_lock import (
        RepoLockBusy,
        RepoLockOperationError,
        RepoLockSecurityError,
        RepoLockUnsupported,
    )

    repo = _task6_git_repo(tmp_path, f"repo-{lock_error}")
    error_type = {
        "busy": RepoLockBusy,
        "security": RepoLockSecurityError,
        "unsupported": RepoLockUnsupported,
        "operation": RepoLockOperationError,
    }[lock_error]

    class FailingLock:
        identity = "a" * 64

        def __init__(self, _anchor, *, blocking):
            assert blocking is False

        def acquire(self):
            raise error_type("TOP-SECRET /private/repo errno=13")

        def release(self):
            raise AssertionError("unacquired lock released")

    monkeypatch.setattr(guard_module, "RepoWriteLock", FailingLock)
    guard = guard_module.RepoWriteGuard([repo / "file.txt"])
    with pytest.raises(guard_module.RepoWriteGuardError) as denied:
        guard.acquire()
    assert denied.value.code.value == expected_code
    rendered = str(denied.value)
    assert rendered == expected_code
    assert "TOP-SECRET" not in rendered
    assert "/private/repo" not in rendered
    assert "errno" not in rendered


def test_guard_path_error_is_typed_and_constant():
    from hermes_cli.repo_write_guard import RepoWriteGuard, RepoWriteGuardPathError

    with pytest.raises(RepoWriteGuardPathError) as denied:
        RepoWriteGuard([Path("relative/secret")])
    assert str(denied.value) == "repo_path"


def test_current_process_owner_file_write_succeeds_at_exact_resolved_path(
    tmp_path, monkeypatch
):
    from tools.file_operations import WriteResult

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, "owner-repo")
    target = repo / "nested" / "new.txt"
    observed: list[str] = []

    class DirectOps:
        def write_file(self, path, content):
            observed.append(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
            return WriteResult(bytes_written=len(content.encode()))

    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: DirectOps())
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)
    result = json.loads(
        file_tools.write_file_tool(str(target), "owner\n", task_id="task6")
    )

    assert "error" not in result
    assert observed == [str(target.resolve())]
    assert target.read_text(encoding="utf-8") == "owner\n"


def test_mocked_nonlocal_v4a_keeps_raw_backend_behavior(tmp_path, monkeypatch):
    file_tools = _task6_local_file_tool(monkeypatch)
    monkeypatch.setattr(
        file_tools, "_terminal_env_type_for_task", lambda _task: "ssh"
    )
    repo = _task6_git_repo(tmp_path, "remote-shaped-repo")
    target = repo / "file.txt"
    patch_text = (
        "*** Begin Patch\n"
        f"*** Add File: {target}\n"
        "+remote\n"
        "*** End Patch\n"
    )

    class Result:
        def to_dict(self):
            return {"status": "ok"}

    class RemoteOps:
        def __init__(self):
            self.raw = None

        def patch_v4a(self, raw):
            self.raw = raw
            return Result()

    remote = RemoteOps()
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: remote)
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)
    result = json.loads(
        file_tools.patch_tool(mode="patch", patch=patch_text, task_id="task6")
    )

    assert result["status"] == "ok"
    assert remote.raw == patch_text


@pytest.mark.parametrize("writer_context", [False, True], ids=["normal", "writer"])
@pytest.mark.parametrize("operation", ["write", "replace", "v4a"])
@pytest.mark.parametrize("resolver_error", [OSError, RuntimeError])
def test_local_resolution_exception_is_constant_and_stops_before_policy_and_file_ops(
    monkeypatch, writer_context, operation, resolver_error
):
    import tools.file_tools as file_tools

    monkeypatch.setattr(file_tools, "_terminal_env_type_for_task", lambda _task: "local")
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", writer_context)
    monkeypatch.setattr(
        file_tools,
        "_resolve_path_for_task",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            resolver_error("TOP-SECRET /path errno=5")
        ),
    )
    touched: list[str] = []
    monkeypatch.setattr(
        file_tools,
        "_check_sensitive_path",
        lambda *_a, **_kw: touched.append("policy"),
    )
    monkeypatch.setattr(
        file_tools,
        "_check_cross_profile_path",
        lambda *_a, **_kw: touched.append("cross_profile"),
    )
    monkeypatch.setattr(
        file_tools,
        "_get_file_ops",
        lambda _task: touched.append("file_ops"),
    )

    if operation == "write":
        raw = file_tools.write_file_tool("/TOP-SECRET/path", "data", task_id="task6")
    elif operation == "replace":
        raw = file_tools.patch_tool(
            mode="replace",
            path="/TOP-SECRET/path",
            old_string="old",
            new_string="new",
            task_id="task6",
        )
    else:
        raw = file_tools.patch_tool(
            mode="patch",
            patch=(
                "*** Begin Patch\n"
                "*** Add File: /TOP-SECRET/path\n"
                "+data\n"
                "*** End Patch\n"
            ),
            task_id="task6",
        )

    result = json.loads(raw)
    assert result == {
        "status": "blocked",
        "error": "repo_write_guard_denied",
        "code": "repo_path",
    }
    assert touched == []
    assert "TOP-SECRET" not in json.dumps(result)


def test_repo_guard_maps_marked_checkout_identity_timeout_to_operation(
    tmp_path, monkeypatch
):
    import hermes_cli.repo_write_lock as lock_module
    from hermes_cli.repo_write_guard import RepoWriteGuardOperationError, RepoWriteGuard

    repo = _task6_git_repo(tmp_path, "identity-timeout-repo")
    target = repo / "target.txt"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("TOP-SECRET /private/repo", 10)

    monkeypatch.setattr(lock_module.subprocess, "run", timeout)
    with pytest.raises(RepoWriteGuardOperationError) as denied:
        RepoWriteGuard([target.resolve()])
    assert str(denied.value) == "repo_operation"
    assert "TOP-SECRET" not in str(denied.value)


@pytest.mark.parametrize("operation", ["write", "replace"])
def test_write_and_replace_resolve_canonical_endpoint_once(
    tmp_path, monkeypatch, operation
):
    from tools.file_operations import PatchResult, WriteResult
    import tools.file_tools as file_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(file_tools, "_terminal_env_type_for_task", lambda _task: "local")
    repo = _task6_git_repo(tmp_path, f"resolve-once-{operation}")
    target = repo / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    original_resolver = file_tools._resolve_path_for_task
    calls: list[tuple[str, str]] = []

    def spy(filepath, task_id="default"):
        calls.append((filepath, task_id))
        return original_resolver(filepath, task_id)

    class ExactOps:
        def write_file(self, path, content):
            assert path == str(target.resolve())
            return WriteResult(bytes_written=len(content.encode()))

        def patch_replace(self, path, old, new, replace_all):
            assert (path, old, new, replace_all) == (
                str(target.resolve()), "old", "new", False
            )
            return PatchResult(success=True, files_modified=[path])

    monkeypatch.setattr(file_tools, "_resolve_path_for_task", spy)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: ExactOps())
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)

    if operation == "write":
        raw = file_tools.write_file_tool(str(target), "new\n", task_id="task6")
    else:
        raw = file_tools.patch_tool(
            mode="replace",
            path=str(target),
            old_string="old",
            new_string="new",
            task_id="task6",
        )

    assert "error" not in json.loads(raw)
    assert calls == [(str(target), "task6")]


def test_v4a_resolves_each_unique_endpoint_once_including_repeated_source(
    tmp_path, monkeypatch
):
    from tools.file_operations import PatchResult
    import tools.file_tools as file_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(file_tools, "_terminal_env_type_for_task", lambda _task: "local")
    repo = _task6_git_repo(tmp_path, "resolve-once-v4a")
    source = repo / "source.txt"
    destination = repo / "destination.txt"
    source.write_text("old\n", encoding="utf-8")
    original_resolver = file_tools._resolve_path_for_task
    calls: list[tuple[str, str]] = []

    def spy(filepath, task_id="default"):
        calls.append((filepath, task_id))
        return original_resolver(filepath, task_id)

    class ExactOps:
        pass

    def exact_apply(operations, file_ops):
        assert isinstance(file_ops, ExactOps)
        assert [(item.file_path, item.new_path) for item in operations] == [
            (str(source.resolve()), str(destination.resolve())),
            (str(source.resolve()), None),
        ]
        return PatchResult(success=True, files_modified=[str(source.resolve())])

    monkeypatch.setattr(file_tools, "_resolve_path_for_task", spy)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _task: ExactOps())
    monkeypatch.setattr(file_tools, "_check_sensitive_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", lambda *_a, **_kw: None)
    monkeypatch.setattr(file_tools, "apply_v4a_operations", exact_apply)

    raw = file_tools.patch_tool(
        mode="patch",
        patch=(
            "*** Begin Patch\n"
            f"*** Move File: {source} -> {destination}\n"
            f"*** Update File: {source}\n"
            "@@ @@\n"
            "-old\n"
            "+new\n"
            "*** End Patch\n"
        ),
        task_id="task6",
    )

    assert "error" not in json.loads(raw)
    assert calls == [(str(source), "task6"), (str(destination), "task6")]


def _task7_assert_external_repo_lock_available(repo: Path) -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from hermes_cli.repo_write_lock import RepoWriteLock\n"
                "lock = RepoWriteLock(sys.argv[1], blocking=False)\n"
                "lock.acquire()\n"
                "lock.release()\n"
            ),
            str(repo),
        ],
        cwd=Path(__file__).parents[2],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    assert probe.returncode == 0, (probe.stdout, probe.stderr)


def _task7_invoke_file_mutation(file_tools, operation: str, target: Path) -> str:
    if operation == "write":
        return file_tools.write_file_tool(
            str(target), "new\n", task_id="task7-enter-gap"
        )
    return file_tools.patch_tool(
        mode="replace",
        path=str(target),
        old_string="old",
        new_string="new",
        task_id="task7-enter-gap",
    )


@pytest.mark.parametrize("operation", ["write", "replace"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_file_tools_release_repo_guard_when_acquire_return_is_interrupted(
    tmp_path, monkeypatch, operation, interrupt_type
):
    import hermes_cli.repo_write_guard as guard_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, f"enter-gap-{operation}-{interrupt_type.__name__}")
    target = repo / "target.txt"
    target.write_bytes(b"old\n")
    before = _task6_repo_payload_snapshot(repo)
    interruption = interrupt_type(f"task7-{operation}-return-boundary")
    acquired_guards = []
    path_lock_calls: list[str] = []
    real_acquire = guard_module.RepoWriteGuard.acquire

    def acquire_then_interrupt(self):
        real_acquire(self)
        acquired_guards.append(self)
        raise interruption

    def must_not_enter_path_lock(path):
        path_lock_calls.append(path)
        raise AssertionError("repository acquire interruption reached path lock")

    monkeypatch.setattr(guard_module.RepoWriteGuard, "acquire", acquire_then_interrupt)
    monkeypatch.setattr(file_tools.file_state, "lock_path", must_not_enter_path_lock)

    with pytest.raises(interrupt_type) as raised:
        _task7_invoke_file_mutation(file_tools, operation, target)

    assert raised.value is interruption
    assert len(acquired_guards) == 1
    guard = acquired_guards[0]
    assert guard._frames == []
    assert all(identity not in guard_module._lane_registry for identity in guard.identities)
    assert path_lock_calls == []
    assert _task6_repo_payload_snapshot(repo) == before
    _task7_assert_external_repo_lock_available(repo)


@pytest.mark.parametrize("operation", ["write", "replace"])
def test_file_tools_registered_repo_release_is_safe_before_acquire(
    tmp_path, monkeypatch, operation
):
    import hermes_cli.repo_write_guard as guard_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, f"callback-gap-{operation}")
    target = repo / "target.txt"
    target.write_bytes(b"old\n")
    before = _task6_repo_payload_snapshot(repo)
    interruption = KeyboardInterrupt(f"task7-{operation}-callback-boundary")
    released_guards = []
    path_lock_calls: list[str] = []
    real_callback = file_tools.ExitStack.callback
    real_release = guard_module.RepoWriteGuard.release

    def register_then_interrupt(self, callback, *args, **kwargs):
        registered = real_callback(self, callback, *args, **kwargs)
        if isinstance(
            getattr(callback, "__self__", None), guard_module.RepoWriteGuard
        ):
            raise interruption
        return registered

    def observed_release(self):
        released_guards.append(self)
        real_release(self)

    def must_not_acquire(self):
        raise AssertionError("repository acquire ran after callback interruption")

    def must_not_enter_path_lock(path):
        path_lock_calls.append(path)
        raise AssertionError("callback interruption reached path lock")

    monkeypatch.setattr(file_tools.ExitStack, "callback", register_then_interrupt)
    monkeypatch.setattr(guard_module.RepoWriteGuard, "release", observed_release)
    monkeypatch.setattr(guard_module.RepoWriteGuard, "acquire", must_not_acquire)
    monkeypatch.setattr(file_tools.file_state, "lock_path", must_not_enter_path_lock)

    with pytest.raises(KeyboardInterrupt) as raised:
        _task7_invoke_file_mutation(file_tools, operation, target)

    assert raised.value is interruption
    assert len(released_guards) == 1
    assert released_guards[0]._frames == []
    assert path_lock_calls == []
    assert guard_module._lane_registry == {}
    assert _task6_repo_payload_snapshot(repo) == before
    _task7_assert_external_repo_lock_available(repo)


def test_write_file_registers_release_before_empty_identity_acquire(
    tmp_path, monkeypatch
):
    import hermes_cli.repo_write_guard as guard_module

    file_tools = _task6_local_file_tool(monkeypatch)
    outside_repo = tmp_path / "outside-repo"
    outside_repo.mkdir()
    target = outside_repo / "target.txt"
    target.write_bytes(b"old\n")
    interruption = KeyboardInterrupt("task7-empty-identity-callback-boundary")
    released_guards = []
    acquire_calls = 0
    real_callback = file_tools.ExitStack.callback
    real_release = guard_module.RepoWriteGuard.release

    def register_then_interrupt(self, callback, *args, **kwargs):
        registered = real_callback(self, callback, *args, **kwargs)
        guard = getattr(callback, "__self__", None)
        if isinstance(guard, guard_module.RepoWriteGuard):
            assert guard.identities == ()
            raise interruption
        return registered

    def observed_release(self):
        released_guards.append(self)
        real_release(self)

    def observed_acquire(self):
        nonlocal acquire_calls
        acquire_calls += 1
        return self

    monkeypatch.setattr(file_tools.ExitStack, "callback", register_then_interrupt)
    monkeypatch.setattr(guard_module.RepoWriteGuard, "release", observed_release)
    monkeypatch.setattr(guard_module.RepoWriteGuard, "acquire", observed_acquire)

    with pytest.raises(KeyboardInterrupt) as raised:
        file_tools.write_file_tool(str(target), "new\n", task_id="task7-empty")

    assert raised.value is interruption
    assert acquire_calls == 0
    assert len(released_guards) == 1
    assert released_guards[0].identities == ()
    assert released_guards[0]._frames == []
    assert target.read_bytes() == b"old\n"


def test_write_file_pending_publication_opcode_interrupt_cleans_guard_lane(
    tmp_path, monkeypatch
):
    import hermes_cli.repo_write_guard as guard_module
    import hermes_cli.repo_write_lock as lock_module
    from hermes_cli.repo_write_lock import RepoWriteLock

    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    file_tools = _task6_local_file_tool(monkeypatch)
    repo = _task6_git_repo(tmp_path, "write-pending-publication")
    target = repo / "target.txt"
    target.write_bytes(b"old\n")
    before = _task6_repo_payload_snapshot(repo)
    lock = RepoWriteLock(repo, blocking=False)
    key = (os.fspath(lock_module._lock_root()), lock.identity)
    instructions = list(dis.get_instructions(RepoWriteLock._acquire))
    publication = next(
        index
        for index, item in enumerate(instructions)
        if item.opname == "STORE_SUBSCR"
    )
    target_offset = instructions[publication + 1].offset
    interruption = KeyboardInterrupt("write-file-pending-publication")
    armed = True

    def trace(frame, event, _arg):
        nonlocal armed
        if frame.f_code is RepoWriteLock._acquire.__code__:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti == target_offset and armed:
                armed = False
                sys.settrace(None)
                raise interruption
        return trace

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            file_tools.write_file_tool(
                str(target), "new\n", task_id="task7-pending-publication"
            )
    finally:
        sys.settrace(None)

    assert raised.value is interruption
    assert _task6_repo_payload_snapshot(repo) == before
    assert key not in lock_module._registry
    assert guard_module._lane_registry == {}
    with RepoWriteLock(repo, blocking=False):
        pass
    _task7_assert_external_repo_lock_available(repo)
