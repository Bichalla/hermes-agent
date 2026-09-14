"""Thin loading shim; maintained implementation remains in the external repository."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def handle(event_type, context):
    if event_type != "gateway:startup":
        return
    from bridge.runtime import require_compatible_runtime
    from bridge.gateway import start
    require_compatible_runtime()
    start(context.get("kanban_approval"), ROOT / ".local/config.json")
