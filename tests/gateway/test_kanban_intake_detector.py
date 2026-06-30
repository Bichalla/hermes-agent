import pytest

from gateway.kanban_intake import (
    AuxiliaryLLMDetector,
    KanbanIntakeConfig,
    KeywordHeuristicDetector,
    IntakeDetectionRequest,
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
