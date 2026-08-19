"""No-live production-continuity import and edge-contract smokes."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize(
    "module_name",
    (
        "agent.conversation_loop",
        "agent.conversation_compression",
        "agent.memory_provider",
        "gateway.session_context",
        "hermes_cli.kanban_db",
        "hermes_cli.plugins",
        "tools.workflow_authority",
        "tools.registered_local_workflow",
    ),
)
def test_core_startup_session_memory_compression_plugin_imports(module_name: str) -> None:
    assert importlib.import_module(module_name)


@pytest.mark.parametrize(
    "module_name",
    (
        "cron.scheduler",
        "tools.cronjob_tools",
        "hermes_cli.backup",
        "hermes_cli.subcommands.backup",
    ),
)
def test_backup_and_cron_edges_import_without_live_effects(module_name: str) -> None:
    assert importlib.import_module(module_name)


_EXTERNAL_BROKER_ROOT = Path.home() / ".hermes" / "plugins" / "lifelog-context-broker"


@pytest.mark.skipif(
    not (_EXTERNAL_BROKER_ROOT / "__init__.py").is_file(),
    reason="external lifelog-context-broker source is not installed",
)
def test_actual_impact_v3_broker_registers_and_closes_without_live_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load the real external plugin against candidate host APIs, no DB/provider."""

    package_name = "track_u_v3_lifelog_context_broker"
    for loaded_name in tuple(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(package_name + "."):
            sys.modules.pop(loaded_name, None)
    spec = importlib.util.spec_from_file_location(
        package_name,
        _EXTERNAL_BROKER_ROOT / "__init__.py",
        submodule_search_locations=[str(_EXTERNAL_BROKER_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = plugin
    spec.loader.exec_module(plugin)

    policy_module = importlib.import_module(f"{package_name}.infrastructure.policy_config")
    monkeypatch.setattr(
        policy_module,
        "load_policy_config",
        lambda: SimpleNamespace(impact_v3_enabled=True),
    )
    execution_calls: list[dict[str, Any]] = []

    def no_live_hook(_ctx: Any, _policy: Any, **_kwargs: Any):
        def execute(**kwargs: Any) -> dict[str, str]:
            execution_calls.append(kwargs)
            raise AssertionError("impact-v3 execution crossed the no-live boundary")

        return execute

    monkeypatch.setattr(plugin, "_make_v3_hook", no_live_hook)

    class Context:
        def __init__(self) -> None:
            self.hooks: list[tuple[str, Any]] = []
            self.guards: list[tuple[str, Any]] = []
            self.tools: list[dict[str, Any]] = []

        def register_hook(self, name: str, callback: Any) -> None:
            self.hooks.append((name, callback))

        def register_terminal_output_guard(self, guard_id: str, callback: Any) -> None:
            self.guards.append((guard_id, callback))

        def register_tool(self, **kwargs: Any) -> None:
            self.tools.append(kwargs)

    from tools.workflow_authority import clear_current_turn_user_authority

    clear_current_turn_user_authority()
    context = Context()
    plugin.register(context)
    assert [item["name"] for item in context.tools] == ["lifelog_context_read"]
    assert context.tools[0]["toolset"] == "lifelog_context_broker"

    closed = json.loads(context.tools[0]["handler"]({"query": "synthetic current context"}))
    assert closed == {
        "schema": "lifelog-context-read-result/v1",
        "status": "unavailable",
        "error": "authority_unavailable",
    }
    assert execution_calls == []
