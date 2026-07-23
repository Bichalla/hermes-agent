from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes"
    home.mkdir()
    hermes_home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }
    return env


def _blocked_create_args() -> list[str]:
    return [
        "kanban",
        "create",
        "No authority",
        "--initial-status",
        "blocked",
    ]


def test_main_returns_kanban_handler_failure(tmp_path, monkeypatch, capsys):
    import hermes_cli.main as cli_main

    env = _isolated_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli_main, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(sys, "argv", ["hermes", *_blocked_create_args()])

    assert cli_main.main() == 2
    assert "structured kanban_create tool" in capsys.readouterr().err


def test_module_entrypoint_exits_with_handler_failure(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *_blocked_create_args()],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2
    assert "structured kanban_create tool" in proc.stderr


def test_installed_console_entry_exits_with_handler_failure(tmp_path):
    console = Path(sys.executable).with_name("hermes")
    assert console.is_file()
    proc = subprocess.run(
        [str(console), *_blocked_create_args()],
        cwd=ROOT,
        env=_isolated_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2
    assert "structured kanban_create tool" in proc.stderr


def test_cli_rejects_forged_reserved_authority_even_when_well_formed(tmp_path):
    env = _isolated_env(tmp_path)
    env.update(
        {
            "HERMES_CURRENT_USER_ACTION_FINGERPRINT": "a" * 64,
            "HERMES_CURRENT_USER_REQUEST_TARGET_FINGERPRINT": "b" * 64,
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *_blocked_create_args()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 2
    assert "structured kanban_create tool" in proc.stderr
    assert "\"task_id\"" not in proc.stdout


@pytest.mark.parametrize(
    ("handler_result", "expected"),
    [
        (None, 0),
        (0, 0),
        (7, 7),
        (True, 0),
        (False, 0),
        ("7", 0),
        (object(), 0),
    ],
)
def test_main_normalizes_handler_result_exactly(monkeypatch, handler_result, expected):
    import hermes_cli.main as cli_main

    fake_args = SimpleNamespace(
        version=False,
        yolo=False,
        oneshot=None,
        resume=False,
        continue_last=False,
        command="synthetic",
        func=lambda _args: handler_result,
    )
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", lambda *_a, **_k: fake_args)
    monkeypatch.setattr(cli_main, "_prepare_agent_startup", lambda _args: None)
    monkeypatch.setattr(sys, "argv", ["hermes", "synthetic"])

    assert cli_main.main() == expected
