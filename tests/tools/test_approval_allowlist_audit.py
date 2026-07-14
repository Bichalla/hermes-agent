from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_approval_allowlist.py"


def _run(tmp_path: Path, patterns: object):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"command_allowlist": patterns, "api_key": "must-not-leak"}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config), "--json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_audit_classifies_patterns_and_emits_read_only_preview(tmp_path):
    result = _run(
        tmp_path,
        [
            "recursive delete of root filesystem",
            "rm -rf *",
            "python3 *",
            "hermes kanban comment *",
            "legacy-name",
        ],
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["schema"] == "approval-allowlist-audit/v1"
    assert output["read_only"] is True
    assert output["config_mutated"] is False
    assert [entry["risk_class"] for entry in output["entries"]] == [
        "hardline-unbypassable",
        "broad-destructive",
        "broad-interpreter-shell",
        "workflow-specific-candidate",
        "obsolete-unknown",
    ]
    assert output["migration_preview"]["schema"] == "approval-allowlist-migration-preview/v1"


def test_audit_never_emits_unrelated_config_or_secret_bearing_commands(tmp_path):
    secret = "token=SUPERSECRET-1234567890"
    result = _run(tmp_path, [f"curl https://example.invalid?{secret}", "python3 -c print(1)"])
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout
    assert "must-not-leak" not in result.stdout
    output = json.loads(result.stdout)
    assert output["entries"][0]["pattern_name"].startswith("entry_")
    assert set(output) == {
        "schema",
        "read_only",
        "config_mutated",
        "entries",
        "migration_preview",
        "summary",
    }


def test_audit_never_emits_local_paths_user_labels_or_unknown_raw_names(tmp_path):
    patterns = [
        "/Users/alice/private-script",
        "alice-local-deploy",
        "team-red-internal-label",
        "recursive delete of root filesystem",
    ]
    result = _run(tmp_path, patterns)
    assert result.returncode == 0, result.stderr
    for raw in patterns:
        assert raw not in result.stdout
    output = json.loads(result.stdout)
    assert output["entries"][0]["pattern_name"] == "entry_0001"
    assert output["entries"][1]["pattern_name"] == "entry_0002"
    assert output["entries"][2]["pattern_name"] == "entry_0003"
    assert output["entries"][3]["pattern_name"].startswith("hardline:")


def test_audit_rejects_non_list_allowlist(tmp_path):
    result = _run(tmp_path, {"bad": "shape"})
    assert result.returncode != 0
    assert "command_allowlist" in result.stderr
