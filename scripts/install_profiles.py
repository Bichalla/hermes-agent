#!/usr/bin/env python3
"""Stage or install the external plugin/hook links; never changes runtime or cards."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

import yaml

ROOT = Path(__file__).resolve().parents[1]


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    worker = home / "profiles/work-executor"
    gateway = home / "profiles/work-pm"
    config_file = worker / "config.yaml"
    raw = config_file.read_bytes()
    config = yaml.safe_load(raw)
    gateway_config = yaml.safe_load((gateway / "config.yaml").read_text())
    discord = gateway_config.get("discord", {})
    owners = discord.get("allow_from", [])
    if (len(owners) != 1 or not str(owners[0]).isdecimal()
            or discord.get("allowed_roles") or discord.get("allow_all_users")):
        raise RuntimeError("work-pm does not have exactly one explicit owner")
    enabled = config.get("plugins", {}).get("enabled", [])
    text = raw.decode()
    if "kanban-owner" not in enabled:
        anchor = "    - runtime-write-guard\n"
        if text.count(anchor) != 1:
            raise RuntimeError("worker plugin configuration changed; review required")
        text = text.replace(anchor, anchor + "    - kanban-owner\n", 1)
    if "security" not in config:
        text = text.rstrip() + "\nsecurity:\n  approval:\n    kanban_transport: kanban-owner\n"
    elif config.get("security", {}).get("approval", {}).get("kanban_transport") != "kanban-owner":
        raise RuntimeError("existing security configuration requires a reviewed merge")
    staged = yaml.safe_load(text)
    expected = yaml.safe_load(raw)
    expected.setdefault("plugins", {}).setdefault("enabled", [])
    if "kanban-owner" not in expected["plugins"]["enabled"]:
        expected["plugins"]["enabled"].append("kanban-owner")
    expected.setdefault("security", {}).setdefault("approval", {})["kanban_transport"] = "kanban-owner"
    if staged != expected:
        raise RuntimeError("staged profile changes exceed the bridge settings")
    links = {
        worker / "plugins/kanban-owner": ROOT / "plugins/kanban-owner",
        gateway / "hooks/kanban-owner": ROOT / "hooks/kanban-owner",
    }
    for target, source in links.items():
        if target.exists() or target.is_symlink():
            if not target.is_symlink() or target.resolve() != source.resolve():
                raise RuntimeError("existing plugin/hook path belongs to another installation")
    local = {
        "db_path": str(home / "role-profiles/kanban/kanban.db"),
        "socket_path": str(home / "ops/approval-bridge/owner.sock"),
        "owner_id": str(owners[0]), "notifier_profile": "work-pm",
        "worker_profile": "work-executor", "max_timeout": 300, "max_pending": 8,
    }
    private_config = ROOT / ".local/config.json"
    if private_config.exists():
        if json.loads(private_config.read_text()) != local:
            raise RuntimeError("existing bridge config differs; review required")
        st = private_config.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeError("existing bridge config is not private")
    summary = {
        "mode": "apply" if args.apply else "stage", "worker_profile": str(worker),
        "gateway_profile": str(gateway), "profile_original_sha256": hashlib.sha256(raw).hexdigest(),
        "profile_staged_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "settings": ["plugins.enabled += kanban-owner", "security.approval.kanban_transport = kanban-owner"],
        "links": {str(k): str(v) for k, v in links.items()}, "owner_pinned": True,
    }
    if args.apply:
        if not private_config.exists():
            private_write(private_config, (json.dumps(local, indent=2) + "\n").encode())
        if raw != text.encode():
            backup = ROOT / ".local/backups" / ("work-executor-" + hashlib.sha256(raw).hexdigest()[:12] + ".yaml")
            if not backup.exists():
                private_write(backup, raw)
            # Replace atomically in the same directory, preserving the profile's mode.
            tmp = config_file.with_name(".config.kanban-owner.tmp")
            private_write(tmp, text.encode())
            os.chmod(tmp, stat.S_IMODE(config_file.stat().st_mode))
            os.replace(tmp, config_file)
        for target, source in links.items():
            if not target.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
