#!/usr/bin/env python3
"""Read-only, raw-minimized audit of Hermes permanent command approvals."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "approval-allowlist-audit/v1"
RISK_CLASSES = (
    "hardline-unbypassable",
    "broad-destructive",
    "broad-interpreter-shell",
    "workflow-specific-candidate",
    "obsolete-unknown",
)

_HARDLINE_NAMES = {
    "recursive delete of root filesystem",
    "recursive delete of system directory",
    "recursive delete of home directory",
    "format filesystem (mkfs)",
    "dd to raw block device",
    "redirect to raw block device",
    "fork bomb",
    "kill all processes",
    "system shutdown/reboot",
    "init 0/6 (shutdown/reboot)",
    "systemctl poweroff/reboot",
    "telinit 0/6 (shutdown/reboot)",
}
_DESTRUCTIVE_RE = re.compile(
    r"(?:\brm\b|recursive delete|delete in root path|overwrite system file|\bDROP\b|\bTRUNCATE\b|"
    r"force push|reset --hard|stop/restart (?:system service|hermes gateway)|"
    r"gateway (?:stop|restart)|docker (?:stop|restart|kill)|shutdown|reboot|mkfs|/dev/(?:sd|nvme))",
    re.IGNORECASE,
)
_INTERPRETER_RE = re.compile(
    r"(?:\b(?:python[23]?|bash|sh|zsh|ksh|perl|ruby|node)\b|shell command via|"
    r"script execution via|curl.*\||wget.*\||execute_code|heredoc)",
    re.IGNORECASE,
)
_WORKFLOW_RE = re.compile(
    r"(?:hermes\s+kanban\s+(?:comment|show|create)|run_registered_recorder\.py)",
    re.IGNORECASE,
)
_PUBLIC_SAFE_ALIASES = {
    name: f"hardline:{index:02d}"
    for index, name in enumerate(sorted(_HARDLINE_NAMES), 1)
}
_SECRET_MARKER_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|bearer|credential)=?",
    re.IGNORECASE,
)


def classify_pattern(pattern: str) -> str:
    normalized = pattern.strip()
    if normalized in _HARDLINE_NAMES:
        return "hardline-unbypassable"
    if _DESTRUCTIVE_RE.search(normalized):
        return "broad-destructive"
    if _INTERPRETER_RE.search(normalized):
        return "broad-interpreter-shell"
    if _WORKFLOW_RE.search(normalized):
        return "workflow-specific-candidate"
    return "obsolete-unknown"


def safe_pattern_name(pattern: str, index: int) -> str:
    normalized = pattern.strip()
    if _SECRET_MARKER_RE.search(normalized):
        return f"entry_{index:04d}"
    if normalized in _PUBLIC_SAFE_ALIASES:
        return _PUBLIC_SAFE_ALIASES[normalized]
    if _WORKFLOW_RE.search(normalized):
        if "kanban" in normalized.lower():
            return "workflow:kanban"
        return "workflow:registered-recorder"
    if _INTERPRETER_RE.search(normalized):
        return "interpreter:broad-pattern"
    if _DESTRUCTIVE_RE.search(normalized):
        return "destructive:broad-pattern"
    return f"entry_{index:04d}"


def build_audit(patterns: list[Any]) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    for index, value in enumerate(patterns, 1):
        if not isinstance(value, str) or not value.strip():
            risk_class = "obsolete-unknown"
            name = f"entry_{index:04d}"
        else:
            risk_class = classify_pattern(value)
            name = safe_pattern_name(value, index)
        entries.append({"pattern_name": name, "risk_class": risk_class})

    counts = Counter(entry["risk_class"] for entry in entries)
    migration_entries = [
        {
            "pattern_name": entry["pattern_name"],
            "risk_class": entry["risk_class"],
            "proposal": (
                "remove"
                if entry["risk_class"] in {
                    "hardline-unbypassable",
                    "broad-destructive",
                    "broad-interpreter-shell",
                    "obsolete-unknown",
                }
                else "review-for-exact-cwd-executable-argv-target-policy"
            ),
        }
        for entry in entries
    ]
    return {
        "schema": SCHEMA,
        "read_only": True,
        "config_mutated": False,
        "entries": entries,
        "migration_preview": {
            "schema": "approval-allowlist-migration-preview/v1",
            "apply_supported": False,
            "entries": migration_entries,
        },
        "summary": {risk_class: counts.get(risk_class, 0) for risk_class in RISK_CLASSES},
    }


def _load_config(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - Hermes runtime includes PyYAML
            raise RuntimeError("PyYAML is required to read config.yaml") from exc
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        config_path = args.config
        if config_path is None:
            from hermes_constants import get_hermes_home

            config_path = get_hermes_home() / "config.yaml"
        config = _load_config(config_path)
        patterns = config.get("command_allowlist", [])
        if not isinstance(patterns, list):
            raise ValueError("command_allowlist must be a list")
        result = build_audit(patterns)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=None if args.json else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
