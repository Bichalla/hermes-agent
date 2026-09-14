"""Hermes plugin entry point for the Kanban owner approval transport."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.gateway import load_config
from bridge.worker import WorkerIdentity, present_request


def register(ctx) -> None:
    from bridge.runtime import require_compatible_runtime
    require_compatible_runtime()

    bridge_config = load_config(ROOT / ".local/config.json")
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    identity = WorkerIdentity.from_env()
    if home.name == bridge_config.notifier_profile:
        from bridge.pm import bind_reviewer
        bind_reviewer(ctx.llm, home)
        if identity is None:
            return  # Gateway reviewer; a Kanban work-pm worker also needs the transport.
    from bridge.execution import ExecutionBindings
    bindings = ExecutionBindings()
    if identity is not None:
        bindings.register(ctx)
    socket_path = bridge_config.socket_path
    timeout_seconds = float(bridge_config.max_timeout)

    def present(request):
        return present_request(
            request, identity=identity, socket_path=socket_path, config=bridge_config,
            timeout_seconds=timeout_seconds,
            bindings=bindings,
        )

    ctx.register_approval_transport("kanban-owner", present)
    from bridge.worker_tools import register_tools
    register_tools(ctx, identity, bridge_config, ROOT)
