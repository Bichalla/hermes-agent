from gateway.kanban_intake import (
    AuxiliaryLLMDetector,
    KanbanIntakeConfig,
    KeywordHeuristicDetector,
    IntakeDetectionRequest,
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
