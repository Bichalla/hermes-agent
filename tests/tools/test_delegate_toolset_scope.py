"""Tests for delegate_tool toolset scoping.

Verifies that subagents cannot gain tools that the parent does not have.
The LLM controls the `toolsets` parameter — without intersection with the
parent's enabled_toolsets, it can escalate privileges by requesting
arbitrary toolsets.
"""

from types import SimpleNamespace

import model_tools
import run_agent
from hermes_cli import repo_writer_context

from toolsets import TOOLSETS, resolve_toolset
import tools.delegate_tool as delegate_tool
from tools.delegate_tool import (
    DELEGATE_BLOCKED_TOOLS,
    _build_child_agent,
    _emit_parent_console,
    _strip_blocked_tools,
)


REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING = (
    "repo_writer_tool_capability_contract_missing"
)
_REPO_WRITER_ENV = "HERMES_KANBAN_REPO_WRITER"
_WRITER_CHILD_BLOCKED_TOOLS = {"computer_use", "execute_code"}


def _capture_child_toolsets(monkeypatch, parent, *, requested=None, role="leaf"):
    captured = {}

    def fake_agent(**kwargs):
        captured["enabled_toolsets"] = list(kwargs["enabled_toolsets"])
        return SimpleNamespace(session_id="", _session_init_model_config={})

    monkeypatch.setattr(run_agent, "AIAgent", fake_agent)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(
        delegate_tool, "_resolve_child_credential_pool", lambda *_args: None
    )

    _build_child_agent(
        task_index=0,
        goal="writer child scope",
        context=None,
        toolsets=requested,
        model=None,
        max_iterations=1,
        task_count=1,
        parent_agent=parent,
        role=role,
    )
    return captured["enabled_toolsets"]


def _parent(*, enabled_toolsets, valid_tool_names=()):
    return SimpleNamespace(
        enabled_toolsets=enabled_toolsets,
        valid_tool_names=list(valid_tool_names),
        model="test-model",
        base_url="https://example.invalid/v1",
        provider="test-provider",
        api_key="test-key",
        _client_kwargs={},
        _delegate_depth=0,
        _active_children=[],
    )


def _resolved_child_tools(toolsets):
    return {
        tool_name
        for toolset_name in toolsets
        for tool_name in resolve_toolset(toolset_name)
    }


class TestToolsetIntersection:
    """Subagent toolsets must be a subset of parent's enabled_toolsets."""

    def test_requested_toolsets_intersected_with_parent(self):
        """LLM requests toolsets parent doesn't have — extras are dropped."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file"])

        # Simulate the intersection logic from _build_child_agent
        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "file", "web", "browser", "rl"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["file", "terminal"]
        assert "web" not in scoped
        assert "browser" not in scoped
        assert "rl" not in scoped

    def test_all_requested_toolsets_available_on_parent(self):
        """LLM requests subset of parent tools — all pass through."""
        parent = SimpleNamespace(enabled_toolsets=["terminal", "file", "web", "browser"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["terminal", "web"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert sorted(scoped) == ["terminal", "web"]

    def test_no_toolsets_requested_inherits_parent(self):
        """When toolsets is None/empty, child inherits parent's set."""
        parent_toolsets = ["terminal", "file", "web"]
        child = _strip_blocked_tools(parent_toolsets)
        assert "terminal" in child
        assert "file" in child
        assert "web" in child

    def test_strip_blocked_removes_delegation(self):
        """Blocked toolsets (delegation, clarify, etc.) are always removed."""
        child = _strip_blocked_tools(["terminal", "delegation", "clarify", "memory"])
        assert "delegation" not in child
        assert "clarify" not in child
        assert "memory" not in child
        assert "terminal" in child

    def test_strip_blocked_removes_review_ledger_controller(self):
        child = _strip_blocked_tools(
            ["terminal", "review-ledger-controller"]
        )
        assert child == ["terminal"]

    def test_alias_and_mixed_composite_cannot_expose_review_ledger(self):
        assert _strip_blocked_tools(["terminal", "all", "*"]) == ["terminal"]
        TOOLSETS["mixed-review-ledger-test"] = {
            "description": "test-only mixed composite",
            "tools": ["terminal", "registered_review_ledger"],
            "includes": [],
        }
        try:
            assert _strip_blocked_tools(
                ["terminal", "mixed-review-ledger-test"]
            ) == ["terminal"]
        finally:
            TOOLSETS.pop("mixed-review-ledger-test", None)

    def test_toolset_reconstruction_cannot_restore_registered_local_workflow(self):
        assert "registered_local_workflow" in DELEGATE_BLOCKED_TOOLS
        parent_tool_names = ["terminal", "registered_local_workflow"]
        reconstructed = sorted(
            {
                toolset
                for tool_name in parent_tool_names
                if (toolset := model_tools.get_toolset_for_tool(tool_name)) is not None
            }
        )
        assert "registered-workflow" in reconstructed
        child_toolsets = _strip_blocked_tools(reconstructed)
        assert child_toolsets == ["terminal"]
        assert all(
            "registered_local_workflow" not in set(TOOLSETS[name].get("tools", []))
            for name in child_toolsets
        )

    def test_empty_intersection_yields_empty_toolsets(self):
        """If parent has no overlap with requested, child gets nothing extra."""
        parent = SimpleNamespace(enabled_toolsets=["terminal"])

        parent_toolsets = set(parent.enabled_toolsets)
        requested = ["web", "browser"]
        scoped = [t for t in requested if t in parent_toolsets]

        assert scoped == []

    def test_writer_context_strips_direct_alias_and_mixed_computer_use(
        self, monkeypatch
    ):
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        TOOLSETS["mixed-writer-capability-test"] = {
            "description": "test-only mixed writer capability composite",
            "tools": ["terminal", "computer_use"],
            "includes": [],
        }
        try:
            child = _strip_blocked_tools(
                [
                    "terminal",
                    "computer_use",
                    "code_execution",
                    "hermes-cli",
                    "all",
                    "*",
                    "mixed-writer-capability-test",
                ]
            )
        finally:
            TOOLSETS.pop("mixed-writer-capability-test", None)

        assert child == ["terminal"], (
            REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
        )

    def test_writer_requested_custom_composite_cannot_reach_child(
        self, monkeypatch
    ):
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        TOOLSETS["requested-writer-capability-test"] = {
            "description": "test-only requested writer capability composite",
            "tools": ["terminal", "computer_use"],
            "includes": [],
        }
        try:
            child_toolsets = _capture_child_toolsets(
                monkeypatch,
                _parent(enabled_toolsets=["requested-writer-capability-test"]),
                requested=["requested-writer-capability-test"],
            )
        finally:
            TOOLSETS.pop("requested-writer-capability-test", None)

        assert child_toolsets == [], (
            REPO_WRITER_TOOL_CAPABILITY_CONTRACT_MISSING
        )

    def test_writer_inherited_toolsets_cannot_reach_child(self, monkeypatch):
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        child_toolsets = _capture_child_toolsets(
            monkeypatch,
            _parent(
                enabled_toolsets=["terminal", "computer_use", "code_execution"]
            ),
        )

        assert child_toolsets == ["terminal"]
        assert not (_resolved_child_tools(child_toolsets) & _WRITER_CHILD_BLOCKED_TOOLS)

    def test_writer_valid_tool_name_reconstruction_cannot_reach_child(
        self, monkeypatch
    ):
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        child_toolsets = _capture_child_toolsets(
            monkeypatch,
            _parent(
                enabled_toolsets=None,
                valid_tool_names=["terminal", "computer_use", "execute_code"],
            ),
        )

        assert child_toolsets == ["terminal"]
        assert not (_resolved_child_tools(child_toolsets) & _WRITER_CHILD_BLOCKED_TOOLS)

    def test_writer_orchestrator_readd_cannot_restore_blocked_tools(
        self, monkeypatch
    ):
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", True)
        child_toolsets = _capture_child_toolsets(
            monkeypatch,
            _parent(
                enabled_toolsets=["terminal", "computer_use", "code_execution"]
            ),
            role="orchestrator",
        )

        assert "delegation" in child_toolsets
        assert not (_resolved_child_tools(child_toolsets) & _WRITER_CHILD_BLOCKED_TOOLS)

    def test_normal_context_preserves_computer_use_inheritance(self, monkeypatch):
        monkeypatch.delenv(_REPO_WRITER_ENV, raising=False)
        monkeypatch.setattr(repo_writer_context, "_REPO_WRITER_CONTEXT", False)
        child_toolsets = _capture_child_toolsets(
            monkeypatch,
            _parent(enabled_toolsets=["terminal", "computer_use"]),
        )

        assert child_toolsets == ["terminal", "computer_use"]
        assert "computer_use" in _resolved_child_tools(child_toolsets)


class TestEmitParentConsole:
    """Progress lines (e.g. ``✓ [N/M] …``) must route through the parent's
    configured ``_safe_print`` in headless stdio hosts (ACP, gateway) so
    they don't land on stdout and corrupt JSON-RPC frames. Regression for a
    bug where delegate_task completion lines pushed to stdout caused
    ``Failed to parse JSON message: ✓ [3/3] …`` errors in the ACP adapter."""

    def test_routes_through_parent_safe_print_when_available(self, capsys):
        captured_lines = []
        parent = SimpleNamespace(_safe_print=lambda line: captured_lines.append(line))

        _emit_parent_console(parent, "  ✓ [1/3] Research done  (11.55s)")

        assert captured_lines == ["  ✓ [1/3] Research done  (11.55s)"]
        stdout_stderr = capsys.readouterr()
        assert stdout_stderr.out == ""
        assert stdout_stderr.err == ""

    def test_falls_back_to_stdout_when_no_safe_print(self, capsys):
        parent = SimpleNamespace()
        _emit_parent_console(parent, "  ✓ [1/3] fallback path")
        captured = capsys.readouterr()
        assert "fallback path" in captured.out

    def test_falls_back_to_stdout_when_safe_print_raises(self, capsys):
        def raiser(_line):
            raise RuntimeError("boom")

        parent = SimpleNamespace(_safe_print=raiser)
        _emit_parent_console(parent, "  ✓ [2/3] fallback on exception")
        captured = capsys.readouterr()
        assert "fallback on exception" in captured.out

    def test_non_callable_safe_print_is_ignored(self, capsys):
        """Defensive: if _safe_print is set but not callable, fall back."""
        parent = SimpleNamespace(_safe_print="not-a-function")
        _emit_parent_console(parent, "  ✓ [3/3] non-callable guard")
        captured = capsys.readouterr()
        assert "non-callable guard" in captured.out
