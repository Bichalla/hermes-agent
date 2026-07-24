"""Temp-only integration for the fixed registered Lifelog wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[2]
OPS_LIFELOG = Path(
    os.environ.get(
        "HERMES_TEST_OPS_LIFELOG",
        str(Path.home() / ".hermes" / "ops" / "state" / "lifelog"),
    )
)
WRAPPER = AGENT_ROOT / "scripts" / "run_registered_lifelog_recorder.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("registered_lifelog_wrapper", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "home" / ".hermes" / "ops" / "state" / "lifelog"
    (root / "config").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy2(OPS_LIFELOG / "config" / "recorder-registry.json", root / "config")
    for name in (
        "run_registered_recorder.py",
        "record_diet_intake.py",
    ):
        shutil.copy2(OPS_LIFELOG / "scripts" / name, root / "scripts" / name)
    canonical_validator = OPS_LIFELOG / "scripts" / "validate_lifelog.py"
    (root / "scripts" / "validate_lifelog.py").write_text(
        "import runpy\n"
        f"runpy.run_path({str(canonical_validator)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            str(OPS_LIFELOG / "scripts" / "lifelog_migrate.py"),
            "--db",
            str(root / "lifelog.db"),
        ],
        cwd=OPS_LIFELOG,
        check=True,
        text=True,
        capture_output=True,
    )
    payload_root = root / ".runtime-inputs" / "diet-intake"
    payload_root.mkdir(parents=True, mode=0o700)
    payload = {
        "schema_version": "diet_intake/v1",
        "intent": "confirmed_intake",
        "occurred_at": "2026-07-22T12:00:00+09:00",
        "timezone": "Asia/Seoul",
        "person_id": "person_park_sanghyun",
        "meal_label": "lunch",
        "title": "Synthetic temp meal",
        "items": [{"name": "synthetic meal", "quantity_text": "1 serving"}],
        "nutrition_estimate": {},
        "tags": ["diet", "confirmed_intake"],
        "source": {"platform": "manual"},
    }
    payload_path = payload_root / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    payload_path.chmod(0o600)
    return root


def test_fixed_wrapper_temp_db_insert_and_exact_replay(tmp_path: Path, monkeypatch):
    wrapper = _load_wrapper()
    root = _temp_root(tmp_path)
    payload = root / ".runtime-inputs" / "diet-intake" / "payload.json"
    monkeypatch.chdir(root)

    first = wrapper.run_registered_lifelog_recorder(
        "diet_intake.v1", payload, dry_run=False, root=root
    )
    second = wrapper.run_registered_lifelog_recorder(
        "diet_intake.v1", payload, dry_run=False, root=root
    )

    assert first["validation_status"] == "validator_and_readback_passed"
    assert first["idempotency_result"] == "inserted"
    assert second["idempotency_result"] == "existing"
    assert first["event_ids"] == second["event_ids"]


def test_executable_entrypoint_requires_authority_and_replays_exactly(tmp_path: Path):
    root = _temp_root(tmp_path)
    payload = root / ".runtime-inputs" / "diet-intake" / "payload.json"
    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = [
        sys.executable,
        str(WRAPPER),
        "diet_intake.v1",
        "--payload",
        str(payload),
        "--dry-run",
        "false",
        "--json",
    ]

    denied = subprocess.run(
        argv,
        cwd=root,
        env=base_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert denied.returncode == 2

    authorized_env = {
        **base_env,
        "HERMES_CURRENT_USER_ACTION_FINGERPRINT": "test-trusted-current-turn",
    }
    first = subprocess.run(
        argv,
        cwd=root,
        env=authorized_env,
        text=True,
        capture_output=True,
        check=False,
    )
    second = subprocess.run(
        argv,
        cwd=root,
        env=authorized_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["idempotency_result"] == "inserted"
    assert second_result["idempotency_result"] == "existing"
    assert first_result["event_ids"] == second_result["event_ids"]
