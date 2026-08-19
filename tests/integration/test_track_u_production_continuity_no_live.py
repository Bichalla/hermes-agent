"""No-live production-continuity import and edge-contract smokes."""

from __future__ import annotations

import importlib

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
