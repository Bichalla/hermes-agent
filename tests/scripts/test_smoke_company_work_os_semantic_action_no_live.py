from __future__ import annotations

from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_company_work_os_semantic_action_no_live.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import smoke_company_work_os_semantic_action_no_live as smoke


CASE_IDS = [
    "initial_seed_positive_direct",
    "initial_seed_negation",
    "initial_seed_question",
    "initial_seed_plan_explanation",
    "initial_seed_quoted_reported",
]
INITIAL_SEED_ACTION = "company_work_os_initial_seed_record"


def _case(
    case_id: str,
    count: int,
    action: str | None,
    error_code: str | None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "selected_action_count": count,
        "selected_action": action,
        "error_code": error_code,
    }


EXPECTED_SUCCESS = {
    "schema": "semantic-action-capture-smoke/v1",
    "mode": "fake",
    "provider": "fake",
    "api_mode": "codex_responses",
    "pass": True,
    "cases": [
        _case("initial_seed_positive_direct", 1, INITIAL_SEED_ACTION, None),
        _case("initial_seed_negation", 0, None, None),
        _case("initial_seed_question", 0, None, None),
        _case("initial_seed_plan_explanation", 0, None, None),
        _case("initial_seed_quoted_reported", 0, None, None),
    ],
    "error_code": None,
}

EXPECTED_INVALID = {
    "schema": "semantic-action-capture-smoke/v1",
    "mode": "invalid",
    "provider": None,
    "api_mode": None,
    "pass": False,
    "cases": [_case(case_id, 0, None, "not_run") for case_id in CASE_IDS],
    "error_code": "invalid_cli",
}

EXPECTED_INTERNAL_FAILURE = {
    "schema": "semantic-action-capture-smoke/v1",
    "mode": "fake",
    "provider": "fake",
    "api_mode": "codex_responses",
    "pass": False,
    "cases": [_case(case_id, 0, None, "not_run") for case_id in CASE_IDS],
    "error_code": "internal_failure",
}

EXPECTED_ACTUAL_SUCCESS = {
    **EXPECTED_SUCCESS,
    "mode": "actual",
    "provider": "openai-codex",
}

EXPECTED_PROVIDER_FAILURE = {
    "schema": "semantic-action-capture-smoke/v1",
    "mode": "actual",
    "provider": "openai-codex",
    "api_mode": "codex_responses",
    "pass": False,
    "cases": [_case(case_id, 0, None, "not_run") for case_id in CASE_IDS],
    "error_code": "provider_failure",
}


def _fake_actual_runtime():
    return smoke._ActualRuntime(
        model="offline-model",
        provider="openai-codex",
        api_mode="codex_responses",
        api_key="offline-token",
        base_url="https://offline.invalid/codex",
        credential_pool=None,
    )


def _run_cli(*args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _assert_case_failure(report: dict[str, object]) -> None:
    assert report["pass"] is False
    assert report["error_code"] == "case_failure"
    assert any(case["error_code"] == "selection_mismatch" for case in report["cases"])
    assert all(
        case["error_code"] in (None, "selection_mismatch")
        for case in report["cases"]
    )


@pytest.mark.parametrize(
    ("config_model", "expected_model"),
    (
        ("gpt-offline-string", "gpt-offline-string"),
        ({"default": "gpt-offline-default"}, "gpt-offline-default"),
        ({"model": "gpt-offline-model"}, "gpt-offline-model"),
    ),
)
def test_actual_runtime_resolver_uses_fixed_provider_and_configured_model_only(
    monkeypatch, config_model, expected_model
):
    import hermes_cli.config as hermes_config
    import hermes_cli.runtime_provider as runtime_provider

    calls = []

    def offline_resolver(*, requested, target_model):
        calls.append((requested, target_model))
        return {
            "provider": " openai-codex ",
            "api_mode": "codex_responses",
            "api_key": "offline-token",
            "base_url": "https://offline.invalid/codex",
            "credential_pool": "offline-pool",
        }

    monkeypatch.setattr(hermes_config, "load_config", lambda: {"model": config_model})
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", offline_resolver)

    resolved = smoke._resolve_actual_runtime()

    assert resolved == smoke._ActualRuntime(
        model=expected_model,
        provider="openai-codex",
        api_mode="codex_responses",
        api_key="offline-token",
        base_url="https://offline.invalid/codex",
        credential_pool="offline-pool",
    )
    assert calls == [("openai-codex", expected_model)]


@pytest.mark.parametrize(
    "config_model,runtime_patch",
    (
        (None, {}),
        ("offline-model", {"provider": "custom"}),
        ("offline-model", {"api_mode": "chat_completions"}),
        ("offline-model", {"api_key": ""}),
        ("offline-model", {"base_url": None}),
        ("offline-model", {"base_url": ""}),
        ("offline-model", {"base_url": "not-a-url"}),
        ("offline-model", {"base_url": 7}),
    ),
)
def test_actual_runtime_resolver_rejects_invalid_runtime_with_constant_failure(
    monkeypatch, config_model, runtime_patch
):
    import hermes_cli.config as hermes_config
    import hermes_cli.runtime_provider as runtime_provider

    runtime = {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "api_key": "offline-token",
        "base_url": "https://offline.invalid/codex",
    }
    runtime.update(runtime_patch)
    monkeypatch.setattr(hermes_config, "load_config", lambda: {"model": config_model})
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **_kwargs: runtime)

    with pytest.raises(smoke._ActualProviderFailure) as raised:
        smoke._resolve_actual_runtime()

    assert str(raised.value) == ""


def test_actual_agent_builder_wires_runtime_without_replacing_production_client(monkeypatch):
    import run_agent

    real_agent_class = run_agent.AIAgent
    real_client_factory = real_agent_class._create_openai_client
    constructor_calls = []

    def offline_constructor(**kwargs):
        assert real_agent_class._create_openai_client is real_client_factory
        constructor_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(run_agent, "AIAgent", offline_constructor)

    runtime = _fake_actual_runtime()
    agent = smoke._build_actual_agent("initial_seed_positive_direct", runtime)

    assert constructor_calls == [
        {
            "model": runtime.model,
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": runtime.base_url,
            "api_key": runtime.api_key,
            "credential_pool": None,
            "max_iterations": 1,
            "tool_delay": 0,
            "quiet_mode": True,
            "skip_context_files": True,
            "skip_memory": True,
            "save_trajectories": False,
            "platform": "semantic-action-capture-smoke",
        }
    ]
    assert agent.tools == smoke.TOOL_DEFINITIONS
    assert agent.tools is not smoke.TOOL_DEFINITIONS
    assert agent.valid_tool_names == {smoke.TOOL_NAME}
    assert agent._persist_disabled is True
    assert agent._session_db is None
    assert agent.save_trajectories is False
    assert getattr(agent, "_cached_system_prompt", None) == (
        "Call the supplied tool exactly once only for a direct current-user instruction "
        "to record the company Work OS initial seed. Do not call it for negation, "
        "questions, plans or explanations, quoted text, or reported speech. "
        "Otherwise answer normally without tools."
    )


def test_actual_smoke_with_injected_offline_runtime_uses_five_fresh_agents():
    agents = []

    def offline_builder(case, runtime):
        assert runtime == _fake_actual_runtime()
        agent = smoke._build_agent(case)
        agents.append(agent)
        return agent

    report, audit = smoke._run_actual_smoke_with_audit(
        runtime_resolver=_fake_actual_runtime,
        agent_builder=offline_builder,
    )

    assert report == EXPECTED_ACTUAL_SUCCESS
    assert [case["selected_action_count"] for case in report["cases"]] == [1, 0, 0, 0, 0]
    assert len(agents) == 5
    assert len({id(agent) for agent in agents}) == 5
    assert audit.agents == agents
    assert audit.owner_dispatch_calls == 0
    assert audit.registry_dispatch_calls == 0
    assert audit.request_dump_calls == 0


def test_actual_resolver_sensitive_failure_is_closed_and_builder_is_not_called(capsys):
    sensitive = "sensitive-runtime-token-7742"
    builder_calls = 0

    def failed_resolver():
        raise RuntimeError(sensitive)

    def forbidden_builder(_case, _runtime):
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("builder reached")

    report = smoke.run_actual_smoke(
        runtime_resolver=failed_resolver,
        agent_builder=forbidden_builder,
    )
    captured = capsys.readouterr()

    assert report == EXPECTED_PROVIDER_FAILURE
    assert builder_calls == 0
    assert sensitive not in json.dumps(report)
    assert captured.out == ""
    assert captured.err == ""


def test_actual_model_call_failure_is_closed_without_sensitive_output(capsys):
    sensitive = "sensitive-provider-body-1948"
    agents = []

    class FailedOfflineAgent:
        def run_conversation(self, _prompt):
            raise RuntimeError(sensitive)

    def failed_builder(_case, runtime):
        assert runtime == _fake_actual_runtime()
        agent = FailedOfflineAgent()
        agents.append(agent)
        return agent

    report = smoke.run_actual_smoke(
        runtime_resolver=_fake_actual_runtime,
        agent_builder=failed_builder,
    )
    captured = capsys.readouterr()

    _assert_case_failure(report)
    assert report["mode"] == "actual"
    assert report["provider"] == "openai-codex"
    assert len(agents) == 5
    assert all(case["error_code"] == "selection_mismatch" for case in report["cases"])
    assert sensitive not in json.dumps(report)
    assert captured.out == ""
    assert captured.err == ""


def test_actual_collector_prevents_owner_and_registry_dispatch_with_offline_agents():
    import run_agent
    import tools.registry as tool_registry

    inspected = 0

    def inspecting_builder(case, _runtime):
        nonlocal inspected
        inspected += 1
        assert run_agent.handle_function_call is smoke._production_dispatch_sentinel
        assert tool_registry.ToolRegistry.dispatch is smoke._registry_dispatch_sentinel
        return smoke._build_agent(case)

    report, audit = smoke._run_actual_smoke_with_audit(
        runtime_resolver=_fake_actual_runtime,
        agent_builder=inspecting_builder,
    )

    assert report == EXPECTED_ACTUAL_SUCCESS
    assert inspected == 5
    assert audit.owner_dispatch_calls == 0
    assert audit.registry_dispatch_calls == 0


def test_actual_malformed_selection_stops_at_collector_before_any_dispatch():
    sensitive_wrong_action = "sensitive-unregistered-action-5831"

    class MalformedOfflineAgent:
        def run_conversation(self, _prompt):
            assistant_message = SimpleNamespace(
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name=smoke.TOOL_NAME,
                            arguments=json.dumps({"action": sensitive_wrong_action}),
                        )
                    )
                ]
            )
            self._execute_tool_calls(assistant_message, [], "offline-task")
            raise AssertionError("collector did not stop")

    report, audit = smoke._run_actual_smoke_with_audit(
        runtime_resolver=_fake_actual_runtime,
        agent_builder=lambda _case, _runtime: MalformedOfflineAgent(),
    )

    _assert_case_failure(report)
    assert all(case["error_code"] == "selection_mismatch" for case in report["cases"])
    assert sensitive_wrong_action not in json.dumps(report)
    assert audit.owner_dispatch_calls == 0
    assert audit.registry_dispatch_calls == 0


@pytest.mark.parametrize("fault", ("duplicate", "repairable_name"))
def test_actual_raw_selection_fails_before_production_repair_or_dedup(
    monkeypatch, fault
) -> None:
    real_events = smoke._provider_events_for_prompt

    def faulty_events(prompt):
        events = real_events(prompt)
        if smoke._case_id_for_prompt(prompt) != "initial_seed_positive_direct":
            return events
        for index, event in enumerate(events):
            item = getattr(event, "item", None)
            if item is None or getattr(item, "type", None) != "function_call":
                continue
            if fault == "repairable_name":
                item.name = "registered_local_workflaw"
            else:
                duplicate = deepcopy(event)
                duplicate.item.id = "fc_duplicate_actual"
                duplicate.item.call_id = "call_duplicate_actual"
                events.insert(index + 1, duplicate)
            break
        return events

    monkeypatch.setattr(smoke, "_provider_events_for_prompt", faulty_events)

    report, audit = smoke._run_actual_smoke_with_audit(
        _fake_actual_runtime,
        lambda case, _runtime: smoke._build_agent(case),
    )

    _assert_case_failure(report)
    assert report["cases"][0]["error_code"] == "selection_mismatch"
    assert audit.actual_provider_requests["initial_seed_positive_direct"] == 1
    assert audit.owner_dispatch_calls == 0
    assert audit.registry_dispatch_calls == 0


@pytest.mark.parametrize("failure", (False, True))
def test_actual_smoke_restores_auxiliary_runtime_globals_exactly(failure) -> None:
    import agent.auxiliary_client as auxiliary_client

    names = (
        "_RUNTIME_MAIN_PROVIDER",
        "_RUNTIME_MAIN_MODEL",
        "_RUNTIME_MAIN_BASE_URL",
        "_RUNTIME_MAIN_API_KEY",
        "_RUNTIME_MAIN_API_MODE",
    )
    seeded = tuple(f"seed-{index}" for index in range(len(names)))
    previous = tuple(getattr(auxiliary_client, name) for name in names)
    for name, value in zip(names, seeded):
        setattr(auxiliary_client, name, value)

    try:
        def builder(case, _runtime):
            agent = smoke._build_agent(case)
            if failure:
                def fail(_prompt):
                    raise RuntimeError("offline-failure")

                setattr(agent, "run_conversation", fail)
            return agent

        smoke._run_actual_smoke_with_audit(_fake_actual_runtime, builder)
        assert tuple(getattr(auxiliary_client, name) for name in names) == seeded
    finally:
        for name, value in zip(names, previous):
            setattr(auxiliary_client, name, value)


def test_actual_builder_discards_pool_and_seals_post_resolution_recovery(monkeypatch):
    import hermes_cli.auth as hermes_auth
    import run_agent

    real_agent_class = run_agent.AIAgent
    persist_calls = 0

    class Pool:
        def _persist(self):
            nonlocal persist_calls
            persist_calls += 1
            raise AssertionError("credential pool persistence reached")

    runtime = smoke._ActualRuntime(
        model="offline-model",
        provider="openai-codex",
        api_mode="codex_responses",
        api_key="offline-token",
        base_url="https://offline.invalid/codex",
        credential_pool=Pool(),
    )
    constructor_calls = []

    def offline_constructor(**kwargs):
        constructor_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(run_agent, "AIAgent", offline_constructor)
    built = smoke._build_actual_agent("initial_seed_positive_direct", runtime)

    assert constructor_calls[0]["credential_pool"] is None
    assert getattr(built, "credential_pool", None) is None
    assert built._recover_with_credential_pool(
        status_code=401,
        has_retried_429=False,
        classified_reason=None,
        error_context={},
    )[0] is False
    assert built._try_refresh_codex_client_credentials() is False
    assert persist_calls == 0
    monkeypatch.setattr(run_agent, "AIAgent", real_agent_class)

    inspected = 0

    def builder(case, resolved):
        nonlocal inspected
        inspected += 1
        assert resolved is runtime
        assert hermes_auth.resolve_codex_runtime_credentials is smoke._credential_resolution_sentinel
        return smoke._build_agent(case)

    report, audit = smoke._run_actual_smoke_with_audit(lambda: runtime, builder)

    assert report == EXPECTED_ACTUAL_SUCCESS
    assert inspected == 5
    assert persist_calls == 0
    assert audit.credential_resolution_calls == 0

    def attacking_builder(_case, _resolved):
        hermes_auth.resolve_codex_runtime_credentials()

    failed_report, failed_audit = smoke._run_actual_smoke_with_audit(
        lambda: runtime,
        attacking_builder,
    )
    assert failed_report["pass"] is False
    assert failed_report["error_code"] == "case_failure"
    assert failed_audit.credential_resolution_calls == 5
    assert persist_calls == 0


def test_actual_builder_disables_environment_probe(monkeypatch):
    import tools.env_probe as env_probe

    calls = 0

    def ambient_probe():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(env_probe, "warm_environment_probe_async", ambient_probe)
    agent = smoke._build_actual_agent("initial_seed_positive_direct", _fake_actual_runtime())

    assert calls == 0
    assert getattr(agent, "_environment_probe", None) is False


def test_fake_report_is_exact_for_one_direct_selection_and_four_no_selections():
    report = smoke.run_fake_smoke()

    assert report == EXPECTED_SUCCESS
    assert list(report) == [
        "schema",
        "mode",
        "provider",
        "api_mode",
        "pass",
        "cases",
        "error_code",
    ]
    assert [case["case_id"] for case in report["cases"]] == CASE_IDS
    assert [case["selected_action_count"] for case in report["cases"]] == [1, 0, 0, 0, 0]
    assert [case["selected_action"] for case in report["cases"]] == [
        INITIAL_SEED_ACTION,
        None,
        None,
        None,
        None,
    ]


def test_tool_bootstrap_is_exactly_one_registered_workflow_with_one_closed_action():
    assert smoke.TOOL_DEFINITIONS == [
        {
            "type": "function",
            "function": {
                "name": "registered_local_workflow",
                "description": "Capture a closed company Work OS semantic action.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [INITIAL_SEED_ACTION],
                        }
                    },
                    "required": ["action"],
                },
            },
        }
    ]
    assert len(smoke.TOOL_DEFINITIONS) == 1
    assert list(smoke.TOOL_DEFINITIONS[0]["function"]["parameters"]["properties"]) == [
        "action"
    ]


def test_case_prompt_corpus_is_fixed_korean_and_ordered_by_frozen_meaning():
    assert smoke.CASE_PROMPTS == {
        "initial_seed_positive_direct": "회사 Work OS 초기 시드 기록을 남겨 주세요.",
        "initial_seed_negation": "회사 Work OS 초기 시드 기록을 남기지 마세요.",
        "initial_seed_question": "회사 Work OS 초기 시드 기록을 남겨야 하나요?",
        "initial_seed_plan_explanation": (
            "회사 Work OS 초기 시드 기록을 남기는 방법을 설명해 주세요."
        ),
        "initial_seed_quoted_reported": (
            '민지가 "회사 Work OS 초기 시드 기록을 남겨 주세요"라고 말했다고 전해 주세요.'
        ),
    }
    assert list(smoke.CASE_PROMPTS) == CASE_IDS


def test_removed_action_is_absent_from_source_tool_and_success_report():
    forbidden_action = bytes.fromhex(
        "636f6d70616e795f776f726b5f6f735f6f7065726174696e675f7265636f7264"
    ).decode("ascii")
    source = SCRIPT.read_text(encoding="utf-8")

    assert forbidden_action not in source
    assert forbidden_action not in json.dumps(smoke.TOOL_DEFINITIONS, ensure_ascii=False)
    assert forbidden_action not in json.dumps(EXPECTED_SUCCESS, ensure_ascii=False)
    assert forbidden_action not in json.dumps(smoke.run_fake_smoke(), ensure_ascii=False)


def test_every_case_uses_a_fresh_production_aiagent(monkeypatch):
    from run_agent import AIAgent

    real_builder = smoke._build_agent
    agents: list[AIAgent] = []

    def recording_builder(case):
        agent = real_builder(case)
        assert isinstance(agent, AIAgent)
        agents.append(agent)
        return agent

    monkeypatch.setattr(smoke, "_build_agent", recording_builder)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert len(agents) == 5
    assert len({id(agent) for agent in agents}) == 5


def test_private_audit_proves_zero_dispatch_and_network_attempts():
    report, audit = smoke._run_fake_smoke_with_audit()

    assert report == EXPECTED_SUCCESS
    assert audit.owner_dispatch_calls == 0
    assert audit.network_calls == 0
    assert audit.credential_resolution_calls == 0
    assert audit.provider_resolution_calls == 0
    assert audit.registry_dispatch_calls == 0
    assert audit.request_dump_calls == 0
    assert len(audit.agents) == 5


def test_fake_mode_installs_credential_provider_and_registry_sentinels():
    import agent.auxiliary_client as auxiliary_client
    import hermes_cli.auth as auth
    import hermes_cli.runtime_provider as runtime_provider
    import tools.registry as tool_registry

    real_builder = smoke._build_agent
    inspected = 0

    def inspecting_builder(case):
        nonlocal inspected
        inspected += 1
        assert runtime_provider.resolve_runtime_provider is smoke._provider_resolution_sentinel
        assert auxiliary_client.resolve_provider_client is smoke._provider_resolution_sentinel
        assert auxiliary_client.get_text_auxiliary_client is smoke._provider_resolution_sentinel
        assert auth.resolve_codex_runtime_credentials is smoke._credential_resolution_sentinel
        assert tool_registry.ToolRegistry.dispatch is smoke._registry_dispatch_sentinel
        return real_builder(case)

    report, audit = smoke._run_fake_smoke_with_audit(inspecting_builder)

    assert report == EXPECTED_SUCCESS
    assert inspected == 5
    assert audit.credential_resolution_calls == 0
    assert audit.provider_resolution_calls == 0
    assert audit.registry_dispatch_calls == 0


def test_fake_mode_resolution_attempt_fails_closed_and_is_audited():
    import hermes_cli.runtime_provider as runtime_provider

    def attacking_builder(_case):
        runtime_provider.resolve_runtime_provider(requested="openai-codex")

    report, audit = smoke._run_fake_smoke_with_audit(attacking_builder)

    assert report["pass"] is False
    assert report["error_code"] == "case_failure"
    assert audit.provider_resolution_calls == 5
    assert audit.credential_resolution_calls == 0
    assert audit.registry_dispatch_calls == 0


def test_capture_collector_stops_before_production_dispatch(monkeypatch):
    dispatch_attempts = 0

    def forbidden_dispatch(*_args, **_kwargs):
        nonlocal dispatch_attempts
        dispatch_attempts += 1
        raise AssertionError("production dispatch reached")

    monkeypatch.setattr(smoke, "_production_dispatch_sentinel", forbidden_dispatch)

    report, audit = smoke._run_fake_smoke_with_audit()

    assert report == EXPECTED_SUCCESS
    assert dispatch_attempts == 0
    assert audit.owner_dispatch_calls == 0


def test_fake_mode_installs_socket_sentinel_and_attempts_no_network(monkeypatch):
    socket_attempts = 0

    def forbidden_socket(*_args, **_kwargs):
        nonlocal socket_attempts
        socket_attempts += 1
        raise AssertionError("network socket reached")

    monkeypatch.setattr(smoke, "_socket_sentinel", forbidden_socket)

    report, audit = smoke._run_fake_smoke_with_audit()

    assert report == EXPECTED_SUCCESS
    assert socket_attempts == 0
    assert audit.network_calls == 0


def test_fake_mode_never_resolves_or_calls_an_actual_provider(monkeypatch):
    import agent.auxiliary_client as auxiliary_client

    provider_attempts = 0

    def forbidden_provider(*_args, **_kwargs):
        nonlocal provider_attempts
        provider_attempts += 1
        raise AssertionError("actual provider resolution reached")

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", forbidden_provider)
    monkeypatch.setattr(auxiliary_client, "get_text_auxiliary_client", forbidden_provider)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert provider_attempts == 0


def test_malformed_provider_event_fails_closed_without_raw_exception_or_output(monkeypatch, capsys):
    real_events = smoke._provider_events_for_prompt
    sensitive_exception = "RAW_PRIVATE_EXCEPTION credential-sentinel-7719"

    def malformed_events(prompt):
        events = real_events(prompt)
        for event in events:
            item = getattr(event, "item", None)
            if item is not None and getattr(item, "type", None) == "function_call":
                item.arguments = "{" + sensitive_exception
        return events

    monkeypatch.setattr(smoke, "_provider_events_for_prompt", malformed_events)

    report = smoke.run_fake_smoke()
    captured = capsys.readouterr()
    encoded = json.dumps(report, ensure_ascii=False)

    _assert_case_failure(report)
    assert report["cases"][0] == _case(
        "initial_seed_positive_direct", 0, None, "selection_mismatch"
    )
    assert sensitive_exception not in encoded
    assert "RAW_PRIVATE_EXCEPTION" not in encoded
    assert "credential-sentinel-7719" not in encoded
    assert captured.out == ""
    assert captured.err == ""


def test_repairable_malformed_tool_name_is_stdout_contained(monkeypatch, capsys) -> None:
    real_events = smoke._provider_events_for_prompt
    malformed_name = "registered_local_workflaw"

    def repairable_events(prompt):
        events = real_events(prompt)
        for event in events:
            item = getattr(event, "item", None)
            if item is not None and getattr(item, "type", None) == "function_call":
                item.name = malformed_name
        return events

    monkeypatch.setattr(smoke, "_provider_events_for_prompt", repairable_events)

    exit_code = smoke.main([])
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    _assert_case_failure(document)
    assert malformed_name not in captured.out
    assert "Auto-repaired tool name" not in captured.out


@pytest.mark.parametrize(
    "fault",
    ("missing_created", "missing_completed", "duplicate_function_call"),
)
def test_malformed_raw_provider_transcript_fails_closed(monkeypatch, fault) -> None:
    real_events = smoke._provider_events_for_prompt

    def faulty_events(prompt):
        events = real_events(prompt)
        if fault == "missing_created":
            return [event for event in events if event.type != "response.created"]
        if fault == "missing_completed":
            return [event for event in events if event.type != "response.completed"]
        duplicated = list(events)
        for event in events:
            item = getattr(event, "item", None)
            if item is not None and getattr(item, "type", None) == "function_call":
                extra = deepcopy(event)
                extra.item.id = "fc_duplicate"
                extra.item.call_id = "call_duplicate"
                duplicated.insert(-1, extra)
                break
        return duplicated

    monkeypatch.setattr(smoke, "_provider_events_for_prompt", faulty_events)

    report, audit = smoke._run_fake_smoke_with_audit()

    _assert_case_failure(report)
    assert audit.owner_dispatch_calls == 0
    assert audit.network_calls == 0


def test_fake_mode_seals_ambient_llm_middleware(monkeypatch) -> None:
    import hermes_cli.middleware as middleware

    calls = 0

    def ambient_request(payload, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(payload=payload, original_payload=payload, trace=[])

    def ambient_execution(payload, perform_api_call, **_kwargs):
        nonlocal calls
        calls += 1
        return perform_api_call(payload)

    monkeypatch.setattr(middleware, "apply_llm_request_middleware", ambient_request)
    monkeypatch.setattr(middleware, "run_llm_execution_middleware", ambient_execution)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert calls == 0


def test_fake_mode_seals_and_restores_ambient_request_dump(monkeypatch) -> None:
    from run_agent import AIAgent

    calls = 0

    def ambient_dump(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setenv("HERMES_DUMP_REQUESTS", "1")
    monkeypatch.setattr(AIAgent, "_dump_api_request_debug", ambient_dump)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert calls == 0
    assert os.environ["HERMES_DUMP_REQUESTS"] == "1"


def test_fake_mode_restores_global_audit_and_logging_state():
    previous_audit = smoke._ACTIVE_AUDIT
    previous_logging_threshold = logging.root.manager.disable

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert smoke._ACTIVE_AUDIT is previous_audit
    assert logging.root.manager.disable == previous_logging_threshold


def test_fake_cli_is_deterministic_closed_and_does_not_emit_private_material():
    sentinels = {
        "OPENAI_API_KEY": "credential-sentinel-openai-1193",
        "DISCORD_BOT_TOKEN": "credential-sentinel-discord-2284",
        "PRIVATE_OWNER_ID": "private-id-sentinel-3375",
        "PRIVATE_PATH": "/private/sentinel/path-4486",
    }

    first = _run_cli("--mode", "fake", extra_env=sentinels)
    second = _run_cli("--mode", "fake", extra_env=sentinels)

    assert first.returncode == 0
    assert second.returncode == 0
    assert first.stderr == ""
    assert second.stderr == ""
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == EXPECTED_SUCCESS

    for prompt in smoke.CASE_PROMPTS.values():
        assert prompt not in first.stdout
    for sentinel in sentinels.values():
        assert sentinel not in first.stdout
    assert "fake-no-secret" not in first.stdout
    assert "resp_fake_semantic_smoke" not in first.stdout
    assert "call_initial_seed_positive_direct" not in first.stdout
    assert "arguments" not in first.stdout
    assert "exception" not in first.stdout.lower()
    assert "timestamp" not in first.stdout.lower()
    assert str(ROOT) not in first.stdout


def test_no_args_defaults_to_fake_and_invalid_cli_is_constant_safe_with_empty_stderr():
    default_run = _run_cli()
    invalid_mode = _run_cli("--mode", "live")
    extra_arg = _run_cli("--mode", "fake", "--extra-private-argument")

    assert default_run.returncode == 0
    assert default_run.stderr == ""
    assert json.loads(default_run.stdout) == EXPECTED_SUCCESS

    for result in (invalid_mode, extra_arg):
        assert result.returncode == 2
        assert result.stderr == ""
        assert json.loads(result.stdout) == EXPECTED_INVALID
        assert "live" not in result.stdout
        assert "extra-private-argument" not in result.stdout


def test_actual_cli_routes_in_process_without_resolving_or_calling_provider(monkeypatch, capsys):
    calls = 0

    def offline_actual():
        nonlocal calls
        calls += 1
        return EXPECTED_ACTUAL_SUCCESS

    monkeypatch.setattr(smoke, "run_actual_smoke", offline_actual)

    assert smoke.main(["--mode", "actual"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == EXPECTED_ACTUAL_SUCCESS
    assert calls == 1


@pytest.mark.parametrize("base_url", (None, "", "not-a-url"))
def test_actual_builder_rejects_invalid_base_url_before_secondary_resolution(
    monkeypatch, base_url
) -> None:
    import agent.auxiliary_client as auxiliary_client

    secondary_calls = 0

    def forbidden_secondary(*_args, **_kwargs):
        nonlocal secondary_calls
        secondary_calls += 1
        raise AssertionError("secondary provider resolution reached")

    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", forbidden_secondary)
    runtime = smoke._ActualRuntime(
        model="offline-model",
        provider="openai-codex",
        api_mode="codex_responses",
        api_key="offline-key",
        base_url=base_url,
    )

    with pytest.raises(smoke._ActualProviderFailure):
        smoke._build_actual_agent("initial_seed_positive_direct", runtime)

    assert secondary_calls == 0


def test_actual_error_path_seals_request_dump_and_audits_attempts(monkeypatch) -> None:
    from run_agent import AIAgent

    ambient_dump_calls = 0

    def ambient_dump(*_args, **_kwargs):
        nonlocal ambient_dump_calls
        ambient_dump_calls += 1

    monkeypatch.setattr(AIAgent, "_dump_api_request_debug", ambient_dump)

    def failing_builder(case, _runtime):
        agent = smoke._build_agent(case)

        def failing_run(_prompt):
            agent._dump_api_request_debug({}, reason="non_retryable_client_error")
            raise RuntimeError("private-provider-error")

        setattr(agent, "run_conversation", failing_run)
        return agent

    report, audit = smoke._run_actual_smoke_with_audit(
        _fake_actual_runtime,
        failing_builder,
    )

    assert report["pass"] is False
    assert report["error_code"] == "case_failure"
    assert ambient_dump_calls == 0
    assert audit.request_dump_calls == 5


def test_all_direct_routes_avoid_real_actual_resolver(monkeypatch, capsys):
    resolver_calls = 0

    def forbidden_real_resolver():
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("real actual resolver reached")

    monkeypatch.setattr(smoke, "_resolve_actual_runtime", forbidden_real_resolver)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert smoke.main([]) == 0
    assert smoke.main(["--mode", "fake"]) == 0
    assert smoke.main(["--mode", "unsupported"]) == 2
    assert smoke.run_actual_smoke(
        runtime_resolver=_fake_actual_runtime,
        agent_builder=lambda case, _runtime: smoke._build_agent(case),
    ) == EXPECTED_ACTUAL_SUCCESS
    capsys.readouterr()
    assert resolver_calls == 0


def test_internal_failure_is_constant_safe_and_uses_only_closed_error(monkeypatch, capsys):
    private_exception = "private-exception-should-not-escape"

    def fail_run():
        raise KeyboardInterrupt(private_exception)

    monkeypatch.setattr(smoke, "run_fake_smoke", fail_run)

    assert smoke.main([]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == EXPECTED_INTERNAL_FAILURE
    assert private_exception not in captured.out


def test_provider_stream_is_provider_shaped_and_runs_through_conversation_parser(monkeypatch):
    seen_event_types: list[list[str]] = []
    real_events = smoke._provider_events_for_prompt

    def recording_events(prompt):
        events = real_events(prompt)
        seen_event_types.append([event.type for event in events])
        return events

    monkeypatch.setattr(smoke, "_provider_events_for_prompt", recording_events)

    assert smoke.run_fake_smoke() == EXPECTED_SUCCESS
    assert len(seen_event_types) == 5
    assert all(types[0] == "response.created" for types in seen_event_types)
    assert all("response.output_item.done" in types for types in seen_event_types)
    assert all(types[-1] == "response.completed" for types in seen_event_types)


def test_capture_stop_is_private_base_exception():
    assert issubclass(smoke._CaptureStop, BaseException)
    assert not issubclass(smoke._CaptureStop, Exception)


def test_closed_report_has_only_declared_fields_and_closed_error_values():
    reports = [
        smoke.run_fake_smoke(),
        smoke._failure_report("invalid", "invalid_cli"),
        EXPECTED_ACTUAL_SUCCESS,
        EXPECTED_PROVIDER_FAILURE,
    ]
    allowed_errors = {
        None,
        "selection_mismatch",
        "case_failure",
        "not_run",
        "invalid_cli",
        "internal_failure",
        "provider_failure",
    }

    for report in reports:
        assert list(report) == [
            "schema",
            "mode",
            "provider",
            "api_mode",
            "pass",
            "cases",
            "error_code",
        ]
        assert report["error_code"] in allowed_errors
        assert all(
            list(case) == [
                "case_id",
                "selected_action_count",
                "selected_action",
                "error_code",
            ]
            for case in report["cases"]
        )
        assert all(case["error_code"] in allowed_errors for case in report["cases"])
        serialized = json.dumps(report, ensure_ascii=False)
        assert "owner_dispatch_calls" not in serialized
        assert "network_calls" not in serialized
        assert "credential" not in serialized.lower()
        assert "prompt" not in serialized.lower()
        assert "path" not in serialized.lower()
