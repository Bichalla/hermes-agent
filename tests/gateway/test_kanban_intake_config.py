from hermes_cli.config import DEFAULT_CONFIG
from gateway.kanban_intake import parse_config


def test_default_config_is_disabled_and_safe():
    cfg = parse_config(DEFAULT_CONFIG)
    assert cfg.enabled is False
    assert cfg.default_status == "blocked"
    assert cfg.detector == "heuristic"
    assert cfg.auxiliary_detector_enabled is False
    assert cfg.redact_before_auxiliary is True
    assert cfg.title_generator_enabled is False
    assert cfg.title_generator_mode == "fallback_only"
    assert cfg.title_generator_timeout_seconds <= 5
    assert cfg.pending_retention_seconds > 0


def test_missing_block_returns_disabled_config():
    cfg = parse_config({"kanban": {}})
    assert cfg.enabled is False
    assert cfg.default_board == ""


def test_invalid_status_fails_closed_to_blocked():
    cfg = parse_config({"kanban": {"conversational_intake": {"default_status": "ready"}}})
    assert cfg.default_status == "blocked"


def test_title_generation_config_invalid_mode_fails_closed():
    cfg = parse_config({"kanban": {"conversational_intake": {"title_generator_enabled": True, "title_generator_mode": "freeform"}}})
    assert cfg.title_generator_enabled is False
    assert cfg.title_generator_mode == "fallback_only"


def test_title_generation_config_accepts_constrained_llm_mode():
    cfg = parse_config({"kanban": {"conversational_intake": {"title_generator_enabled": True, "title_generator_mode": "constrained_llm", "title_generator_timeout_seconds": 99}}})
    assert cfg.title_generator_enabled is True
    assert cfg.title_generator_mode == "constrained_llm"
    assert cfg.title_generator_timeout_seconds <= 5


def test_retention_is_bounded_not_infinite():
    cfg = parse_config({"kanban": {"conversational_intake": {"pending_retention_seconds": 10**12}}})
    assert cfg.pending_retention_seconds <= 30 * 86400
