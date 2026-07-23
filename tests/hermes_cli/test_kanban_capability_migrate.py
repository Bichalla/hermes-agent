"""Focused tests for the explicit Kanban capability migration CLI."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_capability_migrate as migrate_cli


def _legacy_db(path: Path) -> None:
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    with kb.connect(db_path=path) as conn:
        kb.create_task(conn, title="migration target")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _has_capability(path: Path) -> bool:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (kb.STATUS_MEMORY_IDEMPOTENCY_TABLE,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_dry_run_reports_without_migrating(tmp_path, capsys):
    db_path = tmp_path / "kanban.db"
    _legacy_db(db_path)

    exit_code = migrate_cli.main(
        ["--db", str(db_path), "--dry-run", "true", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_change"] is True
    assert payload["changed"] is False
    assert _has_capability(db_path) is False


def test_apply_migrates_and_second_apply_is_idempotent(tmp_path, capsys):
    db_path = tmp_path / "kanban.db"
    _legacy_db(db_path)

    first_exit = migrate_cli.main(
        ["--db", str(db_path), "--dry-run", "false", "--json"]
    )
    first = json.loads(capsys.readouterr().out)
    second_exit = migrate_cli.main(
        ["--db", str(db_path), "--dry-run", "false", "--json"]
    )
    second = json.loads(capsys.readouterr().out)

    assert first_exit == second_exit == 0
    assert first["changed"] is True
    assert first["migrated_after"] is True
    assert first["exact_schema_ready"] is True
    assert first["post_commit_reopen"] is True
    assert first["post_commit_ready"] is True
    assert first["integrity_check"] == "ok"
    assert first["post_commit_receipt_rows"] == 0
    assert second["changed"] is False
    assert second["migrated_before"] is True
    assert second["post_commit_reopen"] is True
    assert second["integrity_check"] == "ok"
    assert _has_capability(db_path) is True


def test_subprocess_entrypoint_emits_json_and_requires_explicit_boolean(tmp_path):
    db_path = tmp_path / "kanban.db"
    _legacy_db(db_path)
    command = [
        sys.executable,
        "-m",
        "hermes_cli.kanban_capability_migrate",
        "--db",
        str(db_path),
        "--dry-run",
        "true",
        "--json",
    ]

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert payload["db"] == str(db_path.resolve())
    assert payload["dry_run"] is True
    assert _has_capability(db_path) is False

    invalid = subprocess.run(
        [*command[:-2], "yes", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert _has_capability(db_path) is False


def test_missing_db_fails_closed_without_creating_file(tmp_path, capsys):
    db_path = tmp_path / "missing.db"

    exit_code = migrate_cli.main(
        ["--db", str(db_path), "--dry-run", "false", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert db_path.exists() is False


def test_two_concurrent_status_memory_migrations_report_one_change(tmp_path):
    db_path = tmp_path / "kanban.db"
    _legacy_db(db_path)

    def apply():
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            return kb.migrate_status_memory_idempotency(conn, dry_run=False)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(apply), pool.submit(apply))]

    assert sorted(result["changed"] for result in results) == [False, True]
    assert all(result["exact_schema_ready"] is True for result in results)
    assert _has_capability(db_path) is True