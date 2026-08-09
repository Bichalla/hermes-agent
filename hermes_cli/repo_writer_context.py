"""Immutable process-local restrictions for authenticated repository writers.

The environment marker is captured exactly once when this lightweight module is
imported, before dotenv hydration.  It grants no authority: an exact marker only
narrows the process tool surface for its lifetime.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any


REPO_WRITER_CONTEXT_ENV = "HERMES_KANBAN_REPO_WRITER"
REPO_WRITER_BLOCKED_TOOL_NAMES = frozenset({"execute_code", "computer_use"})

# Intentionally no production setter/reset. Tests may monkeypatch this private
# snapshot directly, then restore it through monkeypatch teardown.
_REPO_WRITER_CONTEXT = os.environ.get(REPO_WRITER_CONTEXT_ENV) == "1"


def is_repo_writer_context() -> bool:
    """Return the immutable import-time repository-writer restriction bit."""
    return _REPO_WRITER_CONTEXT


def is_repo_writer_blocked_tool(name: object) -> bool:
    """Return whether *name* is an exact blocked name in writer context."""
    return (
        _REPO_WRITER_CONTEXT
        and isinstance(name, str)
        and name in REPO_WRITER_BLOCKED_TOOL_NAMES
    )


def _emitted_function_name(definition: object) -> str | None:
    if not isinstance(definition, dict):
        return None
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else None


def filter_repo_writer_tool_definitions(definitions: Any):
    """Remove malformed or blocked final emitted schemas in writer context.

    The input is never mutated. Outside writer context the original object is
    returned so existing list identity and semantics remain unchanged.
    """
    if not _REPO_WRITER_CONTEXT:
        return definitions
    if definitions is None:
        return []
    try:
        candidates = list(definitions)
    except (TypeError, ValueError):
        return []
    return [
        definition
        for definition in candidates
        if (name := _emitted_function_name(definition)) is not None
        and name not in REPO_WRITER_BLOCKED_TOOL_NAMES
    ]


def filter_repo_writer_tool_names(names: Any):
    """Remove malformed and exact blocked names without mutating *names*."""
    if not _REPO_WRITER_CONTEXT:
        return names
    if names is None:
        return set()
    try:
        filtered = [
            name
            for name in names
            if isinstance(name, str)
            and name
            and name not in REPO_WRITER_BLOCKED_TOOL_NAMES
        ]
    except (TypeError, ValueError):
        return set()
    if isinstance(names, frozenset):
        return frozenset(filtered)
    if isinstance(names, set):
        return set(filtered)
    if isinstance(names, tuple):
        return tuple(filtered)
    if isinstance(names, list):
        return filtered
    return set(filtered)


def filter_repo_writer_tool_surface(
    definitions: Any,
    context_engine_tool_names: Iterable[object] | None = None,
) -> tuple[Any, set[str], set[str]]:
    """Return one coherent staged ``(definitions, names, engine_names)`` tuple."""
    if not _REPO_WRITER_CONTEXT:
        names = {
            name
            for definition in definitions or []
            if (name := _emitted_function_name(definition)) is not None
        }
        engine_names = {
            name
            for name in (context_engine_tool_names or ())
            if isinstance(name, str)
        }
        return definitions, names, engine_names

    filtered = filter_repo_writer_tool_definitions(definitions)
    names = {
        name
        for definition in filtered
        if (name := _emitted_function_name(definition)) is not None
    }
    engine_names = {
        name
        for name in (context_engine_tool_names or ())
        if isinstance(name, str) and name in names
    }
    return filtered, names, engine_names


def filter_agent_tool_surface(agent: object) -> object:
    """Coherently final-filter an already-built fake or real agent surface."""
    if not _REPO_WRITER_CONTEXT:
        return agent

    definitions, names, engine_names = filter_repo_writer_tool_surface(
        getattr(agent, "tools", None),
        getattr(agent, "_context_engine_tool_names", None),
    )
    setattr(agent, "tools", definitions)
    setattr(agent, "valid_tool_names", names)

    current_engine_names = getattr(agent, "_context_engine_tool_names", None)
    if isinstance(current_engine_names, set):
        current_engine_names.clear()
        current_engine_names.update(engine_names)
    elif current_engine_names is not None:
        setattr(agent, "_context_engine_tool_names", engine_names)
    return agent
