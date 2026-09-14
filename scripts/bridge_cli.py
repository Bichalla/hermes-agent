#!/usr/bin/env python3
"""Inspect the installed bridge without issuing an approval or changing card state."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".hermes"
RUNTIME_CONFIG = HOME / "ops/runtime-protection/runtime-protection.json"


def main():
    active = json.loads(RUNTIME_CONFIG.read_text())
    interpreter = Path(active["venv"]) / "bin/python"
    if Path(sys.prefix).resolve() != Path(active["venv"]).resolve():
        os.execv(str(interpreter), [str(interpreter), "-B", str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(ROOT))
    from bridge.compat import load_manifest, validate_candidate_source
    from bridge.gateway import load_config
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    check = sub.add_parser("check", help="refuse an incompatible candidate before an update")
    check.add_argument("source", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(ROOT / "compat/manifest.json")
    if args.command == "check":
        print(json.dumps(validate_candidate_source(args.source, manifest).as_dict(), indent=2))
        return 0

    result = {}
    try:
        report = validate_candidate_source(Path(active["venv"]).parent, manifest)
        result["runtime_compatible"] = True
        result["runtime_commit"] = report.head
    except Exception:
        result["runtime_compatible"] = False
    try:
        config = load_config(ROOT / ".local/config.json")
        result["private_config"] = True
    except Exception:
        config = None
        result["private_config"] = False
    worker = HOME / "profiles/work-executor"
    gateway = HOME / "profiles/work-pm"
    result["plugin_link"] = (worker / "plugins/kanban-owner").resolve() == ROOT / "plugins/kanban-owner"
    result["hook_link"] = (gateway / "hooks/kanban-owner").resolve() == ROOT / "hooks/kanban-owner"
    cfg = yaml.safe_load((worker / "config.yaml").read_text())
    result["worker_configured"] = (
        "kanban-owner" in cfg.get("plugins", {}).get("enabled", [])
        and cfg.get("security", {}).get("approval", {}).get("kanban_transport") == "kanban-owner"
    )
    try:
        state = json.loads((gateway / "gateway_state.json").read_text())
        os.kill(int(state["pid"]), 0)
        result["gateway_on_candidate"] = (
            state.get("gateway_state") == "running"
            and state.get("code_sha") == result.get("runtime_commit")
        )
    except (OSError, ValueError, KeyError):
        result["gateway_on_candidate"] = False
    result["broker_listening"] = False
    if config:
        try:
            path = Path(config.socket_path)
            st = path.lstat()
            private = stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
            parent = path.parent.stat()
            private = private and parent.st_uid == os.getuid() and not parent.st_mode & 0o077
            if private:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.5)
                    client.connect(config.socket_path)
                result["broker_listening"] = True
        except OSError:
            pass
    keys = ("runtime_compatible", "private_config", "plugin_link", "hook_link", "worker_configured", "gateway_on_candidate", "broker_listening")
    result["ready"] = all(result[key] for key in keys)
    result["human_roundtrip_verified"] = False
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
