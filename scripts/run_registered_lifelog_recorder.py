#!/usr/bin/env python3
"""Production narrow route for registered local Lifelog recorders."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_LIFELOG_ROOT = Path.home() / ".hermes" / "ops" / "state" / "lifelog"


def _load_dispatcher(root: Path):
    path = root / "scripts" / "run_registered_recorder.py"
    spec = importlib.util.spec_from_file_location("registered_lifelog_dispatcher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("registered Lifelog dispatcher is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_registered_lifelog_recorder(
    recorder_id: str,
    payload: Path,
    *,
    dry_run: bool,
    root: Path = DEFAULT_LIFELOG_ROOT,
) -> dict[str, Any]:
    """Call the fail-closed dispatcher through the production Hermes route."""
    dispatcher = _load_dispatcher(root)
    return dispatcher.run_registered_recorder(
        recorder_id,
        payload,
        dry_run=dry_run,
        root=root,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recorder_id")
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--dry-run", choices=("true", "false"), required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("HERMES_CURRENT_USER_ACTION_FINGERPRINT"):
        parser.error("registered recording requires current-turn user authority")
    result = run_registered_lifelog_recorder(
        args.recorder_id,
        args.payload,
        dry_run=args.dry_run == "true",
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
