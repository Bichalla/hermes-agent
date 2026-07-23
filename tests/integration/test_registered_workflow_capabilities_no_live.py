"""Integration contract for the no-live registered-workflow smoke runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_registered_workflow_capabilities_no_live.py"
READINESS_SCRIPT = ROOT / "scripts" / "check_registered_workflow_active_readiness.py"


def _run(*args: str, env=None):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        shell=False,
        check=False,
        timeout=300,
    )
    result = json.loads(completed.stdout)
    return completed, result


def test_default_smoke_uses_temp_only_and_denies_live_boundaries():
    completed, result = _run()
    assert completed.returncode == 0, result
    assert result["mode"] == "copied-source-default-deny"
    assert result["passed"] is True
    assert len(result["source_manifest_sha256"]) == 64
    int(result["source_manifest_sha256"], 16)
    assert result["network_denied"] is True
    assert result["hostile_boundaries_denied"] is True
    assert result["live_home_denied"] is True


def test_active_readiness_expect_absent_uses_isolated_profile(tmp_path: Path):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / ".hermes"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(READINESS_SCRIPT),
            "--expect-absent",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        shell=False,
        check=False,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert completed.returncode == 0, result
    assert result == {
        "schema": "registered-workflow-active-readiness/v1",
        "mode": "explicit-immutable-read-only",
        "feature_enabled": False,
        "tool_ready": False,
        "pending_db_ready": False,
        "dependencies_ready": True,
        "dependency_count": 0,
        "discord_toolset_enabled": False,
        "schema_present": False,
        "board_db_count": 1,
        "board_dbs_ready": False,
        "expect_absent": True,
        "expect_ready": False,
        "passed": True,
    }
