import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli import env_loader, repo_writer_context
from hermes_cli.env_loader import load_hermes_dotenv


_REPO_WRITER_ENV = "HERMES_KANBAN_REPO_WRITER"
_CONTRACT_MISSING = "repo_writer_tool_capability_contract_missing"


@pytest.fixture(autouse=True)
def _reset_repo_writer_context(monkeypatch):
    monkeypatch.delenv(_REPO_WRITER_ENV, raising=False)
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", False)
    yield
    os.environ.pop(_REPO_WRITER_ENV, None)


def test_env_loader_import_does_not_require_dotenv_to_be_a_package():
    real_env_loader = sys.modules["hermes_cli.env_loader"]
    hermes_cli_package = sys.modules["hermes_cli"]
    real_dotenv_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "dotenv" or name.startswith("dotenv.")
    }
    fake_dotenv = ModuleType("dotenv")
    assert not hasattr(fake_dotenv, "__path__")

    try:
        for name in real_dotenv_modules:
            sys.modules.pop(name, None)
        sys.modules["dotenv"] = fake_dotenv
        sys.modules.pop("hermes_cli.env_loader", None)
        hermes_cli_package.__dict__.pop("env_loader", None)

        imported = importlib.import_module("hermes_cli.env_loader")

        assert imported.__name__ == "hermes_cli.env_loader"
        assert "dotenv.main" not in sys.modules
    finally:
        for name in list(sys.modules):
            if name == "dotenv" or name.startswith("dotenv."):
                sys.modules.pop(name)
        sys.modules.update(real_dotenv_modules)
        sys.modules["hermes_cli.env_loader"] = real_env_loader
        setattr(hermes_cli_package, "env_loader", real_env_loader)


def test_user_env_overrides_stale_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://new.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"


def test_project_env_overrides_stale_shell_values_when_user_env_missing(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://project.example/v1"


def test_project_env_is_sanitized_before_loading(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    project_env = tmp_path / ".env"
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=0123456789:test"
        "ANTHROPIC_API_KEY=sk-ant-test123\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [project_env]
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "0123456789:test"
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-test123"


def test_user_env_takes_precedence_over_project_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text("OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home, project_env=project_env)

    assert loaded == [user_env, project_env]
    assert os.getenv("OPENAI_BASE_URL") == "https://user.example/v1"
    assert os.getenv("OPENAI_API_KEY") == "project-key"


def test_null_bytes_in_user_env_are_stripped(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    env_file = home / ".env"
    # Null bytes can be introduced when copy-pasting API keys.
    env_file.write_text("GLM_API_KEY=abc\x00\x00\nOPENAI_API_KEY=sk-123\n", encoding="utf-8")

    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_hermes_dotenv(hermes_home=home)

    assert loaded == [env_file]
    assert os.getenv("GLM_API_KEY") == "abc"
    assert os.getenv("OPENAI_API_KEY") == "sk-123"


def test_main_import_applies_user_env_over_shell_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / ".env").write_text(
        "OPENAI_BASE_URL=https://new.example/v1\nHERMES_INFERENCE_PROVIDER=custom\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")

    sys.modules.pop("hermes_cli.main", None)
    importlib.import_module("hermes_cli.main")

    assert os.getenv("OPENAI_BASE_URL") == "https://new.example/v1"
    assert os.getenv("HERMES_INFERENCE_PROVIDER") == "custom"


def _writer_dotenv_source(tmp_path, monkeypatch, source, value):
    home = tmp_path / "hermes"
    kwargs = {"hermes_home": home}
    if source == "profile":
        home.mkdir()
        dotenv_path = home / ".env"
        expected = [dotenv_path]
    elif source == "project":
        dotenv_path = tmp_path / "project.env"
        kwargs["project_env"] = dotenv_path
        expected = [dotenv_path]
    elif source == "managed":
        managed = tmp_path / "managed"
        managed.mkdir()
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        dotenv_path = managed / ".env"
        expected = []
    else:
        raise AssertionError(source)
    dotenv_path.write_text(f"{_REPO_WRITER_ENV}={value}\n", encoding="utf-8")
    return kwargs, expected


@pytest.mark.parametrize(
    ("inherited_writer", "dotenv_value", "expected"),
    [("1", "0", "1"), (None, "1", "<absent>"), ("nonexact", "1", "<absent>")],
)
def test_first_env_loader_import_captures_only_exact_writer_context(
    tmp_path, inherited_writer, dotenv_value, expected
):
    home = tmp_path / "hermes"
    home.mkdir()
    home.joinpath(".env").write_text(
        f"{_REPO_WRITER_ENV}={dotenv_value}\n", encoding="utf-8"
    )
    env = dict(os.environ)
    if inherited_writer is None:
        env.pop(_REPO_WRITER_ENV, None)
    else:
        env[_REPO_WRITER_ENV] = inherited_writer
    script = f"""
import os
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(hermes_home={str(home)!r})
load_hermes_dotenv(hermes_home={str(home)!r})
print(os.environ.get({_REPO_WRITER_ENV!r}, "<absent>"))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stdout.strip() == expected, _CONTRACT_MISSING


@pytest.mark.parametrize("source", ["profile", "project", "managed"])
@pytest.mark.parametrize("dotenv_value", ["0", ""], ids=["zero", "empty"])
def test_repeated_dotenv_loads_cannot_disable_trusted_writer_context(
    tmp_path, monkeypatch, source, dotenv_value
):
    kwargs, expected = _writer_dotenv_source(
        tmp_path, monkeypatch, source, dotenv_value
    )
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
    monkeypatch.setenv(_REPO_WRITER_ENV, "1")

    for _ in range(2):
        assert load_hermes_dotenv(**kwargs) == expected, _CONTRACT_MISSING
        assert os.environ.get(_REPO_WRITER_ENV) == "1", _CONTRACT_MISSING


@pytest.mark.parametrize("source", ["profile", "project", "managed"])
def test_repeated_dotenv_loads_cannot_activate_untrusted_writer_context(
    tmp_path, monkeypatch, source
):
    kwargs, expected = _writer_dotenv_source(tmp_path, monkeypatch, source, "1")
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", False)
    monkeypatch.setenv(_REPO_WRITER_ENV, "nonexact")

    for _ in range(2):
        assert load_hermes_dotenv(**kwargs) == expected, _CONTRACT_MISSING
        assert _REPO_WRITER_ENV not in os.environ, _CONTRACT_MISSING


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_dotenv_implementation_exception_still_restores_writer_context(
    monkeypatch, trusted
):
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", trusted)
    if trusted:
        monkeypatch.setenv(_REPO_WRITER_ENV, "1")

    def fail_after_overwrite(**_kwargs):
        os.environ[_REPO_WRITER_ENV] = "0" if trusted else "1"
        raise RuntimeError("dotenv implementation failed")

    monkeypatch.setattr(
        env_loader, "_load_hermes_dotenv_impl", fail_after_overwrite, raising=False
    )
    with pytest.raises(RuntimeError, match="dotenv implementation failed"):
        load_hermes_dotenv()

    assert os.environ.get(_REPO_WRITER_ENV) == ("1" if trusted else None), (
        _CONTRACT_MISSING
    )


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_dotenv_reload_never_transiently_mutates_writer_marker_or_runtime_gates(
    tmp_path, monkeypatch, trusted
):
    """Pause after dotenv application and inspect all guards concurrently."""
    import json
    import threading

    import model_tools
    import tools.terminal_tool as terminal_module

    home = tmp_path / "hermes"
    home.mkdir()
    home.joinpath(".env").write_text(
        f"{_REPO_WRITER_ENV}={'0' if trusted else '1'}\n"
        "TASK5_UNRELATED_VALUE=loaded\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", trusted)
    if trusted:
        monkeypatch.setenv(_REPO_WRITER_ENV, "1")
    else:
        monkeypatch.delenv(_REPO_WRITER_ENV, raising=False)

    reached_after_apply = threading.Event()
    release_reload = threading.Event()
    original_sanitize = env_loader._sanitize_loaded_credentials

    def pause_after_apply():
        reached_after_apply.set()
        assert release_reload.wait(timeout=5)
        original_sanitize()

    monkeypatch.setattr(env_loader, "_sanitize_loaded_credentials", pause_after_apply)

    surface = {"computer_use", "execute_code", "terminal"}
    monkeypatch.setattr(
        model_tools.registry,
        "get_definitions",
        lambda names, quiet=False: [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in sorted(set(names) & surface)
        ],
    )
    model_tools._clear_tool_defs_cache()

    terminal_reached = []

    def terminal_normal_path():
        terminal_reached.append(True)
        raise RuntimeError("normal terminal path")

    monkeypatch.setattr(terminal_module, "_get_env_config", terminal_normal_path)
    errors = []

    def reload_env():
        try:
            load_hermes_dotenv(hermes_home=home)
        except BaseException as exc:  # pragma: no cover - assertion relay
            errors.append(exc)

    thread = threading.Thread(target=reload_env)
    thread.start()
    assert reached_after_apply.wait(timeout=5), _CONTRACT_MISSING
    try:
        assert os.environ.get(_REPO_WRITER_ENV) == ("1" if trusted else None)
        assert repo_writer_context.is_repo_writer_context() is trusted
        names = {
            item["function"]["name"]
            for item in model_tools.get_tool_definitions(
                enabled_toolsets=["computer_use", "code_execution", "terminal"],
                quiet_mode=False,
                skip_tool_search_assembly=True,
            )
        }
        background = json.loads(
            terminal_module.terminal_tool("printf task5", background=True)
        )
        if trusted:
            assert not (names & {"computer_use", "execute_code"})
            assert background["status"] == "blocked"
            assert terminal_reached == []
        else:
            assert {"computer_use", "execute_code"}.issubset(names)
            assert terminal_reached == [True]
            assert background.get("status") != "blocked"
    finally:
        release_reload.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert os.environ.get(_REPO_WRITER_ENV) == ("1" if trusted else None)
    assert os.environ["TASK5_UNRELATED_VALUE"] == "loaded"


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_external_secret_source_cannot_mutate_writer_marker_even_inside_apply(
    tmp_path, monkeypatch, trusted
):
    from types import SimpleNamespace
    from agent.secret_sources import registry as secret_registry

    monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", trusted)
    if trusted:
        monkeypatch.setenv(_REPO_WRITER_ENV, "1")
    else:
        monkeypatch.delenv(_REPO_WRITER_ENV, raising=False)
    monkeypatch.setattr(env_loader, "_load_secrets_config", lambda _home: {"x": {}})
    observed_inside_apply = []

    def fake_apply_all(_cfg, _home, environ=None):
        assert environ is not None
        environ[_REPO_WRITER_ENV] = "0" if trusted else "1"
        observed_inside_apply.append(os.environ.get(_REPO_WRITER_ENV))
        return SimpleNamespace(
            applied_any=False,
            provenance={},
            sources=[],
            conflicts=[],
        )

    monkeypatch.setattr(secret_registry, "apply_all", fake_apply_all)
    env_loader.reset_secret_source_cache()

    load_hermes_dotenv(hermes_home=tmp_path)

    expected = "1" if trusted else None
    assert observed_inside_apply == [expected], _CONTRACT_MISSING
    assert os.environ.get(_REPO_WRITER_ENV) == expected


def test_repeated_dotenv_loads_preserve_unrelated_precedence_and_return_paths(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    user_env = home / ".env"
    project_env = tmp_path / ".env"
    user_env.write_text("OPENAI_BASE_URL=https://user.example/v1\n", encoding="utf-8")
    project_env.write_text(
        "OPENAI_BASE_URL=https://project.example/v1\nOPENAI_API_KEY=project-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shell.example/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    for _ in range(2):
        assert load_hermes_dotenv(
            hermes_home=home, project_env=project_env
        ) == [user_env, project_env]
        assert os.environ["OPENAI_BASE_URL"] == "https://user.example/v1"
        assert os.environ["OPENAI_API_KEY"] == "project-key"
