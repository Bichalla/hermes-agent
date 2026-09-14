#!/usr/bin/env python3
"""Validate and optionally prepare a sealed Hermes bridge candidate.

Default mode is a dry-run compatibility check.  `--prepare` builds an inactive
candidate release under the protected releases directory, still leaving manager
finalization and activation to the existing runtime-protection commands.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / ".work/hermes-source"
DEFAULT_MANIFEST = ROOT / "compat/manifest.json"
DEFAULT_RUNTIME_PROTECTION = Path.home() / ".hermes/ops/runtime-protection"
DEFAULT_ACTIVE_CONFIG = DEFAULT_RUNTIME_PROTECTION / "runtime-protection.json"
DEFAULT_RELEASES_ROOT = Path.home() / ".hermes/runtime/protected-releases"
DEFAULT_UV = Path.home() / ".local/bin/uv"

sys.path.insert(0, str(ROOT))

from bridge.compat import CompatError, load_manifest, validate_candidate_source  # noqa: E402


class PrepareCandidateError(RuntimeError):
    """Raised when candidate preparation cannot proceed safely."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--runtime-protection", type=Path, default=DEFAULT_RUNTIME_PROTECTION)
    parser.add_argument("--active-config", type=Path, default=DEFAULT_ACTIVE_CONFIG)
    parser.add_argument("--releases-root", type=Path, default=DEFAULT_RELEASES_ROOT)
    parser.add_argument("--uv", type=Path, default=DEFAULT_UV)
    parser.add_argument("--prepare", action="store_true", help="build and seal an inactive candidate")
    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        prepare_release = _load_prepare_release(args.runtime_protection)
        active_config = _read_json(args.active_config)
        active_source = prepare_release._source_from_config(active_config)
        report = validate_candidate_source(args.source, manifest, active_source=active_source)
        if not args.prepare:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
            return 0
        receipt = prepare_committed_overlay(
            active_config=active_config,
            releases_root=args.releases_root,
            local_source=args.source,
            manifest=manifest,
            uv=args.uv,
            prepare_release=prepare_release,
        )
        result = report.as_dict()
        result.update({"status": "prepared", "receipt": str(receipt)})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Bridge candidate validation stopped: {exc}", file=sys.stderr)
        return 1


def prepare_committed_overlay(
    *,
    active_config: dict[str, Any],
    releases_root: Path,
    local_source: Path,
    manifest: dict[str, Any],
    uv: Path,
    prepare_release,
) -> Path:
    """Build an inactive sealed candidate from a validated committed overlay source."""

    active_source = prepare_release._source_from_config(active_config)
    report = validate_candidate_source(local_source, manifest, active_source=active_source)
    root = prepare_release._check_roots(active_source, releases_root)
    dirty = prepare_release._dirty_snapshot(active_source)
    base_python = prepare_release._base_python(active_config)
    local_source = local_source.resolve()
    ref = report.head
    candidate_root = prepare_release._unique_candidate_root(root, ref)
    prepare_release._check_candidate_root(active_source, candidate_root)
    candidate_source = candidate_root / "source"
    receipt_path = candidate_root / "candidate-receipt.json"

    try:
        official_origin = prepare_release._git_stdout(active_source, "remote", "get-url", "origin").strip()
        if not official_origin:
            raise PrepareCandidateError("active source must have an origin remote")
        prepare_release._clone_candidate(active_source, candidate_source, official_origin)
        git_info = prepare_release._checkout_ref(candidate_source, active_source, str(local_source), ref)
        prepare_release._build_env(candidate_source, uv, base_python)
        checks = prepare_release._check_candidate(candidate_source, uv)
        prepare_release._verify_no_hardlinks(candidate_source)

        venv_manifest_path = candidate_root / "venv-manifest.json"
        source_manifest_path = candidate_root / "source-manifest.json"
        candidate_config_path = candidate_root / "runtime-protection.candidate.json"
        prepare_release._write_json(venv_manifest_path, prepare_release._venv_manifest(candidate_source))
        prepare_release._write_json(source_manifest_path, prepare_release._source_manifest(candidate_source))

        candidate_config = dict(active_config)
        candidate_config["venv"] = str(candidate_source / ".venv")
        candidate_config["manifest"] = str(venv_manifest_path)
        candidate_config["source_manifest"] = str(source_manifest_path)
        candidate_config["python_version"] = prepare_release.PYTHON_VERSION
        policy_path = Path(str(candidate_config.get("policy", "")))
        if policy_path.is_absolute() and policy_path.exists() and policy_path.is_file() and not policy_path.is_symlink():
            candidate_config["policy_sha256"] = prepare_release._sha256(policy_path)
        prepare_release._write_json(candidate_config_path, candidate_config)

        receipt = {
            "schema_version": prepare_release.SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "prepared",
            "requested_ref": ref,
            "candidate_root": str(candidate_root),
            "candidate_source": str(candidate_source),
            "candidate_config": str(candidate_config_path),
            "venv_manifest": str(venv_manifest_path),
            "source_manifest": str(source_manifest_path),
            "dirty_snapshot": dirty,
            "git": git_info,
            "checks": checks,
            "bridge_compat": report.as_dict(),
            "review_hashes": prepare_release._review_hashes(candidate_source),
            "uv": str(uv),
            "base_python": str(base_python),
            "next_step": "Run the existing runtime-protection manager finalize path; activation remains a separate operator action.",
        }
        prepare_release._write_json(receipt_path, receipt)
        prepare_release._seal_tree(candidate_source)
        return receipt_path
    except Exception as exc:
        if candidate_root.exists():
            candidate_root.mkdir(parents=True, exist_ok=True)
            prepare_release._write_json(receipt_path, {
                "schema_version": prepare_release.SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "requested_ref": ref,
                "candidate_root": str(candidate_root),
                "candidate_source": str(candidate_source),
                "error": str(exc),
            })
        raise

def _load_prepare_release(runtime_protection: Path):
    module_path = runtime_protection / "prepare_release.py"
    spec = importlib.util.spec_from_file_location("hermes_bridge_prepare_release", module_path)
    if spec is None or spec.loader is None:
        raise PrepareCandidateError(f"cannot load prepare_release.py from {runtime_protection}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
