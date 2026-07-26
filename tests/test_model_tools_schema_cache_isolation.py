"""Regression coverage for model-facing tool-schema cache isolation."""

from __future__ import annotations

import model_tools
import pytest
from agent.codex_responses_adapter import _responses_tools
from tools.registered_local_workflow import REGISTERED_LOCAL_WORKFLOW_SCHEMA
from tools.schema_sanitizer import sanitize_tool_schemas


@pytest.fixture(autouse=True)
def _isolate_tool_definition_cache():
    model_tools._clear_tool_defs_cache()
    yield
    model_tools._clear_tool_defs_cache()


def test_quiet_tool_definition_cache_isolates_nested_schema_mutation(monkeypatch):
    schema = [
        {
            "type": "function",
            "function": {
                "name": "registered_local_workflow",
                "description": "registered workflow",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["childcare_event_record"],
                        },
                        "payload_name": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    calls = 0

    def fake_compute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return schema

    model_tools._clear_tool_defs_cache()
    monkeypatch.setattr(model_tools, "_compute_tool_definitions", fake_compute)

    first = model_tools.get_tool_definitions(
        enabled_toolsets=["registered-workflow"], quiet_mode=True
    )
    first[0]["function"]["parameters"]["properties"].clear()

    second = model_tools.get_tool_definitions(
        enabled_toolsets=["registered-workflow"], quiet_mode=True
    )

    assert calls == 1
    assert second[0]["function"]["parameters"]["properties"]["action"] == {
        "type": "string",
        "enum": ["childcare_event_record"],
    }

    second[0]["function"]["parameters"]["properties"]["action"]["enum"].append(
        "cache_hit_poison"
    )
    third = model_tools.get_tool_definitions(
        enabled_toolsets=["registered-workflow"], quiet_mode=True
    )

    assert calls == 1
    assert third[0]["function"]["parameters"]["properties"]["action"] == {
        "type": "string",
        "enum": ["childcare_event_record"],
    }


def test_registered_workflow_schema_survives_cache_and_codex_conversion(monkeypatch):
    tool = {
        "type": "function",
        "function": REGISTERED_LOCAL_WORKFLOW_SCHEMA,
    }

    model_tools._clear_tool_defs_cache()
    monkeypatch.setattr(
        model_tools,
        "_compute_tool_definitions",
        lambda *_args, **_kwargs: sanitize_tool_schemas([tool]),
    )

    first = model_tools.get_tool_definitions(
        enabled_toolsets=["registered-workflow"], quiet_mode=True
    )
    first[0]["function"]["parameters"]["properties"].clear()
    cached = model_tools.get_tool_definitions(
        enabled_toolsets=["registered-workflow"], quiet_mode=True
    )
    converted = _responses_tools(cached)
    assert converted is not None
    responses_schema = converted[0]["parameters"]

    assert responses_schema["required"] == ["action"]
    assert "childcare_event_record" in responses_schema["properties"]["action"]["enum"]
    assert "payload_name" in responses_schema["properties"]
