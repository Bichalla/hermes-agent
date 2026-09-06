from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_real_cli(**kwargs):
    clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as cli_mod

        cli_mod = importlib.reload(cli_mod)
        with patch.object(cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            cli_mod.__dict__, {"CLI_CONFIG": clean_config}
        ):
            return cli_mod.HermesCLI(**kwargs)


class _DummyCLI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = "session-123"
        self.system_prompt = "base prompt"
        self.preloaded_skills = []
        self.preloaded_skill_sources = []

    def show_banner(self):
        return None

    def show_tools(self):
        return None

    def show_toolsets(self):
        return None

    def run(self):
        return None


def _real_finalize(cli_obj):
    """Call the real HermesCLI.finalize_preloaded_skills on a dummy object."""
    return _REAL_FINALIZE(cli_obj)


def _capture_real_finalize():
    import cli as cli_mod
    return cli_mod.HermesCLI.__dict__["finalize_preloaded_skills"]


_REAL_FINALIZE = _capture_real_finalize()


def test_main_applies_preloaded_skills_to_system_prompt(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None, return_metadata=False: (
            "skill prompt",
            ["hermes-agent-dev", "github-auth"],
            [],
            [
                {"source_id": "hermes-agent-dev", "source_kind": "ordinary", "status": "loaded"},
                {"source_id": "github-auth", "source_kind": "ordinary", "status": "loaded"},
            ],
        ),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="hermes-agent-dev,github-auth", list_tools=True)

    cli_obj = created["cli"]
    # The preload now runs in a background thread and is folded in at agent
    # init via finalize_preloaded_skills() (startup-latency change). Drive
    # the finalize explicitly — the same call _init_agent makes.
    _real_finalize(cli_obj)
    assert cli_obj.system_prompt == "base prompt\n\nskill prompt"
    assert cli_obj.preloaded_skills == ["hermes-agent-dev", "github-auth"]


def test_main_raises_for_unknown_preloaded_skill(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None, return_metadata=False: ("", [], ["missing-skill"], []),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="missing-skill", list_tools=True)

    # The all-skills-unknown hard failure now surfaces when the preload is
    # finalized (agent init), preserving the fail-loud contract.
    with pytest.raises(ValueError, match=r"Unknown skill\(s\): missing-skill"):
        _real_finalize(created["cli"])


def test_finalize_preloaded_skills_records_builtin_source_metadata():
    from agent.skill_commands import build_preloaded_skills_prompt

    cli_obj = _DummyCLI()
    cli_obj._preload_skills_thread = MagicMock()
    cli_obj._preload_skills_thread.is_alive.return_value = False
    cli_obj._preload_skills_result = build_preloaded_skills_prompt(
        ["hermes-builtin:sdlc-review"],
        return_metadata=True,
    )

    _real_finalize(cli_obj)

    assert cli_obj.preloaded_skills == ["sdlc-review"]
    assert cli_obj.preloaded_skill_sources == cli_obj._preload_skills_result[3]
    assert cli_obj.preloaded_skill_sources[0]["source_id"] == "hermes-builtin:sdlc-review"
    assert cli_obj.preloaded_skill_sources[0]["source_kind"] == "builtin"
    assert cli_obj.preloaded_skill_sources[0]["status"] == "loaded"
    assert cli_obj.preloaded_skill_sources[0]["source_hash"].startswith("sha256:")


def test_finalize_preloaded_skills_fails_closed_for_builtin_failure_even_with_loaded_skill():
    cli_obj = _DummyCLI()
    cli_obj._preload_skills_thread = MagicMock()
    cli_obj._preload_skills_thread.is_alive.return_value = False
    cli_obj._preload_skills_result = (
        "ordinary prompt",
        ["ordinary-skill"],
        ["hermes-builtin:sdlc-review"],
        [
            {
                "source_id": "hermes-builtin:sdlc-review",
                "source_kind": "builtin",
                "status": "missing",
                "builtin_name": "sdlc-review",
            },
            {
                "source_id": "ordinary-skill",
                "source_kind": "ordinary",
                "status": "loaded",
            },
        ],
    )

    with pytest.raises(
        ValueError,
        match=r"Required builtin skill\(s\) unavailable: sdlc-review",
    ):
        _real_finalize(cli_obj)

    assert cli_obj.preloaded_skill_sources[0]["status"] == "missing"


def test_finalize_preloaded_skills_times_out_for_required_builtin():
    cli_obj = _DummyCLI()
    cli_obj._preload_skills_thread = MagicMock()
    cli_obj._preload_skills_thread.is_alive.return_value = True
    cli_obj._preload_skills_requested = ["hermes-builtin:sdlc-review"]
    cli_obj._preload_skills_result = None

    with pytest.raises(TimeoutError, match=r"Required builtin skill preload timed out: sdlc-review"):
        _real_finalize(cli_obj)


def test_show_banner_does_not_print_skills():
    """show_banner() no longer prints the activated skills line — it moved to run()."""
    cli_obj = _make_real_cli(compact=False)
    cli_obj.preloaded_skills = ["hermes-agent-dev", "github-auth"]
    cli_obj.console = MagicMock()

    with patch("cli.build_welcome_banner") as mock_banner, patch(
        "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
    ):
        cli_obj.show_banner()

    print_calls = [
        call.args[0]
        for call in cli_obj.console.print.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    startup_lines = [line for line in print_calls if "Activated skills:" in line]
    assert len(startup_lines) == 0
    assert mock_banner.call_count == 1
