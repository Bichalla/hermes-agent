"""Explicit operator migration for exact-retry-safe Kanban status memory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from hermes_cli import kanban_db as kb


_REPORT_SCHEMA = "kanban-status-memory-capability-migration/v1"


def _strict_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected exactly 'true' or 'false'")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicitly migrate one Kanban DB for retry-safe status memory."
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--dry-run", required=True, type=_strict_bool)
    parser.add_argument("--json", required=True, action="store_true")
    return parser


def _open_existing(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("Kanban DB path does not exist as a regular file")
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _consistent_snapshot(source_path: Path, destination_path: Path) -> None:
    wal_path = source_path.with_name(source_path.name + "-wal")
    if wal_path.exists() and wal_path.stat().st_size != 0:
        raise sqlite3.OperationalError("source WAL must be checkpointed before dry-run")
    before_digest = hashlib.sha256(source_path.read_bytes()).digest()
    source = sqlite3.connect(
        source_path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5.0
    )
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
    db_path = args.db.expanduser().resolve()
    base = {
        "schema": _REPORT_SCHEMA,
        "db": str(db_path),
        "dry_run": args.dry_run,
        "capability": "exact_retry_safe_status_memory",
    }
    scratch: tempfile.TemporaryDirectory[str] | None = None
    open_path = db_path
    if args.dry_run and db_path.exists() and db_path.is_file():
        scratch = tempfile.TemporaryDirectory(prefix="kanban-capability-migrate-")
        open_path = Path(scratch.name) / db_path.name
        try:
            _consistent_snapshot(db_path, open_path)
        except (sqlite3.Error, OSError) as exc:
            scratch.cleanup()
            _emit({**base, "ok": False, "error": str(exc)})
            return 1
    try:
        conn = _open_existing(open_path, read_only=False)
        try:
            report = kb.migrate_status_memory_idempotency(
                conn, dry_run=args.dry_run
            )
        finally:
            conn.close()
        if not args.dry_run:
            readback = _open_existing(db_path, read_only=True)
            try:
                post_commit_ready = (
                    kb._status_memory_idempotency_state(readback) == "ready"
                )
                integrity_row = readback.execute("PRAGMA integrity_check").fetchone()
                integrity_ok = integrity_row is not None and integrity_row[0] == "ok"
                receipt_rows = int(
                    readback.execute(
                        f"SELECT COUNT(*) FROM {kb.STATUS_MEMORY_IDEMPOTENCY_TABLE}"
                    ).fetchone()[0]
                )
            finally:
                readback.close()
            if not post_commit_ready or not integrity_ok:
                raise kb.StatusMemoryCapabilityError(
                    "status-memory post-commit readback failed"
                )
            report.update(
                {
                    "post_commit_reopen": True,
                    "post_commit_ready": True,
                    "integrity_check": "ok",
                    "post_commit_receipt_rows": receipt_rows,
                }
            )
    except (FileNotFoundError, sqlite3.Error, kb.StatusMemoryCapabilityError) as exc:
        _emit({**base, "ok": False, "error": str(exc)})
        if scratch is not None:
            scratch.cleanup()
        return 1

    _emit({**base, "ok": True, **report})
    if scratch is not None:
        scratch.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())