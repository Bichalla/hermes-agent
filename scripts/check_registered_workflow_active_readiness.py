#!/usr/bin/env python3
"""Explicit physically read-only active readiness check for registered-workflow."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _wal_is_clear(path: Path) -> bool:
    wal = path.with_name(path.name + "-wal")
    return not wal.exists() or wal.stat().st_size == 0


def _board_db_ready(path: Path) -> bool:
    from hermes_cli import kanban_db as kb
    from tools.registered_local_workflow import _sha256

    try:
        path = path.expanduser().resolve()
        if not path.is_file() or not _wal_is_clear(path):
            return False
        before_digest = _sha256(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            ready = kb._status_memory_idempotency_state(connection) == "ready"
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity is not None and integrity[0] == "ok"
        finally:
            connection.close()
        return (
            ready
            and integrity_ok
            and _wal_is_clear(path)
            and _sha256(path) == before_digest
        )
    except (OSError, sqlite3.Error):
        return False


def _canonical_board_paths() -> list[Path]:
    from hermes_cli import kanban_db as kb

    return [
        kb.kanban_db_path(str(metadata["slug"])).expanduser().resolve()
        for metadata in kb.list_boards(include_archived=True)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    expectation = parser.add_mutually_exclusive_group(required=True)
    expectation.add_argument("--expect-absent", action="store_true")
    expectation.add_argument("--expect-ready", action="store_true")
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args()

    from tools.registered_local_workflow import (
        _DEPENDENCY_DIGESTS,
        _feature_enabled,
        _pending_dependencies_ready,
        _sha256,
        check_registered_workflow_requirements,
    )
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import _clear_tool_defs_cache, get_tool_definitions

    enabled = _feature_enabled()
    pending_ready = _pending_dependencies_ready()
    ready = check_registered_workflow_requirements()
    dependency_matches = [
        path.is_file() and _sha256(path) == expected
        for path, expected in _DEPENDENCY_DIGESTS.items()
    ]
    dependencies_ready = all(dependency_matches)
    discord_toolsets = _get_platform_tools(load_config() or {}, "discord")
    discord_enabled = "registered-workflow" in discord_toolsets
    _clear_tool_defs_cache()
    definitions = get_tool_definitions(
        ["registered-workflow"], quiet_mode=True, skip_tool_search_assembly=True
    )
    schema_present = any(
        item.get("function", {}).get("name") == "registered_local_workflow"
        for item in definitions
    )
    _clear_tool_defs_cache()
    try:
        board_paths = _canonical_board_paths()
        board_results = [_board_db_ready(path) for path in board_paths]
    except (KeyError, OSError, TypeError, ValueError):
        board_paths = []
        board_results = []
    board_dbs_ready = bool(board_results) and all(board_results)
    passed = (
        not enabled and not ready and not discord_enabled and not schema_present
        if args.expect_absent
        else enabled
        and ready
        and pending_ready
        and dependencies_ready
        and discord_enabled
        and schema_present
        and board_dbs_ready
    )
    print(
        json.dumps(
            {
                "schema": "registered-workflow-active-readiness/v1",
                "mode": "explicit-immutable-read-only",
                "feature_enabled": enabled,
                "tool_ready": ready,
                "pending_db_ready": pending_ready,
                "dependencies_ready": dependencies_ready,
                "dependency_count": len(dependency_matches),
                "discord_toolset_enabled": discord_enabled,
                "schema_present": schema_present,
                "board_db_count": len(board_paths),
                "board_dbs_ready": board_dbs_ready,
                "expect_absent": args.expect_absent,
                "expect_ready": args.expect_ready,
                "passed": passed,
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
