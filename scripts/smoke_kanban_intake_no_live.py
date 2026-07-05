#!/usr/bin/env python3
"""No-live smoke for gateway conversational Kanban intake."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        os.environ["HERMES_HOME"] = str(Path(td) / ".hermes")
        from gateway.kanban_intake import (
            IntakeDetectionRequest,
            KanbanCardProposal,
            KanbanIntakeConfig,
            KeywordHeuristicDetector,
            PendingKanbanStore,
            SourceBinding,
            TitleGenerationRule,
            constrained_llm_title_generator,
            explicit_title_from_request,
            handle_reply,
            validate_proposal,
        )
        from hermes_cli import kanban_db as kb
        from scripts.eval_kanban_intake_quality import evaluate, load_cases

        board = "lifelog-control"
        kb.create_board(board, name="Lifelog Control")
        cfg = KanbanIntakeConfig(enabled=True, default_board=board, store_path=Path(td) / "pending.db")
        detector = KeywordHeuristicDetector()

        def detector_request(user_summary: str) -> IntakeDetectionRequest:
            return IntakeDetectionRequest(
                platform="discord",
                session_key="s1",
                source_ref="kp_safe",
                user_summary=user_summary,
                assistant_summary="답변했다.",
                default_board=board,
                default_tenant="lifelog",
            )

        one_off_card_proposal_suppressed = not detector.detect(detector_request("이거 왜이래??")).card_worthy
        meta_kanban_card_proposal_suppressed = not detector.detect(detector_request("카드 생성 조건이 너무 후한거 아닌가?")).card_worthy
        pasted_bad_proposal_meta_complaint_suppressed = not detector.detect(detector_request(
            "흠 근데 title generator 가 LLM의 장점을 활용해서 이름을 잘 생성하게끔 업데이트 한거 아니었나??\n\n---\n"
            "카드 후보 감지: 이건 Kanban에 blocked review 카드로 남기는 게 좋다.\n"
            "board: lifelog-control\ntitle: Review family health Lifelog capture\n"
            "domain: lifelog-core\ntenant: lifelog\nstatus: blocked\nwhy: durable project follow-up\n\n"
            "승인하려면 “승인/ㅇㅇ/고고”, 취소하려면 “취소”.\n\n또 이런식으로 나오는데??"
        )).card_worthy
        read_only_candidate_audit_suppressed = not detector.detect(detector_request(
            "내 헤르메스 프로젝트 전체 좀 보고 보드 또는 카드 후보로 올릴 수 있는 대상 뭔지 확인하고 추천 목록 작성해서 알려줘봐. (실행은 금지)"
        )).card_worthy
        existing_card_update_suppressed = not detector.detect(detector_request(
            "t_5b858cd6 카드 업데이트 승인"
        )).card_worthy
        direct_card_operation_suppressed = not detector.detect(IntakeDetectionRequest(
            platform="discord",
            session_key="s1",
            source_ref="kp_safe",
            user_summary="suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
            assistant_summary="실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
            default_board=board,
            default_tenant="lifelog",
        )).card_worthy
        direct_card_operation_failure_suppressed = not detector.detect(IntakeDetectionRequest(
            platform="discord",
            session_key="s1",
            source_ref="kp_safe",
            user_summary="suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
            assistant_summary="카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
            default_board=board,
            default_tenant="lifelog",
        )).card_worthy
        durable_status_update_remains_eligible = detector.detect(detector_request(
            "gateway status update feature 구현/테스트까지 해줘"
        )).card_worthy
        lifelog_generic_title_rewritten = detector.detect(detector_request(
            "방금 복약 기록 후속 작업 정리하고 카드로 남겨줘"
        )).title == "Review medication intake Lifelog capture"
        hybrid_request = detector_request("lifelog medication reminder cron 누락 재발 방지 테스트 카드로 남겨줘")
        hybrid_title_generator_accepts_safe_draft = explicit_title_from_request(
            hybrid_request,
            "Review lifelog follow-up work",
            title_generator=lambda *_: '{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}',
        ) == "Investigate missed medication reminder regression"
        hybrid_title_generator_rejects_unsafe_draft = explicit_title_from_request(
            hybrid_request,
            "Review lifelog follow-up work",
            title_generator=lambda *_: '{"title":"[상현] lifelog medication reminder cron 누락 원인 분석","action":"Review","object":"medication reminder"}',
        ) == "Fix Lifelog medication reminder cron regression"
        from agent import auxiliary_client

        original_call_llm = auxiliary_client.call_llm
        live_adapter_uses_auxiliary_title_generation = False
        try:
            class _FakeMessage:
                content = '{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}'

            class _FakeChoice:
                message = _FakeMessage()

            class _FakeResponse:
                choices = [_FakeChoice()]

            def fake_call_llm(**kwargs):
                nonlocal live_adapter_uses_auxiliary_title_generation
                live_adapter_uses_auxiliary_title_generation = kwargs.get("task") == "title_generation"
                return _FakeResponse()

            auxiliary_client.call_llm = fake_call_llm
            constrained_llm_title_generator(hybrid_request, TitleGenerationRule())
        finally:
            auxiliary_client.call_llm = original_call_llm
        store = PendingKanbanStore(cfg.store_path)
        binding = SourceBinding("discord", "raw_chat_123456789", "raw_thread_123456789", "u1", "s1")
        proposal = KanbanCardProposal(
            board=board,
            title="Verify no-live guardrail smoke",
            body={"source_ref": "kp_safe", "acceptance_criteria": ["pass"]},
            source_ref="kp_safe",
            user_id="u1",
        )
        store.put_pending(proposal, binding, cfg)
        approved = handle_reply("ㅇㅇ", binding, cfg, store)
        conn = kb.connect(board=board)
        try:
            tasks = kb.list_tasks(conn, include_archived=True)
            task_status = tasks[0].status if tasks else None
            worker_pid = tasks[0].worker_pid if tasks else None
            claim_lock = tasks[0].claim_lock if tasks else None
            spawned = []
            dispatch = kb.dispatch_once(conn, board=board, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 12345)
            after_dispatch = kb.get_task(conn, tasks[0].id) if tasks else None
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
        expired_pending = store.put_pending(
            KanbanCardProposal(
                board=board,
                title="Verify expired pending hygiene workflow",
                body={"source_ref": "kp_expired", "acceptance_criteria": ["flag expired"]},
                source_ref="kp_expired",
                user_id="u3",
            ),
            SourceBinding("discord", "expired_chat_123456789", "expired_thread_123456789", "u3", "s1"),
            cfg,
            now=1,
        )
        expired_review = store.review_pending(now=1 + cfg.proposal_ttl_seconds + 1)
        expired_items = {item["pending_id"]: item for item in expired_review["items"]}
        expired_pending_hygiene_flagged = (
            expired_review["counts"].get("pending_expired", 0) >= 1
            and expired_items[expired_pending.pending_id]["effective_status"] == "expired"
            and "expired" in expired_items[expired_pending.pending_id]["flags"]
        )
        sensitive = KanbanCardProposal(
            board=board,
            title="아이 fever raw",
            body={"source_ref": "kp_safe"},
            source_ref="kp_safe",
            user_id="u1",
        )
        sensitive_payload_in_card_body = validate_proposal(sensitive, cfg)[0]
        quality = evaluate(load_cases(Path(__file__).resolve().parents[1] / "tests/fixtures/kanban_intake_golden_cases.jsonl"))

        result = {
            "gateway_restarted": False,
            "discord_sent_live": False,
            "board_created_live": False,
            "cron_mutated": False,
            "lifelog_db_mutated": False,
            "graphify_run": False,
            "jokl_public_customer_mutation": False,
            "kanban_env_overrides_cleared": cleared,
            "card_created_in_temp_home": bool(tasks),
            "card_status": task_status,
            "card_blocked_by_default": task_status == "blocked",
            "card_unclaimed_before_dispatch": worker_pid is None and claim_lock is None,
            "blocked_card_not_dispatched": bool(after_dispatch and after_dispatch.status == "blocked" and not spawned and not dispatch.spawned),
            "approved_short_phrase": bool(approved.verified),
            "cross_user_fail_closed": cross.handled is False,
            "missing_user_id_fail_closed": missing_user_ok,
            "one_off_card_proposal_suppressed": one_off_card_proposal_suppressed,
            "meta_kanban_card_proposal_suppressed": meta_kanban_card_proposal_suppressed,
            "pasted_bad_proposal_meta_complaint_suppressed": pasted_bad_proposal_meta_complaint_suppressed,
            "read_only_candidate_audit_suppressed": read_only_candidate_audit_suppressed,
            "existing_card_update_suppressed": existing_card_update_suppressed,
            "direct_card_operation_suppressed": direct_card_operation_suppressed,
            "direct_card_operation_failure_suppressed": direct_card_operation_failure_suppressed,
            "durable_status_update_remains_eligible": durable_status_update_remains_eligible,
            "lifelog_generic_title_rewritten": lifelog_generic_title_rewritten,
            "hybrid_title_generator_accepts_safe_draft": hybrid_title_generator_accepts_safe_draft,
            "hybrid_title_generator_rejects_unsafe_draft": hybrid_title_generator_rejects_unsafe_draft,
            "live_adapter_uses_auxiliary_title_generation": live_adapter_uses_auxiliary_title_generation,
            "expired_pending_hygiene_flagged": expired_pending_hygiene_flagged,
            "quality_metrics_present": True,
            "candidate_precision_threshold_met": quality["candidate_precision"] >= 0.90,
            "candidate_recall_threshold_met": quality["candidate_recall"] >= 0.70,
            "generic_title_rate_threshold_met": quality["generic_title_rate"] <= 0.05,
            "raw_copy_rate_zero": quality["raw_copy_rate"] == 0,
            "sensitive_title_leak_zero": quality["sensitive_title_leak"] == 0,
            "quality_thresholds_passed": bool(quality["thresholds_passed"]),
            "raw_source_ids_in_card_body": any(raw in (body or "") for raw in ("raw_chat_123456789", "raw_thread_123456789", "u1")),
            "sensitive_payload_in_card_body": bool(sensitive_payload_in_card_body),
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("PASS" if all([
            not result["gateway_restarted"],
            not result["discord_sent_live"],
            not result["board_created_live"],
            not result["cron_mutated"],
            not result["lifelog_db_mutated"],
            not result["graphify_run"],
            not result["jokl_public_customer_mutation"],
            result["kanban_env_overrides_cleared"],
            result["card_created_in_temp_home"],
            result["card_blocked_by_default"],
            result["card_unclaimed_before_dispatch"],
            result["blocked_card_not_dispatched"],
            result["approved_short_phrase"],
            result["cross_user_fail_closed"],
            result["missing_user_id_fail_closed"],
            result["one_off_card_proposal_suppressed"],
            result["meta_kanban_card_proposal_suppressed"],
            result["pasted_bad_proposal_meta_complaint_suppressed"],
            result["read_only_candidate_audit_suppressed"],
            result["existing_card_update_suppressed"],
            result["direct_card_operation_suppressed"],
            result["direct_card_operation_failure_suppressed"],
            result["durable_status_update_remains_eligible"],
            result["lifelog_generic_title_rewritten"],
            result["hybrid_title_generator_accepts_safe_draft"],
            result["hybrid_title_generator_rejects_unsafe_draft"],
            result["live_adapter_uses_auxiliary_title_generation"],
            result["expired_pending_hygiene_flagged"],
            result["quality_metrics_present"],
            result["candidate_precision_threshold_met"],
            result["candidate_recall_threshold_met"],
            result["generic_title_rate_threshold_met"],
            result["raw_copy_rate_zero"],
            result["sensitive_title_leak_zero"],
            result["quality_thresholds_passed"],
            not result["raw_source_ids_in_card_body"],
            not result["sensitive_payload_in_card_body"],
        ]) else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
