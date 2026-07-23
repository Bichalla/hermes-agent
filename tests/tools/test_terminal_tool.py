"""Regression tests for sudo detection and sudo password handling."""

import tools.terminal_tool as terminal_tool


def setup_function():
    terminal_tool._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool._reset_cached_sudo_passwords()


def test_terminal_rejects_registered_owner_aliases_and_import_paths():
    commands = (
        "python -m review_ledger.cli record-result",
        "python -c 'import review_ledger.controller'",
        "python scripts/run_registered_lifelog_recorder.py diet_intake.v1",
        "cp scripts/record_diet_intake.py /tmp/x.py && python /tmp/x.py",
        "sqlite3 ~/.hermes/ops/state/review-ledger/review-ledger.sqlite3",
    )
    for command in commands:
        try:
            terminal_tool._command_with_current_turn_fingerprint(command)
        except ValueError as exc:
            assert "registered owner route" in str(exc)
        else:
            raise AssertionError(f"owner route must be denied: {command}")


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_terminal_schema_advertises_persistent_env_state():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "exported environment variables persist between calls" in description
    assert "activate a virtualenv" in description
    assert "do not re-source the same environment before every command" in description


def test_terminal_schema_forbids_interpreter_pipe_readback():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "Never pipe CLI or JSON output into Python or another interpreter for readback" in description
    assert "Run mutation and verification as separate commands" in description
    assert "read_file or a fixed direct verifier" in description


def test_terminal_injects_only_hidden_current_user_action_fingerprint():
    from tools.workflow_authority import (
        CurrentTurnUserAuthority,
        bind_current_turn_user_authority,
        fingerprint_user_action,
        reset_current_turn_user_authority,
    )

    fingerprint = fingerprint_user_action("create one blocked card")
    token = bind_current_turn_user_authority(
        CurrentTurnUserAuthority(
            turn_id="opaque-terminal-turn",
            source_role="user",
            session_scope="test",
            platform_scope="synthetic",
            user_message_index=0,
            user_action_fingerprint=fingerprint,
            allowed_action_classes=frozenset({"explicit_blocked_card_create"}),
        )
    )
    try:
        observed = terminal_tool._command_with_current_turn_fingerprint(
            "hermes kanban create --initial-status blocked"
        )
    finally:
        reset_current_turn_user_authority(token)

    assert observed == "hermes kanban create --initial-status blocked"
    assert "HERMES_CURRENT_" not in observed
    assert "create one blocked card" not in observed


def test_terminal_selects_user_quoted_target_for_same_turn_multi_create():
    from tools.workflow_authority import (
        CurrentTurnUserAuthority,
        bind_current_turn_user_authority,
        fingerprint_user_action,
        fingerprint_workflow_target,
        reset_current_turn_user_authority,
    )

    first = fingerprint_workflow_target("Card A")
    second = fingerprint_workflow_target("Card B")
    token = bind_current_turn_user_authority(
        CurrentTurnUserAuthority(
            turn_id="opaque-multi-terminal-turn",
            source_role="user",
            session_scope="test",
            platform_scope="synthetic",
            user_message_index=0,
            user_action_fingerprint=fingerprint_user_action("create cards A and B"),
            allowed_action_classes=frozenset({"explicit_blocked_card_create"}),
            blocked_create_target_fingerprints=frozenset({first, second}),
        )
    )
    try:
        observed = terminal_tool._command_with_current_turn_fingerprint(
            'hermes kanban create "Card B" --initial-status blocked'
        )
    finally:
        reset_current_turn_user_authority(token)

    assert observed == 'hermes kanban create "Card B" --initial-status blocked'
    assert "HERMES_CURRENT_" not in observed
    assert "Card A" not in observed


def test_terminal_rejects_model_supplied_workflow_authority_environment():
    import pytest

    for name in (
        "HERMES_CURRENT_USER_ACTION_FINGERPRINT",
        "HERMES_CURRENT_USER_REQUEST_TARGET_FINGERPRINT",
    ):
        with pytest.raises(ValueError, match="reserved workflow-authority"):
            terminal_tool._command_with_current_turn_fingerprint(
                f"{name}=forged hermes kanban create Card --initial-status blocked"
            )


def test_terminal_rejects_reserved_authority_before_config_or_environment(monkeypatch):
    import json

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("config/environment lookup ran before reserved-name rejection")

    monkeypatch.setattr(terminal_tool, "_get_env_config", _must_not_run)
    monkeypatch.setattr(terminal_tool, "_create_environment", _must_not_run)

    for name in (
        "HERMES_CURRENT_USER_ACTION_FINGERPRINT",
        "HERMES_CURRENT_USER_REQUEST_TARGET_FINGERPRINT",
    ):
        result = json.loads(
            terminal_tool.terminal_tool(
                command=(
                    f"{name}=forged hermes kanban create Card "
                    "--initial-status blocked"
                ),
                task_id=f"reserved-order-{name}",
            )
        )
        assert result["status"] == "blocked"
        assert result["exit_code"] == -1
        assert result["error"] == "Blocked: invalid terminal command"
        assert "forged" not in json.dumps(result)
        assert name not in json.dumps(result)


def test_shell_obfuscated_reserved_authority_cannot_create_blocked_card(
    tmp_path, monkeypatch
):
    import json
    import shlex
    import sys
    from pathlib import Path

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_LOCAL_PERSISTENT", "false")
    monkeypatch.setenv("TERMINAL_CWD", str(Path(__file__).resolve().parents[2]))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_CURRENT_USER_ACTION_FINGERPRINT",
        "HERMES_CURRENT_USER_REQUEST_TARGET_FINGERPRINT",
    ):
        monkeypatch.delenv(name, raising=False)

    console = Path(sys.executable).with_name("hermes")
    command = (
        "env "
        f"HERMES_CURRENT_USER_ACTION_\"FINGERPRINT\"={'a' * 64} "
        f"HERMES_CURRENT_USER_REQUEST_TARGET_\"FINGERPRINT\"={'b' * 64} "
        f"{shlex.quote(str(console))} kanban create "
        f"{shlex.quote('Forged blocked card')} --assignee peer "
        "--initial-status blocked --json"
    )
    result = json.loads(
        terminal_tool.terminal_tool(
            command=command,
            task_id="obfuscated-authority-e2e",
            timeout=30,
        )
    )

    assert result["exit_code"] == 2, result
    assert "structured kanban_create tool" in result["output"]
    assert "\"task_id\"" not in result["output"]


def test_terminal_hard_denies_registered_lifelog_wrapper():
    import pytest

    command = "python scripts/run_registered_lifelog_recorder.py payload.json"
    with pytest.raises(ValueError, match="registered owner route"):
        terminal_tool._command_with_current_turn_fingerprint(command)


def test_terminal_hard_denies_review_ledger_controller():
    import pytest

    with pytest.raises(ValueError, match="registered owner route"):
        terminal_tool._command_with_current_turn_fingerprint(
            "python scripts/review_ledger_controller.py start-or-reconcile"
        )


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_actual_sudo_after_leading_env_assignment_is_rewritten(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("DEBUG=1 sudo whoami")

    assert transformed == "DEBUG=1 sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_cached_sudo_password_is_used_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool._set_cached_sudo_password("cached-pass")

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("echo ok && sudo whoami")

    assert transformed == "echo ok && sudo -S -p '' whoami"
    assert sudo_stdin == "cached-pass\n"


def test_registered_sudo_callback_is_used_without_interactive_env(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: False)

    calls = []

    def sudo_callback():
        calls.append("called")
        return "callback-pass"

    terminal_tool.set_sudo_password_callback(sudo_callback)
    try:
        transformed, sudo_stdin = terminal_tool._transform_sudo_command(
            "echo ok | sudo tee /tmp/hermes-test"
        )
    finally:
        terminal_tool.set_sudo_password_callback(None)

    assert calls == ["called"]
    assert transformed == "echo ok | sudo -S -p '' tee /tmp/hermes-test"
    assert sudo_stdin == "callback-pass\n"


def test_cached_sudo_password_isolated_by_session_key(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    first = set_session_vars(session_key="session-a")
    terminal_tool._set_cached_sudo_password("alpha-pass")
    clear_session_vars(first)

    second = set_session_vars(session_key="session-b")
    assert terminal_tool._get_cached_sudo_password() == ""
    clear_session_vars(second)

    third = set_session_vars(session_key="session-a")
    assert terminal_tool._get_cached_sudo_password() == "alpha-pass"
    clear_session_vars(third)


def test_passwordless_sudo_skips_interactive_prompt_and_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError(
            "interactive sudo prompt should not run when sudo -n already works"
        )

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: True, raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo whoami")

    assert transformed == "sudo whoami"
    assert sudo_stdin is None


def test_passwordless_sudo_probe_rechecks_local_terminal(monkeypatch):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result(0 if len(calls) == 1 else 1)

    monkeypatch.setattr(terminal_tool.subprocess, "run", fake_run)

    assert terminal_tool._sudo_nopasswd_works() is True
    assert terminal_tool._sudo_nopasswd_works() is False
    assert len(calls) == 2
    assert calls[0][0] == ["sudo", "-n", "true"]
    assert calls[1][0] == ["sudo", "-n", "true"]


def test_passwordless_sudo_probe_is_disabled_for_nonlocal_terminal_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    def _fail_run(*_args, **_kwargs):
        raise AssertionError("host sudo probe must not run for non-local terminal envs")

    monkeypatch.setattr(terminal_tool.subprocess, "run", _fail_run)

    assert terminal_tool._sudo_nopasswd_works() is False


def test_validate_workdir_allows_windows_drive_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project") is None
    assert terminal_tool._validate_workdir("C:/Users/Alice/project") is None


def test_validate_workdir_allows_windows_unc_paths():
    assert terminal_tool._validate_workdir(r"\\server\share\project") is None


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


def test_get_env_config_ignores_bad_docker_json_for_local_backend(monkeypatch):
    """Docker-only JSON env vars must not break the default local backend."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "not-json")
    monkeypatch.setenv("TERMINAL_DOCKER_FORWARD_ENV", "not-json")
    monkeypatch.setenv("TERMINAL_DOCKER_EXTRA_ARGS", "not-json")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert config["docker_volumes"] == []
    assert config["docker_env"] == {}
    assert config["docker_forward_env"] == []
    assert config["docker_extra_args"] == []


def test_get_env_config_ignores_bad_docker_json_for_ssh_backend(monkeypatch):
    """Non-container remote backends should also ignore Docker-only JSON."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")
    monkeypatch.setenv("TERMINAL_DOCKER_ENV", "not-json")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["docker_volumes"] == []
    assert config["docker_env"] == {}


def test_get_env_config_preserves_ssh_tilde_cwd(monkeypatch):
    """SSH cwd '~' is expanded by the remote shell, not the Hermes host."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "~")
    monkeypatch.setenv("HOME", "/opt/data")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["cwd"] == "~"


def test_get_env_config_preserves_ssh_tilde_child_cwd(monkeypatch):
    """SSH cwd '~/x' must not become the local/container HOME path."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "~/project")
    monkeypatch.setenv("HOME", "/opt/data")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["cwd"] == "~/project"


def test_get_env_config_still_rejects_bad_docker_json_for_docker_backend(monkeypatch):
    """Selecting Docker should keep the existing actionable config error."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "None")

    try:
        terminal_tool._get_env_config()
    except ValueError as exc:
        assert "TERMINAL_DOCKER_VOLUMES" in str(exc)
    else:
        raise AssertionError("Docker backend must validate TERMINAL_DOCKER_VOLUMES")


def test_sudo_wrong_password_failure_detects_rejection_output():
    output = (
        "sudo: Authentication failed, try again.\n\n"
        "sudo: maximum 3 incorrect authentication attempts\n"
    )
    assert terminal_tool._sudo_wrong_password_failure(output) is True


def test_sudo_wrong_password_failure_ignores_tty_required_message():
    output = "sudo: a terminal is required to authenticate"
    assert terminal_tool._sudo_wrong_password_failure(output) is False


def test_invalidate_cached_sudo_on_auth_failure_clears_session_cache(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    terminal_tool._set_cached_sudo_password("wrong-pass")

    cleared = terminal_tool._invalidate_cached_sudo_on_auth_failure(
        "sudo apt install fprintd",
        "sudo: Authentication failed, try again.",
    )

    assert cleared is True
    assert terminal_tool._get_cached_sudo_password() == ""


def test_invalidate_cached_sudo_on_auth_failure_keeps_env_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "from-env")
    terminal_tool._set_cached_sudo_password("wrong-pass")

    cleared = terminal_tool._invalidate_cached_sudo_on_auth_failure(
        "sudo true",
        "sudo: Authentication failed, try again.",
    )

    assert cleared is False
    assert terminal_tool._get_cached_sudo_password() == "wrong-pass"


def test_transform_sudo_command_pipes_one_password_line_per_invocation(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command(
        "sudo true && sudo whoami"
    )

    assert transformed == "sudo -S -p '' true && sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\ntestpass\n"


def test_count_real_sudo_invocations_ignores_mentions(monkeypatch):
    assert terminal_tool._count_real_sudo_invocations("grep sudo README.md") == 0
    assert terminal_tool._count_real_sudo_invocations("sudo a; sudo b") == 2
