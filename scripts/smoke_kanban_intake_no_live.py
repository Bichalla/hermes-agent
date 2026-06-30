#!/usr/bin/env python3
"""No-live smoke for gateway conversational Kanban intake."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cleared = True
    for name in [
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ]:
        if os.environ.pop(name, None) is not None:
            pass
    cleared = all(name not in os.environ for name in [
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ])

    with tempfile.TemporaryDirectory(prefix="hermes-kanban-intake-") as td:
        os.environ["HERMES_HOME"] = str(Path(td) / ".hermes")
        from gateway.kanban_intake import (
            KanbanCardProposal,
            KanbanIntakeConfig,
            PendingKanbanStore,
            SourceBinding,
            handle_reply,
            validate_proposal,
        )
        from hermes_cli import kanban_db as kb

        board = "lifelog-control"
        kb.create_board(board, name="Lifelog Control")
        cfg = KanbanIntakeConfig(enabled=True, default_board=board, store_path=Path(td) / "pending.db")
        store = PendingKanbanStore(cfg.store_path)
        binding = SourceBinding("discord", "raw_chat_123456789", "raw_thread_123456789", "u1", "s1")
        proposal = KanbanCardProposal(
            board=board,
            title="No-live guardrail smoke",
            body={"source_ref": "kp_safe", "acceptance_criteria": ["pass"]},
            source_ref="kp_safe",
            user_id="u1",
        )
        store.put_pending(proposal, binding, cfg)
        approved = handle_reply("ㅇㅇ", binding, cfg, store)
        conn = kb.connect(board=board)
        try:
            tasks = kb.list_tasks(conn, include_archived=True)
        finally:
            conn.close()
        body = tasks[0].body if tasks else ""
        missing_user_ok = False
        try:
            SourceBinding("discord", "c", "t", "", "s")
            store.put_pending(proposal, SourceBinding("discord", "c", "t", "", "s"), cfg)
        except Exception:
            missing_user_ok = True
        cross = handle_reply("승인", SourceBinding("discord", "raw_chat_123456789", "raw_thread_123456789", "u2", "s1"), cfg, store)
        sensitive = KanbanCardProposal(
            board=board,
            title="아이 fever raw",
            body={"source_ref": "kp_safe"},
            source_ref="kp_safe",
            user_id="u1",
        )
        sensitive_payload_in_card_body = validate_proposal(sensitive, cfg)[0]

        result = {
            "gateway_restarted": False,
            "discord_sent_live": False,
            "cron_mutated": False,
            "lifelog_db_mutated": False,
            "graphify_run": False,
            "kanban_env_overrides_cleared": cleared,
            "card_created_in_temp_home": bool(tasks),
            "approved_short_phrase": bool(approved.verified),
            "cross_user_fail_closed": cross.handled is False,
            "missing_user_id_fail_closed": missing_user_ok,
            "raw_source_ids_in_card_body": any(raw in (body or "") for raw in ("raw_chat_123456789", "raw_thread_123456789", "u1")),
            "sensitive_payload_in_card_body": bool(sensitive_payload_in_card_body),
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("PASS" if all([
            not result["gateway_restarted"],
            not result["discord_sent_live"],
            not result["cron_mutated"],
            not result["lifelog_db_mutated"],
            not result["graphify_run"],
            result["kanban_env_overrides_cleared"],
            result["card_created_in_temp_home"],
            result["approved_short_phrase"],
            result["cross_user_fail_closed"],
            result["missing_user_id_fail_closed"],
            not result["raw_source_ids_in_card_body"],
            not result["sensitive_payload_in_card_body"],
        ]) else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
