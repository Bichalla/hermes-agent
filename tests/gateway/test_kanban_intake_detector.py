import pytest

from gateway.kanban_intake import (
    AuxiliaryLLMDetector,
    KanbanIntakeConfig,
    KeywordHeuristicDetector,
    IntakeDetectionRequest,
    card_proposal_eligibility,
    explicit_title_from_request,
    minimize_for_detector,
    parse_detector_json,
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
    assert title == "Plan Kanban follow-up work"
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
    assert title == "Review lifelog follow-up work"
    assert title != raw_title
    assert "누락 원인 분석" not in title


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


def test_strict_json_parser_fail_closed():
    assert parse_detector_json('{"card_worthy": true, "status": "ready", "title": "x"}').card_worthy is False
    assert parse_detector_json('{"card_worthy": true}').card_worthy is False
    good = parse_detector_json('{"card_worthy": true, "title": "Safe follow-up", "status": "blocked", "body": {"source_ref": "kp"}}')
    assert good.card_worthy is True


def test_strict_json_parser_defaults_to_blocked_when_status_omitted():
    decision = parse_detector_json('{"card_worthy": true, "title": "Safe follow-up", "body": {"source_ref": "kp"}}')
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
