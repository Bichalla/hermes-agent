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
