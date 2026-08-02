#!/usr/bin/env python3
"""Deterministic capture-only semantic-action smoke with no live dispatch."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import socket
import sys
from types import MethodType, SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch
from urllib.parse import urlparse


SCHEMA = "semantic-action-capture-smoke/v1"
TOOL_NAME = "registered_local_workflow"
ACTIONS = (
    "company_work_os_initial_seed_record",
)
CASE_PROMPTS = {
    "initial_seed_positive_direct": "회사 Work OS 초기 시드 기록을 남겨 주세요.",
    "initial_seed_negation": "회사 Work OS 초기 시드 기록을 남기지 마세요.",
    "initial_seed_question": "회사 Work OS 초기 시드 기록을 남겨야 하나요?",
    "initial_seed_plan_explanation": "회사 Work OS 초기 시드 기록을 남기는 방법을 설명해 주세요.",
    "initial_seed_quoted_reported": (
        '민지가 "회사 Work OS 초기 시드 기록을 남겨 주세요"라고 말했다고 전해 주세요.'
    ),
}
CASE_IDS = tuple(CASE_PROMPTS)
EXPECTED_ACTIONS = {
    "initial_seed_positive_direct": "company_work_os_initial_seed_record",
    "initial_seed_negation": None,
    "initial_seed_question": None,
    "initial_seed_plan_explanation": None,
    "initial_seed_quoted_reported": None,
}
SYSTEM_PROMPT = (
    "Call the supplied tool exactly once only for a direct current-user instruction "
    "to record the company Work OS initial seed. Do not call it for negation, "
    "questions, plans or explanations, quoted text, or reported speech. "
    "Otherwise answer normally without tools."
)
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Capture a closed company Work OS semantic action.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(ACTIONS),
                    }
                },
                "required": ["action"],
            },
        },
    }
]


class _CaptureStop(BaseException):
    """Stop a turn before any normalized tool call can reach dispatch."""


class _OwnerDispatchStop(BaseException):
    """Fail closed if a production dispatch seam is reached."""


class _NetworkStop(BaseException):
    """Fail closed if code attempts to construct a network socket."""


class _CredentialResolutionStop(BaseException):
    """Fail closed if fake mode reaches any credential resolver."""


class _ProviderResolutionStop(BaseException):
    """Fail closed if fake mode reaches any provider resolver."""


class _RegistryDispatchStop(BaseException):
    """Fail closed if fake mode reaches the effective tool registry."""


class _RawActualSelectionStop(Exception):
    """Stop malformed actual provider output before production repair or retry."""


class _ActualProviderFailure(Exception):
    """Constant-safe actual runtime resolution failure."""


@dataclass
class _Audit:
    owner_dispatch_calls: int = 0
    network_calls: int = 0
    credential_resolution_calls: int = 0
    provider_resolution_calls: int = 0
    registry_dispatch_calls: int = 0
    request_dump_calls: int = 0
    actual_provider_requests: dict[str, int] = field(default_factory=dict)
    agents: list[Any] = field(default_factory=list)
    invalid_transcripts: set[str] = field(default_factory=set)


@dataclass
class _Capture:
    call_count: int = 0
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    valid: bool = False


@dataclass(frozen=True)
class _ActualRuntime:
    model: str
    provider: str
    api_mode: str
    api_key: str
    base_url: str | None
    credential_pool: Any = None


_ACTIVE_AUDIT: _Audit | None = None


def _production_dispatch_sentinel(*_args: Any, **_kwargs: Any) -> None:
    """Module-level last-resort guard against any production tool dispatch."""
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.owner_dispatch_calls += 1
    raise _OwnerDispatchStop()


def _socket_sentinel(*_args: Any, **_kwargs: Any) -> None:
    """Module-level fake-mode guard against network socket construction."""
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.network_calls += 1
    raise _NetworkStop()


def _credential_resolution_sentinel(*_args: Any, **_kwargs: Any) -> None:
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.credential_resolution_calls += 1
    raise _CredentialResolutionStop()


def _provider_resolution_sentinel(*_args: Any, **_kwargs: Any) -> None:
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.provider_resolution_calls += 1
    raise _ProviderResolutionStop()


def _registry_dispatch_sentinel(*_args: Any, **_kwargs: Any) -> None:
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.registry_dispatch_calls += 1
    raise _RegistryDispatchStop()


def _request_dump_sentinel(*_args: Any, **_kwargs: Any) -> None:
    if _ACTIVE_AUDIT is not None:
        _ACTIVE_AUDIT.request_dump_calls += 1


def _valid_actual_base_url(value: Any) -> bool:
    if type(value) is not str or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def _actual_runtime_is_valid(runtime: Any) -> bool:
    return bool(
        type(runtime) is _ActualRuntime
        and type(runtime.model) is str
        and runtime.model.strip()
        and runtime.provider == "openai-codex"
        and runtime.api_mode == "codex_responses"
        and type(runtime.api_key) is str
        and runtime.api_key.strip()
        and _valid_actual_base_url(runtime.base_url)
    )


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)


def _terminal_event() -> SimpleNamespace:
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            id="resp_fake_semantic_smoke",
            status="completed",
            usage=_usage(),
            model="fake-codex-responses",
        ),
    )


def _case_id_for_prompt(prompt: str) -> str:
    try:
        return next(
            candidate for candidate, fixed_prompt in CASE_PROMPTS.items() if prompt == fixed_prompt
        )
    except StopIteration:
        raise RuntimeError("closed fake prompt mismatch") from None


def _provider_events_for_prompt(prompt: str) -> list[SimpleNamespace]:
    """Return provider-shaped Codex Responses events for one exact fixed prompt."""
    case_id = _case_id_for_prompt(prompt)

    action = EXPECTED_ACTIONS[case_id]
    if action is None:
        item = SimpleNamespace(
            type="message",
            role="assistant",
            status="completed",
            content=[
                SimpleNamespace(
                    type="output_text",
                    text="No semantic action selected.",
                )
            ],
        )
    else:
        item = SimpleNamespace(
            type="function_call",
            id=f"fc_{case_id}",
            call_id=f"call_{case_id}",
            name=TOOL_NAME,
            arguments=json.dumps(
                {"action": action},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            status="completed",
        )

    return [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_item.done", item=item),
        _terminal_event(),
    ]


def _raw_transcript_is_valid(events: list[SimpleNamespace], case_id: str) -> bool:
    if [getattr(event, "type", None) for event in events] != [
        "response.created",
        "response.output_item.done",
        "response.completed",
    ]:
        return False

    terminal_response = getattr(events[2], "response", None)
    usage = getattr(terminal_response, "usage", None)
    if (
        getattr(terminal_response, "id", None) != "resp_fake_semantic_smoke"
        or getattr(terminal_response, "status", None) != "completed"
        or getattr(terminal_response, "model", None) != "fake-codex-responses"
        or getattr(usage, "input_tokens", None) != 1
        or getattr(usage, "output_tokens", None) != 1
        or getattr(usage, "total_tokens", None) != 2
    ):
        return False

    item = getattr(events[1], "item", None)
    expected_action = EXPECTED_ACTIONS[case_id]
    if expected_action is None:
        content = getattr(item, "content", None)
        return bool(
            getattr(item, "type", None) == "message"
            and getattr(item, "role", None) == "assistant"
            and getattr(item, "status", None) == "completed"
            and type(content) is list
            and len(content) == 1
            and getattr(content[0], "type", None) == "output_text"
            and getattr(content[0], "text", None) == "No semantic action selected."
        )
    if (
        getattr(item, "type", None) != "function_call"
        or getattr(item, "id", None) != f"fc_{case_id}"
        or getattr(item, "call_id", None) != f"call_{case_id}"
        or getattr(item, "name", None) != TOOL_NAME
        or getattr(item, "status", None) != "completed"
    ):
        return False
    raw_arguments = getattr(item, "arguments", None)
    if type(raw_arguments) is not str:
        return False
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError):
        return False
    return bool(
        type(arguments) is dict
        and set(arguments) == {"action"}
        and arguments.get("action") == expected_action
    )


def _text_from_content(content: Any) -> str | None:
    if type(content) is str:
        return content
    if type(content) is not list:
        return None
    parts: list[str] = []
    for part in content:
        if type(part) is dict:
            text = part.get("text")
        else:
            text = getattr(part, "text", None)
        if type(text) is str:
            parts.append(text)
    return "".join(parts) if parts else None


def _prompt_from_request(request: dict[str, Any]) -> str:
    items = request.get("input")
    if type(items) is not list:
        raise RuntimeError("closed fake input mismatch")
    for item in reversed(items):
        if type(item) is dict:
            role = item.get("role")
            content = item.get("content")
        else:
            role = getattr(item, "role", None)
            content = getattr(item, "content", None)
        if role == "user":
            text = _text_from_content(content)
            if text is not None:
                return text
    raise RuntimeError("closed fake user input missing")


class _FakeEventStream:
    def __init__(self, events: list[SimpleNamespace]):
        self._events = tuple(events)
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        self.closed = True


class _FakeResponses:
    def create(self, **request: Any) -> _FakeEventStream:
        if request.get("stream") is not True:
            raise RuntimeError("closed fake stream contract mismatch")
        prompt = _prompt_from_request(request)
        case_id = _case_id_for_prompt(prompt)
        events = _provider_events_for_prompt(prompt)
        if _ACTIVE_AUDIT is not None and not _raw_transcript_is_valid(events, case_id):
            _ACTIVE_AUDIT.invalid_transcripts.add(case_id)
        return _FakeEventStream(events)


class _FakeClient:
    def __init__(self):
        self.responses = _FakeResponses()

    def close(self) -> None:
        return None


def _field(value: Any, name: str) -> Any:
    return value.get(name) if type(value) is dict else getattr(value, name, None)


def _validate_raw_actual_call(item: Any, case_id: str, call_count: int) -> None:
    expected_action = EXPECTED_ACTIONS[case_id]
    if expected_action is None or call_count != 1:
        raise _RawActualSelectionStop()
    if _field(item, "name") != TOOL_NAME:
        raise _RawActualSelectionStop()
    raw_arguments = _field(item, "arguments")
    if type(raw_arguments) is not str:
        raise _RawActualSelectionStop()
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, ValueError):
        raise _RawActualSelectionStop() from None
    if (
        type(arguments) is not dict
        or set(arguments) != {"action"}
        or arguments.get("action") != expected_action
    ):
        raise _RawActualSelectionStop()


class _ActualGuardedStream:
    def __init__(self, stream: Any, case_id: str):
        self._stream = stream
        self._iterator = iter(stream)
        self._case_id = case_id
        self._function_call_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        event = next(self._iterator)
        if _field(event, "type") == "response.output_item.done":
            item = _field(event, "item")
            if _field(item, "type") == "function_call":
                self._function_call_count += 1
                _validate_raw_actual_call(
                    item,
                    self._case_id,
                    self._function_call_count,
                )
        return event

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


class _ActualGuardedResponses:
    def __init__(self, responses: Any, case_id: str, audit: _Audit):
        self._responses = responses
        self._case_id = case_id
        self._audit = audit

    def create(self, **request: Any) -> _ActualGuardedStream:
        if self._audit.actual_provider_requests.get(self._case_id, 0) >= 1:
            raise _RawActualSelectionStop()
        self._audit.actual_provider_requests[self._case_id] = 1
        return _ActualGuardedStream(
            self._responses.create(**request),
            self._case_id,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._responses, name)


class _ActualGuardedClient:
    def __init__(self, client: Any, case_id: str, audit: _Audit):
        self._client = client
        self.responses = _ActualGuardedResponses(client.responses, case_id, audit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _guard_actual_client(client: Any, case_id: str, audit: _Audit) -> Any:
    if isinstance(client, _ActualGuardedClient):
        return client
    if client is None or not hasattr(client, "responses"):
        return client
    return _ActualGuardedClient(client, case_id, audit)


def _install_actual_provider_guard(agent: Any, case_id: str, audit: _Audit) -> None:
    client = getattr(agent, "client", None)
    if client is not None:
        agent.client = _guard_actual_client(client, case_id, audit)

    factory = getattr(agent, "_create_request_openai_client", None)
    if callable(factory):
        def guarded_factory(*args: Any, **kwargs: Any) -> Any:
            return _guard_actual_client(factory(*args, **kwargs), case_id, audit)

        agent._create_request_openai_client = guarded_factory


def _no_credential_pool_recovery(*_args: Any, **kwargs: Any) -> tuple[bool, bool]:
    return False, bool(kwargs.get("has_retried_429", False))


def _no_credential_refresh(*_args: Any, **_kwargs: Any) -> bool:
    return False


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _identity_request_middleware(payload: dict[str, Any], **_kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(payload=payload, original_payload=dict(payload), trace=[])


def _direct_execution_middleware(
    payload: dict[str, Any],
    perform_api_call: Callable[[dict[str, Any]], Any],
    **_kwargs: Any,
) -> Any:
    return perform_api_call(payload)


def _resolve_actual_runtime() -> _ActualRuntime:
    """Resolve the fixed actual runtime lazily without exposing private material."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider

        config = load_config()
        if type(config) is not dict:
            raise ValueError
        model_config = config.get("model")
        if type(model_config) is str:
            model = model_config.strip()
        elif type(model_config) is dict:
            configured = model_config.get("default")
            if type(configured) is not str or not configured.strip():
                configured = model_config.get("model")
            model = configured.strip() if type(configured) is str else ""
        else:
            model = ""
        if not model:
            raise ValueError

        resolved = resolve_runtime_provider(
            requested="openai-codex",
            target_model=model,
        )
        if type(resolved) is not dict:
            raise ValueError

        provider_value = resolved.get("provider")
        api_mode_value = resolved.get("api_mode")
        api_key_value = resolved.get("api_key")
        base_url_value = resolved.get("base_url")
        if (
            type(provider_value) is not str
            or provider_value.strip().lower() != "openai-codex"
            or type(api_mode_value) is not str
            or api_mode_value.strip().lower() != "codex_responses"
            or type(api_key_value) is not str
            or not api_key_value.strip()
            or not _valid_actual_base_url(base_url_value)
        ):
            raise ValueError
        assert type(base_url_value) is str

        return _ActualRuntime(
            model=model,
            provider="openai-codex",
            api_mode="codex_responses",
            api_key=api_key_value.strip(),
            base_url=base_url_value.strip(),
            credential_pool=resolved.get("credential_pool"),
        )
    except BaseException:
        raise _ActualProviderFailure() from None


def _build_agent(case: Any):
    """Build one fresh production AIAgent around a provider-shaped fake client."""
    del case  # Selection occurs from the request prompt at the fake provider seam.

    import agent.agent_init as agent_init
    import agent.memory_manager as memory_manager
    import agent.model_metadata as model_metadata
    import gateway.session_context as session_context
    import hermes_cli.config as hermes_config
    import hermes_logging
    import run_agent
    import tools.registry as tool_registry

    fake_client = _FakeClient()
    inert_home = Path("/__semantic_action_capture_smoke_no_live__")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(run_agent, "get_tool_definitions", return_value=deepcopy(TOOL_DEFINITIONS))
        )
        stack.enter_context(patch.object(run_agent, "check_toolset_requirements", return_value={}))
        stack.enter_context(
            patch.object(
                run_agent.AIAgent,
                "_create_openai_client",
                return_value=fake_client,
            )
        )
        stack.enter_context(patch.object(
                hermes_config,
                "load_config",
                return_value={"agent": {"environment_probe": False}},
            ))
        stack.enter_context(patch.object(agent_init, "get_provider_request_timeout", return_value=None))
        stack.enter_context(patch.object(agent_init, "get_hermes_home", return_value=inert_home))
        stack.enter_context(patch.object(model_metadata, "fetch_model_metadata", return_value={}))
        stack.enter_context(patch.object(hermes_logging, "setup_logging", side_effect=_noop))
        stack.enter_context(patch.object(hermes_logging, "setup_verbose_logging", side_effect=_noop))
        stack.enter_context(patch.object(session_context, "set_current_session_id", side_effect=_noop))
        stack.enter_context(
            patch.object(tool_registry, "registry", SimpleNamespace(_generation=0))
        )
        stack.enter_context(
            patch.object(memory_manager, "inject_memory_provider_tools", side_effect=_noop)
        )
        stack.enter_context(patch.object(Path, "mkdir", side_effect=_noop))

        agent = run_agent.AIAgent(
            model="fake-codex-responses",
            provider="custom",
            api_mode="codex_responses",
            base_url="https://fake.invalid/v1",
            api_key="fake-no-secret",
            max_iterations=2,
            tool_delay=0,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            platform="fake",
        )

    # Re-seal the exact tool surface after all normal initialization injectors.
    agent.tools = deepcopy(TOOL_DEFINITIONS)
    agent.valid_tool_names = {TOOL_NAME}
    agent.client = fake_client
    agent._create_request_openai_client = lambda **_kwargs: fake_client
    setattr(agent, "_cached_system_prompt", SYSTEM_PROMPT)
    agent._use_prompt_caching = False
    agent._skip_mcp_refresh = True
    agent._persist_disabled = True
    agent._session_db = None
    agent._session_json_enabled = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.suppress_status_output = True
    agent._print_fn = _noop
    agent._persist_session = _noop
    agent._flush_messages_to_session_db = _noop
    agent._save_trajectory = _noop
    agent._cleanup_task_resources = _noop
    return agent


def _build_actual_agent(case: Any, runtime: _ActualRuntime):
    """Build a fresh production AIAgent for the separately approved actual path."""
    del case  # Selection occurs from the fixed prompt at the production provider seam.
    if not _actual_runtime_is_valid(runtime):
        raise _ActualProviderFailure()

    import agent.agent_init as agent_init
    import agent.memory_manager as memory_manager
    import agent.model_metadata as model_metadata
    import gateway.session_context as session_context
    import hermes_cli.config as hermes_config
    import hermes_logging
    import run_agent
    import tools.registry as tool_registry

    inert_home = Path("/__semantic_action_capture_smoke_actual__")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(run_agent, "get_tool_definitions", return_value=deepcopy(TOOL_DEFINITIONS))
        )
        stack.enter_context(patch.object(run_agent, "check_toolset_requirements", return_value={}))
        stack.enter_context(patch.object(
                hermes_config,
                "load_config",
                return_value={"agent": {"environment_probe": False}},
            ))
        stack.enter_context(patch.object(agent_init, "get_provider_request_timeout", return_value=None))
        stack.enter_context(patch.object(agent_init, "get_hermes_home", return_value=inert_home))
        stack.enter_context(patch.object(model_metadata, "fetch_model_metadata", return_value={}))
        stack.enter_context(patch.object(hermes_logging, "setup_logging", side_effect=_noop))
        stack.enter_context(patch.object(hermes_logging, "setup_verbose_logging", side_effect=_noop))
        stack.enter_context(patch.object(session_context, "set_current_session_id", side_effect=_noop))
        stack.enter_context(
            patch.object(tool_registry, "registry", SimpleNamespace(_generation=0))
        )
        stack.enter_context(
            patch.object(memory_manager, "inject_memory_provider_tools", side_effect=_noop)
        )
        stack.enter_context(patch.object(Path, "mkdir", side_effect=_noop))

        agent = run_agent.AIAgent(
            model=runtime.model,
            provider=runtime.provider,
            api_mode=runtime.api_mode,
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            credential_pool=None,
            max_iterations=1,
            tool_delay=0,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            platform="semantic-action-capture-smoke",
        )

    # Re-seal the exact tool surface after all normal initialization injectors.
    agent.tools = deepcopy(TOOL_DEFINITIONS)
    agent.valid_tool_names = {TOOL_NAME}
    setattr(agent, "_cached_system_prompt", SYSTEM_PROMPT)
    agent._use_prompt_caching = False
    agent._skip_mcp_refresh = True
    agent._persist_disabled = True
    agent._session_db = None
    agent._session_json_enabled = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.suppress_status_output = True
    agent._print_fn = _noop
    agent._persist_session = _noop
    agent._flush_messages_to_session_db = _noop
    agent._save_trajectory = _noop
    agent._cleanup_task_resources = _noop
    agent._dump_api_request_debug = _request_dump_sentinel
    setattr(agent, "credential_pool", None)
    agent._recover_with_credential_pool = MethodType(_no_credential_pool_recovery, agent)
    agent._try_refresh_codex_client_credentials = MethodType(_no_credential_refresh, agent)
    setattr(agent, "_environment_probe", False)
    return agent


def _normalize_capture(assistant_message: Any) -> _Capture:
    calls = getattr(assistant_message, "tool_calls", None)
    if type(calls) is not list or not calls:
        return _Capture()

    capture = _Capture(call_count=len(calls))
    first = calls[0]
    function = getattr(first, "function", None)
    name = getattr(function, "name", None)
    raw_arguments = getattr(function, "arguments", None)
    capture.tool_name = name if type(name) is str else None

    if type(raw_arguments) is str:
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            arguments = None
    elif type(raw_arguments) is dict:
        arguments = dict(raw_arguments)
    else:
        arguments = None

    capture.arguments = arguments if type(arguments) is dict else None
    capture.valid = bool(
        capture.call_count == 1
        and capture.tool_name == TOOL_NAME
        and type(capture.arguments) is dict
        and set(capture.arguments) == {"action"}
        and type(capture.arguments.get("action")) is str
        and capture.arguments["action"] in ACTIONS
    )
    return capture


def _install_collector(agent: Any, capture_box: list[_Capture]) -> None:
    def collector(
        _agent: Any,
        assistant_message: Any,
        _messages: list[Any],
        _effective_task_id: str,
        _api_call_count: int = 0,
    ) -> None:
        capture_box[:] = [_normalize_capture(assistant_message)]
        raise _CaptureStop()

    agent._execute_tool_calls = MethodType(collector, agent)


def _case_row(
    case_id: str,
    *,
    capture: _Capture | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    capture = capture or _Capture()
    captured_action = capture.arguments.get("action") if capture.arguments else None
    safe_action = captured_action if captured_action in ACTIONS else None
    return {
        "case_id": case_id,
        "selected_action_count": capture.call_count,
        "selected_action": safe_action,
        "error_code": error_code,
    }


def _failure_report(
    mode: str = "invalid",
    error_code: str = "invalid_cli",
) -> dict[str, Any]:
    is_fake = mode == "fake"
    is_actual = mode == "actual"
    cases = [_case_row(case_id, error_code="not_run") for case_id in CASE_IDS]
    return {
        "schema": SCHEMA,
        "mode": mode,
        "provider": "fake" if is_fake else "openai-codex" if is_actual else None,
        "api_mode": "codex_responses" if is_fake or is_actual else None,
        "pass": False,
        "cases": cases,
        "error_code": error_code,
    }


def _run_fake_smoke_with_audit(
    agent_builder: Callable[[Any], Any] | None = None,
) -> tuple[dict[str, Any], _Audit]:
    """Run all fixed cases through fresh production AIAgent instances."""
    global _ACTIVE_AUDIT

    builder = agent_builder or _build_agent
    audit = _Audit()
    previous_audit = _ACTIVE_AUDIT
    previous_logging_threshold = logging.root.manager.disable
    _ACTIVE_AUDIT = audit
    logging.disable(logging.CRITICAL)
    rows: list[dict[str, Any]] = []
    internal_failure = False

    try:
        import agent.auxiliary_client as auxiliary_client
        import agent.conversation_loop as conversation_loop
        import agent.secret_scope as secret_scope
        import hermes_cli.auth as hermes_auth
        import hermes_cli.config as hermes_config
        import hermes_cli.middleware as hermes_middleware
        import hermes_cli.plugins as hermes_plugins
        import hermes_cli.runtime_provider as runtime_provider
        import run_agent
        import tools.registry as tool_registry

        with ExitStack() as stack:
            devnull = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            stack.enter_context(redirect_stdout(devnull))
            stack.enter_context(redirect_stderr(devnull))
            stack.enter_context(patch.dict(os.environ, {"HERMES_DUMP_REQUESTS": ""}))
            stack.enter_context(patch.object(socket, "socket", _socket_sentinel))
            stack.enter_context(
                patch.object(run_agent, "handle_function_call", _production_dispatch_sentinel)
            )
            stack.enter_context(
                patch.object(
                    run_agent.AIAgent,
                    "_dump_api_request_debug",
                    _request_dump_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    runtime_provider,
                    "resolve_runtime_provider",
                    _provider_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    auxiliary_client,
                    "resolve_provider_client",
                    _provider_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    auxiliary_client,
                    "get_text_auxiliary_client",
                    _provider_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    hermes_auth,
                    "resolve_codex_runtime_credentials",
                    _credential_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    runtime_provider,
                    "resolve_codex_runtime_credentials",
                    _credential_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    runtime_provider,
                    "_get_secret",
                    _credential_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    secret_scope,
                    "get_secret",
                    _credential_resolution_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    tool_registry.ToolRegistry,
                    "dispatch",
                    _registry_dispatch_sentinel,
                )
            )
            stack.enter_context(patch.object(
                hermes_config,
                "load_config",
                return_value={"agent": {"environment_probe": False}},
            ))
            stack.enter_context(patch.object(hermes_plugins, "has_hook", return_value=False))
            stack.enter_context(patch.object(hermes_plugins, "invoke_hook", return_value=[]))
            stack.enter_context(
                patch.object(
                    hermes_middleware,
                    "apply_llm_request_middleware",
                    _identity_request_middleware,
                )
            )
            stack.enter_context(
                patch.object(
                    hermes_middleware,
                    "run_llm_execution_middleware",
                    _direct_execution_middleware,
                )
            )
            stack.enter_context(
                patch.object(conversation_loop, "jittered_backoff", return_value=0.0)
            )
            stack.enter_context(
                patch.object(
                    conversation_loop,
                    "adaptive_rate_limit_backoff",
                    return_value=0.0,
                )
            )

            for case_id in CASE_IDS:
                expected_action = EXPECTED_ACTIONS[case_id]
                capture_box: list[_Capture] = []
                result: dict[str, Any] | None = None
                stopped_on_capture = False
                failed = False

                try:
                    agent = builder(case_id)
                    audit.agents.append(agent)  # Retain fresh identities until the full run ends.
                    _install_collector(agent, capture_box)
                    result = agent.run_conversation(CASE_PROMPTS[case_id])
                except _CaptureStop:
                    stopped_on_capture = True
                except BaseException:
                    failed = True

                capture = capture_box[0] if capture_box else _Capture()
                if expected_action is None:
                    passed = bool(
                        not failed
                        and case_id not in audit.invalid_transcripts
                        and not stopped_on_capture
                        and capture.call_count == 0
                        and type(result) is dict
                        and result.get("completed") is True
                        and type(result.get("final_response")) is str
                    )
                else:
                    captured_action = capture.arguments.get("action") if capture.arguments else None
                    passed = bool(
                        not failed
                        and case_id not in audit.invalid_transcripts
                        and stopped_on_capture
                        and capture.valid
                        and capture.call_count == 1
                        and capture.tool_name == TOOL_NAME
                        and captured_action == expected_action
                    )
                rows.append(
                    _case_row(
                        case_id,
                        capture=capture,
                        error_code=None if passed else "selection_mismatch",
                    )
                )
    except BaseException:
        internal_failure = True
    finally:
        logging.disable(previous_logging_threshold)
        _ACTIVE_AUDIT = previous_audit

    if internal_failure or len(rows) != len(CASE_IDS):
        return _failure_report("fake", "internal_failure"), audit

    passed = bool(
        all(row["error_code"] is None for row in rows)
        and audit.owner_dispatch_calls == 0
        and audit.network_calls == 0
        and audit.credential_resolution_calls == 0
        and audit.provider_resolution_calls == 0
        and audit.registry_dispatch_calls == 0
        and audit.request_dump_calls == 0
    )
    if not passed and all(row["error_code"] is None for row in rows):
        rows[0] = _case_row(CASE_IDS[0], error_code="selection_mismatch")

    report = {
        "schema": SCHEMA,
        "mode": "fake",
        "provider": "fake",
        "api_mode": "codex_responses",
        "pass": passed,
        "cases": rows,
        "error_code": None if passed else "case_failure",
    }
    return report, audit


def run_fake_smoke(
    agent_builder: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    report, _audit = _run_fake_smoke_with_audit(agent_builder)
    return report


def _run_actual_smoke_with_audit(
    runtime_resolver: Callable[[], _ActualRuntime] | None = None,
    agent_builder: Callable[[Any, _ActualRuntime], Any] | None = None,
) -> tuple[dict[str, Any], _Audit]:
    """Run fixed cases through the actual capture path with injectable test seams."""
    global _ACTIVE_AUDIT

    resolver = runtime_resolver or _resolve_actual_runtime
    builder = agent_builder or _build_actual_agent
    audit = _Audit()
    previous_audit = _ACTIVE_AUDIT
    previous_logging_threshold = logging.root.manager.disable
    _ACTIVE_AUDIT = audit
    logging.disable(logging.CRITICAL)
    rows: list[dict[str, Any]] = []
    provider_failure = False
    internal_failure = False
    auxiliary_client_module: Any = None
    runtime_main_snapshot: tuple[Any, ...] | None = None

    try:
        import agent.auxiliary_client as auxiliary_client
        import agent.conversation_loop as conversation_loop
        import hermes_cli.auth as hermes_auth
        import hermes_cli.middleware as hermes_middleware
        import hermes_cli.plugins as hermes_plugins
        import run_agent
        import tools.registry as tool_registry

        auxiliary_client_module = auxiliary_client
        runtime_main_snapshot = tuple(
            getattr(auxiliary_client, name)
            for name in (
                "_RUNTIME_MAIN_PROVIDER",
                "_RUNTIME_MAIN_MODEL",
                "_RUNTIME_MAIN_BASE_URL",
                "_RUNTIME_MAIN_API_KEY",
                "_RUNTIME_MAIN_API_MODE",
            )
        )

        with ExitStack() as stack:
            devnull = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            stack.enter_context(redirect_stdout(devnull))
            stack.enter_context(redirect_stderr(devnull))
            stack.enter_context(patch.dict(os.environ, {"HERMES_DUMP_REQUESTS": ""}))
            stack.enter_context(
                patch.object(run_agent, "handle_function_call", _production_dispatch_sentinel)
            )
            stack.enter_context(
                patch.object(
                    tool_registry.ToolRegistry,
                    "dispatch",
                    _registry_dispatch_sentinel,
                )
            )
            stack.enter_context(
                patch.object(
                    run_agent.AIAgent,
                    "_dump_api_request_debug",
                    _request_dump_sentinel,
                )
            )
            stack.enter_context(patch.object(hermes_plugins, "has_hook", return_value=False))
            stack.enter_context(patch.object(hermes_plugins, "invoke_hook", return_value=[]))
            stack.enter_context(
                patch.object(
                    hermes_middleware,
                    "apply_llm_request_middleware",
                    _identity_request_middleware,
                )
            )
            stack.enter_context(
                patch.object(
                    hermes_middleware,
                    "run_llm_execution_middleware",
                    _direct_execution_middleware,
                )
            )
            stack.enter_context(
                patch.object(conversation_loop, "jittered_backoff", return_value=0.0)
            )
            stack.enter_context(
                patch.object(
                    conversation_loop,
                    "adaptive_rate_limit_backoff",
                    return_value=0.0,
                )
            )

            try:
                runtime = resolver()
                if type(runtime) is not _ActualRuntime:
                    raise _ActualProviderFailure()
            except BaseException:
                provider_failure = True
            else:
                stack.enter_context(
                    patch.object(
                        hermes_auth,
                        "resolve_codex_runtime_credentials",
                        _credential_resolution_sentinel,
                    )
                )
                stack.enter_context(
                    patch.object(
                        auxiliary_client,
                        "resolve_provider_client",
                        _provider_resolution_sentinel,
                    )
                )
                stack.enter_context(
                    patch.object(
                        auxiliary_client,
                        "get_text_auxiliary_client",
                        _provider_resolution_sentinel,
                    )
                )
                for case_id in CASE_IDS:
                    expected_action = EXPECTED_ACTIONS[case_id]
                    capture_box: list[_Capture] = []
                    result: dict[str, Any] | None = None
                    stopped_on_capture = False
                    failed = False

                    try:
                        agent = builder(case_id, runtime)
                        audit.agents.append(agent)
                        _install_collector(agent, capture_box)
                        _install_actual_provider_guard(agent, case_id, audit)
                        result = agent.run_conversation(CASE_PROMPTS[case_id])
                    except _CaptureStop:
                        stopped_on_capture = True
                    except BaseException:
                        failed = True

                    capture = capture_box[0] if capture_box else _Capture()
                    if expected_action is None:
                        passed = bool(
                            not failed
                            and not stopped_on_capture
                            and capture.call_count == 0
                            and type(result) is dict
                            and result.get("completed") is True
                            and type(result.get("final_response")) is str
                        )
                    else:
                        captured_action = (
                            capture.arguments.get("action") if capture.arguments else None
                        )
                        passed = bool(
                            not failed
                            and stopped_on_capture
                            and capture.valid
                            and capture.call_count == 1
                            and capture.tool_name == TOOL_NAME
                            and captured_action == expected_action
                        )
                    rows.append(
                        _case_row(
                            case_id,
                            capture=capture,
                            error_code=None if passed else "selection_mismatch",
                        )
                    )
    except BaseException:
        internal_failure = True
    finally:
        if auxiliary_client_module is not None and runtime_main_snapshot is not None:
            for name, value in zip(
                (
                    "_RUNTIME_MAIN_PROVIDER",
                    "_RUNTIME_MAIN_MODEL",
                    "_RUNTIME_MAIN_BASE_URL",
                    "_RUNTIME_MAIN_API_KEY",
                    "_RUNTIME_MAIN_API_MODE",
                ),
                runtime_main_snapshot,
            ):
                setattr(auxiliary_client_module, name, value)
        logging.disable(previous_logging_threshold)
        _ACTIVE_AUDIT = previous_audit

    if provider_failure:
        return _failure_report("actual", "provider_failure"), audit
    if internal_failure or len(rows) != len(CASE_IDS):
        return _failure_report("actual", "internal_failure"), audit

    passed = bool(
        all(row["error_code"] is None for row in rows)
        and len(audit.agents) == len(CASE_IDS)
        and len({id(agent) for agent in audit.agents}) == len(CASE_IDS)
        and audit.owner_dispatch_calls == 0
        and audit.registry_dispatch_calls == 0
        and audit.request_dump_calls == 0
        and audit.credential_resolution_calls == 0
        and audit.provider_resolution_calls == 0
        and all(audit.actual_provider_requests.get(case_id) == 1 for case_id in CASE_IDS)
    )
    if not passed and all(row["error_code"] is None for row in rows):
        rows[0] = _case_row(CASE_IDS[0], error_code="selection_mismatch")

    report = {
        "schema": SCHEMA,
        "mode": "actual",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "pass": passed,
        "cases": rows,
        "error_code": None if passed else "case_failure",
    }
    return report, audit


def run_actual_smoke(
    runtime_resolver: Callable[[], _ActualRuntime] | None = None,
    agent_builder: Callable[[Any, _ActualRuntime], Any] | None = None,
) -> dict[str, Any]:
    report, _audit = _run_actual_smoke_with_audit(runtime_resolver, agent_builder)
    return report


def _serialize(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--mode", "fake"], ["--mode", "actual"]):
        print(_serialize(_failure_report("invalid", "invalid_cli")))
        return 2

    mode = "actual" if args == ["--mode", "actual"] else "fake"
    try:
        report = run_actual_smoke() if mode == "actual" else run_fake_smoke()
    except BaseException:
        report = _failure_report(mode, "internal_failure")
    print(_serialize(report))
    return 0 if report["pass"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
