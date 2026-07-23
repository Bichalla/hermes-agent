"""Explicit pending-intake transition-audit migration tests (temp DB only)."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from gateway.kanban_intake import (
    KanbanCardProposal,
    KanbanIntakeConfig,
    PendingKanbanStore,
    SourceBinding,
    migrate_transition_audit,
    transition_audit_ready,
)
from hermes_cli import kanban_intake_migrate as migrate_cli


AUDIT_COLUMNS = [
    "id",
    "invocation_key",
    "pending_id",
    "from_status",
    "to_status",
    "reason_code",
    "occurred_at",
    "retain_until",
]


def _seed(path: Path, *, status: str = "pending") -> str:
    cfg = KanbanIntakeConfig(enabled=True, default_board="test", store_path=path)
    store = PendingKanbanStore(path)
    with sqlite3.connect(path) as count_conn:
        try:
            ordinal = count_conn.execute("SELECT COUNT(*) FROM kanban_intake_pending").fetchone()[0]
        except sqlite3.OperationalError:
            ordinal = 0
    pending = store.put_pending(
        KanbanCardProposal(
            board="test",
            title="Implement pending migration tests",
            body={"acceptance_criteria": ["temp DB only"]},
            source_ref="kp_source",
            user_id="u1",
        ),
        SourceBinding("discord", "c1", f"t{ordinal}", "u1", "s1"),
        cfg,
        now=100,
    )
    if status != "pending":
        with store.connect() as conn:
            conn.execute(
                "UPDATE kanban_intake_pending SET status = ? WHERE pending_id = ?",
                (status, pending.pending_id),
            )
            conn.commit()
    return pending.pending_id


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _checkpoint_for_immutable_dry_run(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def test_ordinary_store_connect_does_not_create_transition_audit(tmp_path):
    path = tmp_path / "pending.db"
    _seed(path)
    assert "kanban_intake_transition_audit" not in _table_names(path)


def test_dry_run_reports_without_mutating_schema_or_rows(tmp_path, capsys):
    path = tmp_path / "pending.db"
    pending_id = _seed(path, status="needs_revalidation")
    _checkpoint_for_immutable_dry_run(path)

    before = path.read_bytes()
    code = migrate_cli.main(["--db", str(path), "--dry-run", "true", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["would_change"] is True
    assert payload["changed"] is False
    assert payload["baseline_rows"] == 1
    assert path.read_bytes() == before
    assert "kanban_intake_transition_audit" not in _table_names(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM kanban_intake_pending WHERE pending_id = ?", (pending_id,)
        ).fetchone()[0] == "needs_revalidation"


def test_dry_run_fails_closed_without_touching_uncheckpointed_wal(tmp_path, capsys):
    path = tmp_path / "pending.db"
    _seed(path)
    wal = path.with_name(path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    before_db = path.read_bytes()
    before_wal = wal.read_bytes()

    code = migrate_cli.main(["--db", str(path), "--dry-run", "true", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["error_code"] == "source_not_stable"
    assert path.read_bytes() == before_db
    assert wal.read_bytes() == before_wal


def test_apply_creates_exact_append_only_schema_and_one_baseline_per_existing_row(tmp_path, capsys):
    path = tmp_path / "pending.db"
    ids = [
        _seed(path, status="pending"),
        _seed(path, status="needs_revalidation"),
    ]
    with sqlite3.connect(path) as conn:
        statuses_before = conn.execute(
            "SELECT pending_id, status, purge_after FROM kanban_intake_pending ORDER BY pending_id"
        ).fetchall()

    code = migrate_cli.main(["--db", str(path), "--dry-run", "false", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["changed"] is True
    assert payload["baseline_rows"] == 2
    assert payload["post_commit_reopen"] is True
    assert payload["post_commit_ready"] is True
    assert payload["integrity_check"] == "ok"
    assert payload["post_commit_audit_rows"] == 2
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert [row["name"] for row in conn.execute(
            "PRAGMA table_info(kanban_intake_transition_audit)"
        )] == AUDIT_COLUMNS
        statuses_after = conn.execute(
            "SELECT pending_id, status, purge_after FROM kanban_intake_pending ORDER BY pending_id"
        ).fetchall()
        audits = conn.execute(
            "SELECT invocation_key, pending_id, from_status, to_status, reason_code, retain_until "
            "FROM kanban_intake_transition_audit ORDER BY pending_id"
        ).fetchall()
    assert [tuple(row) for row in statuses_after] == [tuple(row) for row in statuses_before]
    assert {row["pending_id"] for row in audits} == set(ids)
    assert all(row["from_status"] == row["to_status"] for row in audits)
    assert all(row["reason_code"] == "legacy_state_observed" for row in audits)
    assert all(row["invocation_key"] == f"migration:v1:{row['pending_id']}:{row['to_status']}" for row in audits)
    expected_retain = {row[0]: row[2] for row in statuses_before}
    assert all(row["retain_until"] == expected_retain[row["pending_id"]] for row in audits)


def test_second_apply_is_idempotent_and_audit_rows_are_append_only(tmp_path, capsys):
    path = tmp_path / "pending.db"
    _seed(path)
    args = ["--db", str(path), "--dry-run", "false", "--json"]
    assert migrate_cli.main(args) == 0
    capsys.readouterr()
    assert migrate_cli.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False
    assert second["post_commit_reopen"] is True
    assert second["integrity_check"] == "ok"

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT id FROM kanban_intake_transition_audit"
        ).fetchone()
        assert row is not None
        try:
            conn.execute(
                "UPDATE kanban_intake_transition_audit SET reason_code='user_denied' WHERE id=?",
                (row[0],),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("transition audit UPDATE must be rejected")
        try:
            conn.execute("DELETE FROM kanban_intake_transition_audit WHERE id=?", (row[0],))
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("transition audit DELETE while owner row exists must be rejected")


def test_invalid_pending_schema_fails_closed_without_creating_audit_table(tmp_path, capsys):
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kanban_intake_pending (pending_id TEXT PRIMARY KEY, status TEXT)")

    code = migrate_cli.main(["--db", str(path), "--dry-run", "false", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error_code"] == "schema_invalid"
    assert "kanban_intake_transition_audit" not in _table_names(path)


def test_subprocess_contract_requires_exact_boolean_and_missing_db_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    command = [
        sys.executable,
        "-m",
        "hermes_cli.kanban_intake_migrate",
        "--db",
        str(missing),
        "--dry-run",
        "false",
        "--json",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error_code"] == "db_not_found"
    assert not missing.exists()

    invalid = subprocess.run(
        [*command[:-2], "yes", "--json"], text=True, capture_output=True, check=False
    )
    assert invalid.returncode == 2
    assert not missing.exists()


def test_two_concurrent_migrations_serialize_and_report_one_change(tmp_path):
    path = tmp_path / "pending.db"
    _seed(path)

    def apply():
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return migrate_transition_audit(conn, dry_run=False, now=101)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(apply), pool.submit(apply))]

    assert sorted(result["changed"] for result in results) == [False, True]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert transition_audit_ready(conn) is True
