"""Refuse to load the custom transport against an unreviewed Hermes runtime."""
from pathlib import Path

from .compat import load_manifest, validate_candidate_source


def require_compatible_runtime() -> None:
    from tools import approval
    source = Path(approval.__file__).resolve().parents[1]
    manifest = Path(__file__).resolve().parents[1] / "compat/manifest.json"
    try:
        validate_candidate_source(source, load_manifest(manifest))
    except Exception:
        raise RuntimeError("Kanban approval bridge/runtime mismatch; compatibility review required") from None
