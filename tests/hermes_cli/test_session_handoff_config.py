from __future__ import annotations


def test_handoff_config_disabled_by_default():
    from hermes_cli.session_handoff import resolve_handoff_config

    cfg = resolve_handoff_config({})

    assert cfg.enabled is False
    assert cfg.surface == "path_only"
    assert cfg.max_messages == 80
    assert cfg.max_chars == 30000
    assert cfg.include_tool_results is False


def test_handoff_config_reads_on_reset_and_clamps_limits():
    from hermes_cli.session_handoff import resolve_handoff_config

    cfg = resolve_handoff_config(
        {
            "session_handoff": {
                "on_reset": {
                    "enabled": True,
                    "surface": "body",
                    "max_messages": 9999,
                    "max_chars": 9999999,
                    "include_tool_results": True,
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.surface == "path_only"
    assert cfg.max_messages <= 200
    assert cfg.max_chars <= 100000
    assert cfg.include_tool_results is True


def test_handoff_config_expands_profile_artifact_dir(tmp_path, monkeypatch):
    from hermes_cli.session_handoff import resolve_handoff_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = resolve_handoff_config(
        {"session_handoff": {"on_reset": {"artifact_dir": "{hermes_home}/handoffs/{profile}"}}},
        profile="worker",
    )

    assert cfg.artifact_dir == tmp_path / ".hermes" / "handoffs" / "worker"


def test_handoff_config_string_booleans_do_not_enable_privacy_sensitive_options():
    from hermes_cli.session_handoff import resolve_handoff_config

    false_values = ["false", "False", "0", "no", "off", "unexpected", 0, 2]
    for value in false_values:
        cfg = resolve_handoff_config(
            {
                "session_handoff": {
                    "on_reset": {
                        "enabled": value,
                        "include_tool_results": value,
                    }
                }
            }
        )

        assert cfg.enabled is False
        assert cfg.include_tool_results is False

    true_cfg = resolve_handoff_config(
        {"session_handoff": {"on_reset": {"enabled": "true", "include_tool_results": 1}}}
    )
    assert true_cfg.enabled is True
    assert true_cfg.include_tool_results is True


def test_handoff_config_uses_context_local_hermes_home(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.session_handoff import resolve_handoff_config

    home = tmp_path / "profile-home"
    token = set_hermes_home_override(home)
    try:
        cfg = resolve_handoff_config({"session_handoff": {"on_reset": {}}})
    finally:
        reset_hermes_home_override(token)

    assert cfg.artifact_dir == home / "handoffs" / "custom"


def test_handoff_config_infers_profile_from_explicit_named_profile_home(tmp_path):
    from hermes_cli.session_handoff import resolve_handoff_config

    home = tmp_path / ".hermes" / "profiles" / "worker"
    cfg = resolve_handoff_config({"session_handoff": {"on_reset": {}}}, hermes_home=home)

    assert cfg.artifact_dir == home / "handoffs" / "worker"


def test_default_config_contains_disabled_session_handoff_section():
    from hermes_cli.config import DEFAULT_CONFIG

    on_reset = DEFAULT_CONFIG["session_handoff"]["on_reset"]
    assert on_reset["enabled"] is False
    assert on_reset["surface"] == "path_only"
