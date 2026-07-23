"""Explicit operator migration for pending-intake transition audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from gateway.kanban_intake import (
    TRANSITION_AUDIT_TABLE,
    migrate_transition_audit,
    transition_audit_ready,
)

_SCHEMA = "kanban-intake-transition-audit-migration/v1"


def _strict_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected exactly 'true' or 'false'")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly install append-only pending transition audit."
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", required=True, type=_strict_bool)
    parser.add_argument("--json", required=True, action="store_true")
    return parser


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))


def _open_existing(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _consistent_snapshot(source_path: Path, destination_path: Path) -> None:
    wal_path = source_path.with_name(source_path.name + "-wal")
    if wal_path.exists() and wal_path.stat().st_size != 0:
        raise sqlite3.OperationalError("source WAL must be checkpointed before dry-run")
    before_digest = hashlib.sha256(source_path.read_bytes()).digest()
    source = sqlite3.connect(
        source_path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5.0
    )
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA busy_timeout=5000")
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise sqlite3.DatabaseError("snapshot integrity check failed")
        if wal_path.exists() and wal_path.stat().st_size != 0:
            raise sqlite3.OperationalError("source WAL appeared during dry-run")
        if hashlib.sha256(source_path.read_bytes()).digest() != before_digest:
            raise sqlite3.OperationalError("source changed during dry-run")
    finally:
        destination.close()
        source.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = args.db.expanduser().resolve()
    base = {"schema": _SCHEMA, "dry_run": args.dry_run}
    scratch: tempfile.TemporaryDirectory[str] | None = None
    open_path = path
    if args.dry_run and path.exists() and path.is_file():
        scratch = tempfile.TemporaryDirectory(prefix="kanban-intake-migrate-")
        open_path = Path(scratch.name) / path.name
        try:
            _consistent_snapshot(path, open_path)
        except (sqlite3.Error, OSError):
            scratch.cleanup()
            _emit({**base, "ok": False, "error_code": "source_not_stable"})
            return 1
    try:
        conn = _open_existing(open_path, read_only=False)
    except FileNotFoundError:
        _emit({**base, "ok": False, "error_code": "db_not_found"})
        if scratch is not None:
            scratch.cleanup()
        return 1
    try:
        try:
            report = migrate_transition_audit(conn, dry_run=args.dry_run)
        except (sqlite3.Error, ValueError, TypeError):
            _emit({**base, "ok": False, "error_code": "schema_invalid"})
            return 1
    finally:
        conn.close()
        if scratch is not None:
            scratch.cleanup()
    if not args.dry_run:
        try:
            readback = _open_existing(path, read_only=True)
            try:
                ready = transition_audit_ready(readback)
                integrity_row = readback.execute("PRAGMA integrity_check").fetchone()
                integrity_ok = integrity_row is not None and integrity_row[0] == "ok"
                audit_rows = int(
                    readback.execute(
                        f"SELECT COUNT(*) FROM {TRANSITION_AUDIT_TABLE}"
                    ).fetchone()[0]
                )
            finally:
                readback.close()
        except (sqlite3.Error, FileNotFoundError):
            _emit({**base, "ok": False, "error_code": "readback_failed"})
            return 1
        if not ready or not integrity_ok:
            _emit({**base, "ok": False, "error_code": "readback_failed"})
            return 1
        report.update(
            {
                "post_commit_ready": True,
                "post_commit_reopen": True,
                "integrity_check": "ok",
                "post_commit_audit_rows": audit_rows,
            }
        )
    _emit({**base, "ok": True, **report})
    return 0


if __name__ == "__main__":
    sys.exit(main())
