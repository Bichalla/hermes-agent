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

    identity = WorkerIdentity.from_env()
    bridge_config = load_config(ROOT / ".local/config.json")
    socket_path = bridge_config.socket_path
    timeout_seconds = float(bridge_config.max_timeout)

    def present(request):
        return present_request(
            request, identity=identity, socket_path=socket_path, config=bridge_config,
            timeout_seconds=timeout_seconds,
        )

    ctx.register_approval_transport("kanban-owner", present)
