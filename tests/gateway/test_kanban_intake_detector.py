import json
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.kanban_intake import (
    AuxiliaryLLMDetector,
    KanbanCardProposal,
    KanbanIntakeConfig,
    KeywordHeuristicDetector,
    IntakeDetectionRequest,
    _infer_request_title_objects,
    _infer_title_object,
    card_proposal_eligibility,
    evaluate_title_quality,
    explicit_title_from_request,
    generated_title_from_json,
    minimize_for_detector,
    parse_detector_json,
    validate_proposal,
)


def req():
    return IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="gateway 구현 후속 작업",
        assistant_summary="tests pass",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )


def test_heuristic_detects_card_worthy_work():
    decision = KeywordHeuristicDetector().detect(req())
    assert decision.card_worthy is True
    assert decision.proposed_status == "blocked"
    assert decision.title == "Implement gateway follow-up work"


@pytest.mark.parametrize("user_summary", [
    "카드 생성 조건이 너무 후한거 아닌가?",
    "칸반보드가 필요한거는 장기로 이어지는 프로젝트에 한정해야되는 거 아니야??",
    "방금 다른 스레드에서 또 카드후보 타이틀 줬는데 변한게 없어 이거 왜이래??",
    "흠 근데 title generator 가 LLM의 장점을 활용해서 이름을 잘 생성하게끔 업데이트 한거 아니었나??\n\n---\n카드 후보 감지: 이건 Kanban에 blocked review 카드로 남기는 게 좋다.\nboard: lifelog-control\ntitle: Review family health Lifelog capture\ndomain: lifelog-core\ntenant: lifelog\nstatus: blocked\nwhy: durable project follow-up\nsafety: dispatch 없음, ready/running 아님, live DB/cron/Graphify/JÖKL public mutation 없음\n\n승인하려면 “승인/ㅇㅇ/고고”, 취소하려면 “취소”.\n\n또 이런식으로 나오는데??",
    "이거 왜이래??",
    "오늘 수면 기록 어떻게 하지?",
    "해커톤 관련해서 칸반보드 만드는 게 낫지 않나?",
])
def test_heuristic_does_not_propose_for_one_off_or_meta_kanban_discussion(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="답변했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


@pytest.mark.parametrize("user_summary", [
    "내 헤르메스 프로젝트 전체 좀 보고 보드 또는 카드 후보로 올릴 수 있는 대상 뭔지 확인하고 추천 목록 작성해서 알려줘봐. (실행은 금지)",
    "보드/카드 후보만 추천해줘. 실행하지 말고 목록만.",
    "현재 프로젝트 카드 후보 확인만 해줘. 생성 금지.",
])
def test_heuristic_does_not_propose_for_read_only_candidate_audits(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="추천 목록을 답변했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


def test_review_subagent_current_turn_command_is_not_durable_kanban_followup():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="리뷰까지 서브에이전트 시켜서 진행해봐",
        assistant_summary="리뷰 3개 서브에이전트로 병렬 발사함.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "insufficient_scope"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


@pytest.mark.parametrize("user_summary", [
    "t_5b858cd6 카드 업데이트 승인",
    "진행상태 업데이트",
    "기존 카드에 진행상태 업데이트해",
    "이 카드에 Task 4 완료 기록 남겨",
    "tracking card t_5b858cd6에 코멘트 추가",
])
def test_existing_card_update_intent_is_not_new_card_candidate(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary=(
            "Hermes Agent repo `/Users/honbul/.hermes/hermes-agent`에서 "
            "Kanban conversational intake 중복 노이즈를 고치는 새 세션 프롬프트를 작성했다. "
            "구현/테스트/커밋 후 검증하면 된다."
        ),
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "existing_card_update_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


@pytest.mark.parametrize("user_summary", [
    "t_deadbeef 카드에 repo URL https://github.com/NousResearch/hermes-agent 기록해줘",
    "t_deadbeef 카드에 PR URL https://github.com/NousResearch/hermes-agent/pull/123 남겨",
    "t_deadbeef 카드에 artifact link /tmp/report.pdf 기록 남겨",
    "t_deadbeef 카드에 verification summary: focused tests 5 passed 기록",
    "t_deadbeef 카드에 handoff note: 다음 worker는 prompt_builder.py부터 보면 됨 남겨",
])
def test_existing_card_metadata_updates_are_not_new_card_candidates(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_test",
        user_summary=user_summary,
        assistant_summary="Hermes Agent repo follow-up implementation work exists.",
        default_board="default",
        default_tenant="hermes-agent",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "existing_card_update_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


def test_assistant_summary_card_words_do_not_override_existing_card_update_suppression():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="t_5b858cd6 진행상태 업데이트",
        assistant_summary="이 내용을 새 카드로 만들어도 좋은 후보라고 답했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "existing_card_update_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


@pytest.mark.parametrize(("user_summary", "assistant_summary"), [
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "실제로 필요한 카드는 이미 `suttanipata-ko` 보드에 만들었어: `t_e9f4c088`.",
    ),
    (
        "suttanipata-ko 보드에 숫타니파타 번역 검수 카드 만들어줘",
        "카드 생성 실패했어. 권한 문제를 먼저 해결해야 해.",
    ),
    (
        "create a card on suttanipata-ko for translation review",
        "I could not create the card because the board was not found.",
    ),
])
def test_direct_card_operation_request_is_not_post_turn_proposal(user_summary, assistant_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary=assistant_summary,
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "direct_card_operation_intent"
    assert KeywordHeuristicDetector().detect(request).card_worthy is False


def test_failed_card_creation_with_task_id_is_not_labeled_fulfilled():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="suttanipata-ko 보드에 카드 만들어줘",
        assistant_summary="카드 생성 실패했어. 이전 후보 `t_e9f4c088`가 남아있어.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is False
    assert eligibility.matched_rule == "direct_card_operation_intent"


@pytest.mark.parametrize("user_summary", [
    "gateway status update feature 구현/테스트까지 해줘",
    "lifelog 진행상태 업데이트 자동화 구현/테스트까지 해줘",
])
def test_durable_status_update_work_is_not_suppressed_as_existing_card_update(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    eligibility = card_proposal_eligibility(request)
    assert eligibility.eligible is True
    assert eligibility.matched_rule == "durable_followup"
    assert KeywordHeuristicDetector().detect(request).card_worthy is True


def test_title_generator_rejects_discord_sender_prefixed_raw_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="리뷰까지 서브에이전트 시켜서 진행해봐",
        assistant_summary="리뷰 3개 서브에이전트로 병렬 발사함.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    title = explicit_title_from_request(
        request,
        "[상현] 리뷰까지 서브에이전트 시켜서 진행해봐",
    )

    assert title == "Follow up on requested work"
    assert "[상현]" not in title
    assert "리뷰까지 서브에이전트" not in title


@pytest.mark.parametrize("user_summary", [
    "이 작업은 카드로 남겨줘: Kanban intake threshold hardening writing plan 리뷰 후 구현",
    "JÖKL 마케팅 패킷 생성기 다음 스프린트 작업을 정리하고 테스트/커밋까지 해야 해",
    "lifelog medication reminder cron 누락 원인 분석하고 재발 방지 테스트까지 해줘",
    "게이트웨이재시작했고 칸반보드 title generator를 더 명시적으로 다듬자. 구현/테스트까지 하자",
    "승인: Discord live smoke 테스트 blocked card 1개 생성/검증해",
    "이 내용을 새 카드로 만들어",
    "별도 카드 생성",
    "새 tracking card 후보로 올려: Kanban intake 중복 노이즈 회귀 테스트 추가",
])
def test_heuristic_proposes_for_explicit_card_durable_followup_or_approved_smoke(user_summary):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    decision = KeywordHeuristicDetector().detect(request)
    assert decision.card_worthy is True
    assert decision.proposed_status == "blocked"


def test_title_generator_replaces_generic_boilerplate_with_specific_work_item():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="게이트웨이재시작했고 칸반보드 title generator를 더 명시적으로 다듬자 너무 허접하다 ㅋㅋ",
        assistant_summary="좋다. 구현하자.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Review gateway restart follow-up and define next action")
    assert title == "Improve Kanban intake title generator"
    assert "Review" not in title
    assert "define next action" not in title


def test_title_generator_names_live_smoke_scope():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="승인: 현재 Discord thread에서 live smoke 하고 lifelog-control에 테스트 blocked card 1개 생성/검증해",
        assistant_summary="카드 생성 후 상태를 검증했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert explicit_title_from_request(request) == "Verify Discord live smoke for lifelog-control"


def test_title_generator_names_project_wide_candidate_audit_when_explicit_card_requested():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="이 작업은 카드로 남겨줘: Hermes 프로젝트 전체 보드/카드 후보 추천 목록 정리",
        assistant_summary="후보 목록을 정리했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "[상현] Hermes 프로젝트 전체 보드 또는 카드 후보 추천 목록 작성")
    assert title == "Review Hermes Kanban board/card candidates"
    assert "[상현]" not in title
    assert "추천 목록 작성" not in title


def test_heuristic_keeps_explicit_candidate_audit_card_request_eligible_with_semantic_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="이 작업은 카드로 남겨줘: Hermes 프로젝트 전체 보드/카드 후보 추천 목록 정리",
        assistant_summary="후보 목록을 정리했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    decision = KeywordHeuristicDetector().detect(request)
    assert decision.card_worthy is True
    assert decision.why == "explicit card request"
    assert decision.title == "Review Hermes Kanban board/card candidates"


def test_title_generator_does_not_label_non_hermes_candidate_audit_as_hermes():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="이 작업은 카드로 남겨줘: JÖKL 프로젝트 보드/카드 후보 추천 목록 정리",
        assistant_summary="후보 목록을 정리했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "[상현] JÖKL 프로젝트 보드 또는 카드 후보 추천 목록 작성")
    assert title != "Review Hermes Kanban board/card candidates"
    assert "Hermes" not in title


def test_title_generator_names_hackathon_kanban_backlog_request():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??",
        assistant_summary="칸반은 만드는 게 낫다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert explicit_title_from_request(request) == "Create hackathon Kanban board backlog"


def test_title_generator_rewrites_raw_clunky_proposed_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??",
        assistant_summary="칸반은 만드는 게 낫다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw_title = "[상현] 이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??"
    assert explicit_title_from_request(request, raw_title) == "Create hackathon Kanban board backlog"


def test_title_generator_rejects_verbatim_user_wording_even_when_not_clunky():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="새 tracking card 후보로 올려: Kanban intake 중복 노이즈 회귀 테스트 추가",
        assistant_summary="테스트 추가가 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw_title = "Kanban intake 중복 노이즈 회귀 테스트 추가"
    title = explicit_title_from_request(request, raw_title)
    assert title == "Add Kanban intake regression tests"
    assert title != raw_title
    assert "중복 노이즈 회귀 테스트 추가" not in title


def test_title_generator_rejects_lightly_reformatted_user_wording():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: lifelog medication reminder cron 누락 원인 분석하고 재발 방지 테스트까지 해줘",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw_title = "lifelog medication reminder cron 누락 원인 분석 / 재발 방지 테스트"
    title = explicit_title_from_request(request, raw_title)
    assert title == "Fix Lifelog medication reminder cron regression"
    assert title != raw_title
    assert "Review lifelog follow-up work" not in title
    assert "누락 원인 분석" not in title


@pytest.mark.parametrize(
    ("user_summary", "proposed_title", "expected"),
    [
        ("방금 복약 기록 후속 작업 정리하고 카드로 남겨줘", "Review lifelog follow-up work", "Review medication intake Lifelog capture"),
        ("수면 기록 follow-up 확인하고 카드로 남겨줘", "Review lifelog follow-up work", "Review sleep log Lifelog capture"),
        ("컨디션 기록 후속 검토 카드로 남겨줘", "Review lifelog follow-up work", "Review condition Lifelog capture"),
        ("식단 기록 follow-up 카드로 남겨줘", "Review lifelog follow-up work", "Review diet intake Lifelog capture"),
        ("육아 기록 follow-up 카드로 남겨줘", "Review lifelog follow-up work", "Review childcare Lifelog capture"),
        ("운동 기록 후속 카드로 남겨줘", "Review lifelog follow-up work", "Review training Lifelog capture"),
        ("여행 기록 follow-up 카드로 남겨줘", "Review lifelog follow-up work", "Review travel Lifelog capture"),
        (
            "Review lifelog follow-up work 같은 generic title 문제를 카드로 남겨줘",
            "Review lifelog follow-up work",
            "Fix Kanban candidate title generation for Lifelog records",
        ),
    ],
)
def test_title_generator_rewrites_generic_lifelog_followup_by_record_object(user_summary, proposed_title, expected):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, proposed_title)
    assert title == expected
    assert title not in {"Review lifelog follow-up work", "Plan Kanban follow-up work"}
    assert "[상현]" not in title


def test_title_generator_names_existing_card_review_request():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="기존 카드들은 어떻게하지? 지우면 되나? 카드후보 기준이 뭐야?",
        assistant_summary="기준을 정리하자.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert explicit_title_from_request(request) == "Review existing lifelog-control Kanban cards"


def test_title_generator_names_title_normalization_complaint():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="방금 다른 스레드에서 또 카드후보 타이틀 줬는데 변한게 없어 이거 왜이래?? ㅅㅂ",
        assistant_summary="원인을 확인했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert explicit_title_from_request(request) == "Improve Kanban intake title normalization"


@pytest.mark.parametrize(
    ("user_summary", "expected", "forbidden_fragments"),
    [
        (
            "이거 해커톤 관련해서 칸반보드 만들고 카드 만드는게 낫지 않나??",
            "Create hackathon Kanban board backlog",
            ["이거", "낫지 않나", "??", "[상현]"],
        ),
        (
            "방금 다른 스레드에서 또 카드후보 타이틀 줬는데 변한게 없어 이거 왜이래?? ㅅㅂ",
            "Improve Kanban intake title normalization",
            ["왜이래", "ㅅㅂ", "??", "[상현]"],
        ),
        (
            "기존 카드들은 어떻게하지? 지우면 되나? 카드후보 기준이 뭐야?",
            "Review existing lifelog-control Kanban cards",
            ["어떻게하지", "지우면", "기준이 뭐야", "?"],
        ),
    ],
)
def test_title_generator_removes_clunky_chat_fragments(user_summary, expected, forbidden_fragments):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="확인했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, f"[상현] {user_summary}")
    assert title == expected
    for fragment in forbidden_fragments:
        assert fragment not in title


def test_title_generator_preserves_specific_human_or_auxiliary_title():
    request = req()
    assert explicit_title_from_request(request, "Verify blocked intake cards stay unclaimed") == "Verify blocked intake cards stay unclaimed"


def test_sleep_noon_reminder_context_does_not_map_to_medication_reminder():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="wearable sleep log noon reminder cron 누락 처리 카드로 남겨줘. wearable pause 상황.",
        assistant_summary="sleep log noon reminder workflow follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request)
    assert title == "Review sleep reminder wearable pause workflow"
    assert "medication" not in title.lower()
    assert "sleep" in title.lower()


def test_korean_sleep_cron_missing_context_does_not_map_to_medication():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="수면 로그 noon reminder cron 누락 처리 카드로 남겨줘",
        assistant_summary="수면 리마인더 후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request)
    assert title == "Review sleep reminder wearable pause workflow"
    assert title != "Fix Lifelog medication reminder cron regression"
    assert "medication" not in title.lower()


@pytest.mark.parametrize(("text", "expected_first"), [
    ("wearable sleep log noon reminder cron 누락 wearable pause", "sleep_reminder"),
    ("수면 로그 noon reminder cron 누락", "sleep_reminder"),
    ("복약 리마인더 cron 누락", "medication_reminder"),
    ("medication reminder cron missing regression", "medication_reminder"),
    ("Kanban title generator semantic mismatch", "kanban_title_generation"),
])
def test_request_semantic_objects_rank_strongest_context(text, expected_first):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_semantic",
        user_summary=text,
        assistant_summary="follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert _infer_request_title_objects(request)[0] == expected_first


@pytest.mark.parametrize(("title", "expected"), [
    ("Fix Lifelog medication reminder cron regression", "medication_reminder"),
    ("Review sleep reminder wearable pause workflow", "sleep_reminder"),
    ("Review sleep log reminder workflow", "sleep_reminder"),
])
def test_title_semantic_object(title, expected):
    assert _infer_title_object(title) == expected


def test_semantic_mismatch_proposed_title_attempts_constrained_generator():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="wearable sleep log noon reminder cron 누락 wearable pause 카드로 남겨줘",
        assistant_summary="sleep reminder workflow follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    calls = []

    def generator(generated_request, rule):
        calls.append((generated_request, rule))
        return '{"title":"Review sleep reminder wearable pause workflow","action":"Review","object":"sleep reminder"}'

    title = explicit_title_from_request(
        request,
        "Fix Lifelog medication reminder cron regression",
        title_generator=generator,
    )
    assert calls
    assert title == "Review sleep reminder wearable pause workflow"


def test_semantic_mismatch_unsafe_generator_falls_back_to_sleep_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="wearable sleep log noon reminder cron 누락 wearable pause 카드로 남겨줘",
        assistant_summary="sleep reminder workflow follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    def generator(_request, _rule):
        return '{"title":"Fix Lifelog medication reminder cron regression","action":"Fix","object":"medication reminder"}'

    assert explicit_title_from_request(
        request,
        "Fix Lifelog medication reminder cron regression",
        title_generator=generator,
    ) == "Review sleep reminder wearable pause workflow"


def test_medication_reminder_context_still_maps_to_medication_regression():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_medication",
        user_summary="복약 리마인더 cron 누락 재발 방지 테스트 카드로 남겨줘",
        assistant_summary="medication reminder regression follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert explicit_title_from_request(request) == "Fix Lifelog medication reminder cron regression"


def test_generated_title_accepts_sleep_reminder_safe_object():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="wearable sleep log noon reminder cron 누락 wearable pause 카드로 남겨줘",
        assistant_summary="sleep reminder workflow follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw = '{"title":"Review sleep reminder wearable pause workflow","action":"Review","object":"sleep reminder"}'
    assert generated_title_from_json(request, raw) == "Review sleep reminder wearable pause workflow"


def test_generated_sleep_title_rejects_private_procedure_detail():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_sleep",
        user_summary="wearable sleep log noon reminder cron 누락 wearable pause 카드로 남겨줘",
        assistant_summary="sleep reminder workflow follow-up",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw = '{"title":"Review private procedure detail sleep reminder workflow","action":"Review","object":"sleep reminder"}'
    assert generated_title_from_json(request, raw) == ""


def test_title_validator_accepts_safe_generated_lifelog_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: lifelog medication reminder cron 누락 재발 방지 테스트 1521423652547989615 fever token",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    raw = '{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}'
    assert generated_title_from_json(request, raw) == "Investigate missed medication reminder regression"


@pytest.mark.parametrize("raw", [
    "not json",
    '{"title":"Review lifelog follow-up work","action":"Review","object":"lifelog"}',
    '{"title":"[상현] lifelog medication reminder cron 누락 원인 분석","action":"Review","object":"medication reminder"}',
    '{"title":"Review child fever 39.2 follow-up","action":"Review","object":"condition"}',
    '{"title":"Ship beautiful amazing emotional story","action":"Ship","object":"story"}',
])
def test_title_validator_rejects_unsafe_or_invalid_generated_titles(raw):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: lifelog medication reminder cron 누락 재발 방지 테스트 1521423652547989615 fever token",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert generated_title_from_json(request, raw) == ""


def test_explicit_title_uses_safe_constrained_generator_before_hard_fallback():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: lifelog medication reminder cron 누락 재발 방지 테스트 1521423652547989615 fever token",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    def generator(generated_request, _rule):
        assert "1521423652547989615" not in generated_request.user_summary
        assert "fever" not in generated_request.user_summary.lower()
        assert "token" not in generated_request.user_summary.lower()
        return '{"title":"Investigate missed medication reminder regression","action":"Investigate","object":"medication reminder"}'

    assert explicit_title_from_request(request, "Review lifelog follow-up work", title_generator=generator) == "Investigate missed medication reminder regression"


def test_explicit_title_falls_back_when_constrained_generator_is_unsafe():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: lifelog medication reminder cron 누락 재발 방지 테스트 1521423652547989615 fever token",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )

    def generator(_request, _rule):
        return '{"title":"[상현] lifelog medication reminder cron 누락 원인 분석","action":"Review","object":"medication reminder"}'

    assert explicit_title_from_request(request, "Review lifelog follow-up work", title_generator=generator) == "Fix Lifelog medication reminder cron regression"


def test_strict_json_parser_fail_closed():
    assert parse_detector_json('{"card_worthy": true, "status": "ready", "title": "x"}').card_worthy is False
    assert parse_detector_json('{"card_worthy": true}').card_worthy is False
    good = parse_detector_json('{"card_worthy": true, "title": "Verify safe diagnostic scope", "status": "blocked", "body": {"source_ref": "kp"}}')
    assert good.card_worthy is True


def test_strict_json_parser_defaults_to_blocked_when_status_omitted():
    decision = parse_detector_json('{"card_worthy": true, "title": "Verify safe diagnostic scope", "body": {"source_ref": "kp"}}')
    assert decision.card_worthy is True
    assert decision.proposed_status == "blocked"


def test_redaction_minimizes_sensitive_payload_and_ids():
    redacted = minimize_for_detector("chat 1521423652547989615 아이 fever token secret")
    assert "1521423652547989615" not in redacted
    assert "fever" not in redacted.lower()
    assert "token" not in redacted.lower()


def test_auxiliary_not_called_when_disabled_or_redaction_off():
    called = {"n": 0}
    def call(_):
        called["n"] += 1
        return '{}'
    assert AuxiliaryLLMDetector(KanbanIntakeConfig(auxiliary_detector_enabled=False), call).detect(req()).card_worthy is False
    assert AuxiliaryLLMDetector(KanbanIntakeConfig(auxiliary_detector_enabled=True, redact_before_auxiliary=False), call).detect(req()).card_worthy is False
    assert called["n"] == 0


def test_golden_corpus_schema_is_valid():
    path = Path("tests/fixtures/kanban_intake_golden_cases.jsonl")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert cases
    required_ids = {
        "p0_kanban_intake_not_medication",
        "p0_kanban_false_positive_tests_not_medication",
        "p0_sleep_noon_reminder_not_medication",
        "p0_medication_reminder_still_medication",
        "p1_child_health_privacy_safe_title",
        "p1_jokl_marketing_packet_non_generic_title",
        "p1_security_sensitive_not_child_health",
        "negative_read_only_candidate_audit",
        "negative_existing_card_update",
        "negative_direct_card_operation",
        "negative_ephemeral_subagent_review_command",
    }
    assert {case["id"] for case in cases} >= required_ids
    for case in cases:
        assert isinstance(case["input"]["user_summary"], str)
        assert isinstance(case["input"].get("assistant_summary", ""), str)
        expected = case["expected"]
        assert expected["candidate_class"] in {
            "direct_operation",
            "existing_card_update",
            "proposal_record_request",
            "read_only_audit",
            "ephemeral_workflow_command",
            "durable_followup",
            "unsafe_live_side_effect",
            "insufficient_signal",
        }
        assert isinstance(expected["card_worthy"], bool)
        assert "title_policy" in expected


@pytest.mark.parametrize(
    ("user_summary", "expected_class", "expected_worthy"),
    [
        ("suttanipata-ko 보드에 카드 만들어줘", "direct_operation", False),
        ("t_5b858cd6 카드에 진행상태 업데이트해", "existing_card_update", False),
        ("카드로 남겨줘: Kanban intake false positive regression tests", "proposal_record_request", True),
        ("보드/카드 후보만 추천해줘. 실행하지 말고 목록만.", "read_only_audit", False),
        ("리뷰까지 서브에이전트 시켜서 진행해봐", "insufficient_signal", False),
        ("gateway status update feature 구현/테스트까지 해줘", "durable_followup", True),
        ("승인: Discord live smoke 테스트 blocked card 1개 생성/검증해", "unsafe_live_side_effect", True),
        ("오늘 뭐하지", "insufficient_signal", False),
    ],
)
def test_candidate_classifier_taxonomy(user_summary, expected_class, expected_worthy):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=user_summary,
        assistant_summary="답변했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    decision = KeywordHeuristicDetector().detect(request)
    assert decision.card_worthy is expected_worthy
    assert decision.candidate_class == expected_class
    assert card_proposal_eligibility(request).candidate_class == expected_class


def test_kanban_intake_titles_do_not_collapse_to_medication():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="Hermes gateway kanban intake title generator 품질 평가 리포트 작성하고 개선안 정리해줘",
        assistant_summary="후속 구현/테스트 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Review lifelog follow-up work")
    assert title == "Review Kanban candidate/title quality"
    assert "medication" not in title.lower()


def test_kanban_false_positive_regression_title_not_medication():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: add regression tests for Kanban intake false positives",
        assistant_summary="테스트 추가가 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Review lifelog follow-up work")
    assert title == "Add Kanban intake false-positive regression tests"
    assert "medication" not in title.lower()


def test_project_title_preserves_jokl_marketing_object_without_raw_copy():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: JÖKL 마케팅 패킷 생성기 다음 스프린트 작업을 정리하고 테스트/커밋까지 해야 해",
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Plan Kanban follow-up work")
    assert title == "Plan JÖKL marketing packet generator sprint work"
    assert "Plan Kanban follow-up work" not in title
    assert "마케팅 패킷 생성기 다음 스프린트 작업" not in title


def test_child_health_title_is_privacy_safe_semantic_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="해수 열 기록 후속 작업 카드로 남겨줘",
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Plan Kanban follow-up work")
    assert title == "Review child health Lifelog capture"
    assert "해수" not in title
    assert "Plan Kanban follow-up work" not in title


def test_typed_sensitive_marker_does_not_collapse_security_to_child_health():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary=minimize_for_detector("token rotation follow-up 카드로 남겨줘"),
        assistant_summary="후속 작업이 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert "[security-sensitive]" in request.user_summary
    title = explicit_title_from_request(request, "Plan Kanban follow-up work")
    assert title == "Review security rotation scope"
    assert "child health" not in title.lower()
    assert "token" not in title.lower()


@pytest.mark.parametrize("bad", [
    "Follow up on requested work",
    "Plan Kanban follow-up work",
    "[상현] 카드로 남겨줘: Kanban intake 중복 노이즈 회귀 테스트 추가",
    "Review child fever 39.2 follow-up",
])
def test_title_quality_rubric_rejects_generic_raw_or_sensitive_titles(bad):
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="카드로 남겨줘: Kanban intake 중복 노이즈 회귀 테스트 추가",
        assistant_summary="후속 작업이다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    assert evaluate_title_quality(bad, request).passed is False


def test_validate_proposal_enforces_title_quality_before_storage():
    cfg = KanbanIntakeConfig(enabled=True, default_board="lifelog-control")
    proposal = KanbanCardProposal(
        board="lifelog-control",
        title="Bad title",
        body={"source_ref": "kp_safe", "acceptance_criteria": ["check"]},
        source_ref="kp_safe",
        user_id="u1",
    )
    assert validate_proposal(proposal, cfg) == (
        False,
        "title quality failed: missing_action_verb,missing_object_or_scope",
    )


def test_completion_summary_existing_card_update_is_not_card_worthy():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="오케이 그렇게 해보자 그러면 카드 제목 수정하고 리포트도 업데이트해봐.",
        assistant_summary="완료. 카드 제목 수정: `t_14ed556e`. 리포트 업데이트. health/medication, family/childcare/profile/value/work/travel. 안 한 것: gateway restart 없음.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    eligibility = card_proposal_eligibility(request)
    decision = KeywordHeuristicDetector().detect(request)
    assert eligibility.eligible is False
    assert eligibility.candidate_class == "existing_card_update"
    assert decision.card_worthy is False
    assert decision.candidate_class == "existing_card_update"


def test_explicit_new_proposal_for_personal_context_remains_eligible():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="personal context broker 후속 개선을 새 카드 후보로 남겨줘",
        assistant_summary="후속 설계가 필요하다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    decision = KeywordHeuristicDetector().detect(request)
    assert decision.card_worthy is True
    assert decision.candidate_class == "proposal_record_request"
    assert "personal context" in decision.title.lower() or "self-context" in decision.title.lower()


def test_semantic_mismatch_does_not_recontaminate_with_child_health_title():
    request = IntakeDetectionRequest(
        platform="discord",
        session_key="s1",
        source_ref="kp_safe",
        user_summary="personal context broker 후속 개선을 새 카드 후보로 남겨줘",
        assistant_summary="완료. health/medication은 실패 사례이고 family/childcare/profile/value/work/travel DB 분포를 확인했다.",
        default_board="lifelog-control",
        default_tenant="lifelog",
    )
    title = explicit_title_from_request(request, "Review child health Lifelog capture")
    assert "child health" not in title.lower()
    assert "personal context" in title.lower() or "self-context" in title.lower()


@pytest.mark.parametrize("good", [
    "Fix Kanban intake false-positive suppression",
    "Add Kanban intake regression tests",
    "Review JÖKL marketing packet generator sprint scope",
    "Review child health Lifelog capture",
])
def test_title_quality_rubric_accepts_action_object_scope_titles(good):
    assert evaluate_title_quality(good).passed is True


def test_eval_script_reports_quality_metrics():
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/eval_kanban_intake_quality.py",
            "--corpus",
            "tests/fixtures/kanban_intake_golden_cases.jsonl",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    data = json.loads(proc.stdout)
    assert data["candidate_precision"] >= 0.90
    assert data["candidate_recall"] >= 0.70
    assert data["unsafe_false_positive"] == 0
    assert data["generic_title_rate"] <= 0.05
    assert data["raw_copy_rate"] == 0
    assert data["sensitive_title_leak"] == 0
    assert data["thresholds_passed"] is True
