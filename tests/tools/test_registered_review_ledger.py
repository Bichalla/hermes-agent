"""Registered review-ledger controller tests using only temporary databases."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from gateway.session_context import clear_session_vars, set_session_vars


@pytest.fixture(autouse=True)
def _clear_context():
    tokens = set_session_vars()
    clear_session_vars(tokens)
    yield
    clear_session_vars([])


def _owner_context(
    platform: str = "discord",
    session_id: str = "main-session",
    controller_role: str = "main_controller",
    user_text: str | None = "run the registered review protocol",
):
    return set_session_vars(
        platform=platform,
        session_id=session_id,
        controller_role=controller_role,
        user_text=user_text,
    )


def _ready(monkeypatch, tool, tmp_path: Path):
    db = tmp_path / "owner" / "review-ledger.sqlite3"
    db.parent.mkdir(mode=0o700)
    db.parent.chmod(0o700)
    monkeypatch.setattr(tool, "_CANONICAL_DB", db)
    monkeypatch.setattr(tool, "_feature_enabled", lambda: True)
    monkeypatch.setattr(tool, "_dependencies_ready", lambda: True)
    return db


def _snapshot(paths: list[Path]):
    result = {}
    for path in paths:
        if path.exists():
            result[str(path)] = (
                path.stat().st_mtime_ns,
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            result[str(path)] = None
    return result


def test_schema_is_closed_and_exposes_no_owner_selected_execution_fields():
    from tools.registered_review_ledger import REGISTERED_REVIEW_LEDGER_SCHEMA

    params = REGISTERED_REVIEW_LEDGER_SCHEMA["parameters"]
    assert params["additionalProperties"] is False
    assert params["properties"]["action"]["enum"] == [
        "finalize",
        "freeze",
        "record_result",
        "start_attempt",
        "status",
    ]
    assert set(params["properties"]) == {
        "action",
        "attempt_id",
        "bundle_sha256",
        "completed_at",
        "created_at",
        "current_bundle_sha256",
        "finding_classes",
        "outcome",
        "required_roles",
        "role",
        "started_at",
    }
    for forbidden in (
        "approved",
        "authority",
        "command",
        "controller_role",
        "db",
        "environment",
        "executable",
        "path",
        "raw_review",
        "safe_summary",
        "sql",
    ):
        assert forbidden not in params["properties"]


def test_handler_rejects_model_supplied_dispatcher_identity_without_crashing():
    import tools.registered_review_ledger as tool

    result = json.loads(
        tool._handle_registered_review_ledger(
            {
                "action": "status",
                "bundle_sha256": "a" * 64,
                "session_id": "forged",
            },
            session_id="trusted",
        )
    )
    assert result["decision"] == "deny_schema_invalid"
    assert result["write_count"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "freeze", "bundle_sha256": "a" * 64},
        {
            "action": "freeze",
            "bundle_sha256": "a" * 64,
            "required_roles": ["security-review", "eng-review"],
            "created_at": "2026-07-24T11:00:00+09:00",
        },
        {
            "action": "start_attempt",
            "bundle_sha256": "a" * 64,
            "role": "eng-review",
            "started_at": True,
        },
        {
            "action": "record_result",
            "attempt_id": 1,
            "outcome": "PASS",
            "finding_classes": ["security", "security"],
            "completed_at": "2026-07-24T11:02:00+09:00",
        },
        {"action": "status", "bundle_sha256": "A" * 64},
        {"action": "unknown"},
    ],
)
def test_invalid_closed_requests_deny_before_owner(monkeypatch, kwargs):
    import tools.registered_review_ledger as tool

    monkeypatch.setattr(
        tool,
        "_invoke_owner",
        lambda *_args, **_kw: pytest.fail("owner invoked for invalid request"),
    )
    result = tool.registered_review_ledger(session_id="main-session", **kwargs)
    assert result["decision"] in {
        "deny_schema_invalid",
        "deny_unregistered_action",
    }
    assert result["write_count"] == 0


@pytest.mark.parametrize(
    "platform,dispatcher_session,env_session,controller_role,user_text",
    [
        ("delegate", "main-session", "main-session", "main_controller", "run"),
        ("review", "main-session", "main-session", "main_controller", "run"),
        ("cron", "main-session", "main-session", "main_controller", "run"),
        ("discord", "", "main-session", "main_controller", "run"),
        ("discord", "counterfeit", "main-session", "main_controller", "run"),
        ("", "main-session", "main-session", "main_controller", "run"),
        ("discord", "main-session", "main-session", "", "run"),
        ("discord", "main-session", "main-session", "delegate", "run"),
        ("discord", "main-session", "main-session", "main_controller", None),
    ],
)
def test_non_controller_identity_denies_before_owner(
    monkeypatch,
    tmp_path,
    platform,
    dispatcher_session,
    env_session,
    controller_role,
    user_text,
):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    tokens = _owner_context(
        platform=platform,
        session_id=env_session,
        controller_role=controller_role,
        user_text=user_text,
    )
    monkeypatch.setattr(
        tool,
        "_invoke_owner",
        lambda *_args, **_kw: pytest.fail("owner invoked without controller identity"),
    )
    result = tool.registered_review_ledger(
        action="status",
        bundle_sha256="a" * 64,
        session_id=dispatcher_session,
    )
    assert result["decision"] == "deny_authority_missing"
    assert result["write_count"] == 0
    clear_session_vars(tokens)


def test_real_fixed_wrapper_executes_five_commands_against_temp_db(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    canonical = Path.home() / ".hermes" / "ops" / "state" / "review-ledger" / "review-ledger.sqlite3"
    canonical_paths = [canonical, Path(str(canonical) + "-wal"), Path(str(canonical) + "-shm")]
    canonical_before = _snapshot(canonical_paths)
    db = _ready(monkeypatch, tool, tmp_path)
    assert tool._OWNER_WRAPPER == Path.home() / ".hermes" / "ops" / "scripts" / "review_ledger.py"
    assert tool._OWNER_ROOT == Path.home() / ".hermes" / "ops"
    tokens = _owner_context()
    bundle = "a" * 64
    roles = ["eng-review", "security-review"]

    frozen = tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=roles,
        created_at="2026-07-24T11:00:00+09:00",
        session_id="main-session",
    )
    assert frozen["decision"] == "allow"
    assert frozen["readback"] == "passed"
    assert frozen["write_count"] == 1
    replayed_freeze = tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=roles,
        created_at="2026-07-24T11:00:00+09:00",
        session_id="main-session",
    )
    assert replayed_freeze["decision"] == "allow"
    assert replayed_freeze["write_count"] == 0
    assert replayed_freeze["idempotency_result"] == "existing"

    for index, role in enumerate(roles, 1):
        started = tool.registered_review_ledger(
            action="start_attempt",
            bundle_sha256=bundle,
            role=role,
            started_at=f"2026-07-24T11:0{index}:00+09:00",
            session_id="main-session",
        )
        assert started["decision"] == "allow"
        attempt_id = started["result"]["attempt_id"]
        recorded = tool.registered_review_ledger(
            action="record_result",
            attempt_id=attempt_id,
            outcome="PASS",
            finding_classes=["no_findings"],
            completed_at=f"2026-07-24T11:1{index}:00+09:00",
            session_id="main-session",
        )
        assert recorded["decision"] == "allow"
        assert recorded["result"]["verdict"] == "PASS"
        replayed_result = tool.registered_review_ledger(
            action="record_result",
            attempt_id=attempt_id,
            outcome="PASS",
            finding_classes=["no_findings"],
            completed_at=f"2026-07-24T11:1{index}:00+09:00",
            session_id="main-session",
        )
        assert replayed_result["decision"] == "allow"
        assert replayed_result["write_count"] == 0
        assert replayed_result["idempotency_result"] == "reconciled"

    status = tool.registered_review_ledger(
        action="status", bundle_sha256=bundle, session_id="main-session"
    )
    assert status["decision"] == "allow"
    assert len(status["result"]["attempts"]) == 2

    final = tool.registered_review_ledger(
        action="finalize",
        bundle_sha256=bundle,
        current_bundle_sha256=bundle,
        session_id="main-session",
    )
    assert final["decision"] == "allow"
    assert final["result"]["decision"] == "CONVERGED"
    assert db.is_file()
    assert db.is_relative_to(tmp_path)
    assert _snapshot(canonical_paths) == canonical_before
    clear_session_vars(tokens)


def test_conflicting_freeze_timestamp_is_not_adopted_as_replay(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    tokens = _owner_context()
    bundle = "f" * 64
    first = tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=["security-review"],
        created_at="2026-07-24T16:00:00+09:00",
        session_id="main-session",
    )
    assert first["decision"] == "allow"

    conflict = tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=["security-review"],
        created_at="2026-07-24T16:00:01+09:00",
        session_id="main-session",
    )
    assert conflict["decision"] == "deny_owner_unavailable"
    assert conflict["write_count"] == 0
    clear_session_vars(tokens)


def test_commit_then_response_loss_reconciles_exact_start_attempt(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    tokens = _owner_context()
    bundle = "b" * 64
    assert tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=["eng-review"],
        created_at="2026-07-24T12:00:00+09:00",
        session_id="main-session",
    )["decision"] == "allow"

    real_run = subprocess.run

    def committed_but_lost(*args, **kwargs):
        completed = real_run(*args, **kwargs)
        assert completed.returncode == 0
        return subprocess.CompletedProcess(
            completed.args, 1, stdout="", stderr="lost response"
        )

    monkeypatch.setattr(tool.subprocess, "run", committed_but_lost)
    result = tool.registered_review_ledger(
        action="start_attempt",
        bundle_sha256=bundle,
        role="eng-review",
        started_at="2026-07-24T12:01:00+09:00",
        session_id="main-session",
    )
    assert result["decision"] == "allow"
    assert result["idempotency_result"] == "reconciled"
    assert result["readback"] == "passed"
    clear_session_vars(tokens)


def test_commit_then_response_loss_reconciles_exact_record_result(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    tokens = _owner_context()
    bundle = "d" * 64
    assert tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=["security-review"],
        created_at="2026-07-24T14:00:00+09:00",
        session_id="main-session",
    )["decision"] == "allow"
    attempt_id = tool.registered_review_ledger(
        action="start_attempt",
        bundle_sha256=bundle,
        role="security-review",
        started_at="2026-07-24T14:01:00+09:00",
        session_id="main-session",
    )["result"]["attempt_id"]

    real_run = subprocess.run

    def committed_but_lost(*args, **kwargs):
        completed = real_run(*args, **kwargs)
        assert completed.returncode == 0
        return subprocess.CompletedProcess(
            completed.args, 1, stdout="", stderr="lost response"
        )

    monkeypatch.setattr(tool.subprocess, "run", committed_but_lost)
    result = tool.registered_review_ledger(
        action="record_result",
        attempt_id=attempt_id,
        outcome="REQUEST_CHANGES",
        finding_classes=["security"],
        completed_at="2026-07-24T14:02:00+09:00",
        session_id="main-session",
    )
    assert result["decision"] == "allow"
    assert result["idempotency_result"] == "reconciled"
    assert result["result"] == {
        "attempt_id": attempt_id,
        "status": "COMPLETED",
        "verdict": "REQUEST_CHANGES",
    }
    clear_session_vars(tokens)


def test_conflicting_record_result_is_not_adopted_as_replay(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    tokens = _owner_context()
    bundle = "e" * 64
    tool.registered_review_ledger(
        action="freeze",
        bundle_sha256=bundle,
        required_roles=["security-review"],
        created_at="2026-07-24T15:00:00+09:00",
        session_id="main-session",
    )
    attempt_id = tool.registered_review_ledger(
        action="start_attempt",
        bundle_sha256=bundle,
        role="security-review",
        started_at="2026-07-24T15:01:00+09:00",
        session_id="main-session",
    )["result"]["attempt_id"]
    first = tool.registered_review_ledger(
        action="record_result",
        attempt_id=attempt_id,
        outcome="REQUEST_CHANGES",
        finding_classes=["security"],
        completed_at="2026-07-24T15:02:00+09:00",
        session_id="main-session",
    )
    assert first["decision"] == "allow"

    conflict = tool.registered_review_ledger(
        action="record_result",
        attempt_id=attempt_id,
        outcome="REQUEST_CHANGES",
        finding_classes=["correctness"],
        completed_at="2026-07-24T15:02:00+09:00",
        session_id="main-session",
    )
    assert conflict["decision"] == "deny_owner_unavailable"
    assert conflict["write_count"] == 0
    clear_session_vars(tokens)


def test_owner_subprocess_is_fixed_isolated_and_shell_free(monkeypatch, tmp_path):
    import tools.registered_review_ledger as tool

    _ready(monkeypatch, tool, tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "bundle_sha256": "c" * 64,
                    "required_roles": ["eng-review"],
                    "created_at": "2026-07-24T13:00:00+09:00",
                }
            )
            + "\n",
            stderr="",
        )

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    monkeypatch.setattr(
        tool,
        "_readback_mutation",
        lambda *_args, **_kwargs: {
            "bundle_sha256": "c" * 64,
            "required_roles": ["eng-review"],
            "created_at": "2026-07-24T13:00:00+09:00",
        },
    )
    tokens = _owner_context()
    result = tool.registered_review_ledger(
        action="freeze",
        bundle_sha256="c" * 64,
        required_roles=["eng-review"],
        created_at="2026-07-24T13:00:00+09:00",
        session_id="main-session",
    )
    assert result["decision"] == "allow"
    assert captured["argv"] == [
        tool._PYTHON,
        "-I",
        "-B",
        str(tool._OWNER_WRAPPER),
        "--db",
        str(tool._CANONICAL_DB),
        "freeze",
    ]
    assert captured["kwargs"]["cwd"] == tool._OWNER_ROOT
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 30
    assert set(captured["kwargs"]["env"]) == {"LANG", "PATH", "TZ"}
    clear_session_vars(tokens)


@pytest.mark.parametrize(
    "stdout",
    [
        '{"bundle_sha256":"a","bundle_sha256":"b"}\n',
        '{"bundle_sha256":NaN}\n',
        '{"bundle_sha256":Infinity}\n',
    ],
)
def test_owner_json_rejects_duplicate_keys_and_nonfinite_values(stdout):
    import tools.registered_review_ledger as tool

    completed = subprocess.CompletedProcess(
        ["owner"], 0, stdout=stdout, stderr=""
    )
    with pytest.raises(tool.OwnerResponseError):
        tool._parse_owner_result(completed)


def test_status_and_finalize_nested_results_are_schema_closed():
    import tools.registered_review_ledger as tool

    bundle = "a" * 64
    slot = {
        "role": "security-review",
        "latest_status": "MISSING",
        "latest_verdict": None,
        "latest_attempt_no": None,
        "latest_review_verdict": None,
        "latest_review_attempt_no": None,
    }
    valid_status = {
        "bundle_sha256": bundle,
        "required_roles": ["security-review"],
        "created_at": "2026-07-24T17:00:00+09:00",
        "slots": [slot],
        "attempts": [],
    }
    assert tool._normalize_read_result(
        "status", {"bundle_sha256": bundle}, valid_status
    ) == valid_status

    bad_slot = dict(slot)
    bad_slot["unexpected"] = True
    malformed_status = dict(valid_status)
    malformed_status["slots"] = [bad_slot]
    assert (
        tool._normalize_read_result(
            "status", {"bundle_sha256": bundle}, malformed_status
        )
        is None
    )

    malformed_attempt = dict(valid_status)
    malformed_attempt["slots"] = [
        dict(slot, latest_status="RUNNING", latest_attempt_no=1)
    ]
    malformed_attempt["attempts"] = [
        {
            "attempt_id": True,
            "role": "security-review",
            "attempt_no": 1,
            "status": "RUNNING",
            "started_at": "2026-07-24T17:01:00+09:00",
            "completed_at": None,
            "verdict": None,
            "safe_summary": None,
        }
    ]
    assert (
        tool._normalize_read_result(
            "status", {"bundle_sha256": bundle}, malformed_attempt
        )
        is None
    )

    malformed_finalize = {
        "bundle_sha256": bundle,
        "current_bundle_sha256": bundle,
        "decision": "CONVERGED",
        "slots": [dict(slot, latest_status="UNKNOWN")],
    }
    assert (
        tool._normalize_read_result(
            "finalize",
            {"bundle_sha256": bundle, "current_bundle_sha256": bundle},
            malformed_finalize,
        )
        is None
    )
