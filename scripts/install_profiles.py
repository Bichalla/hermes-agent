#!/usr/bin/env python3
"""Stage/install all work-role bridge settings without changing runtime or cards."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.broker import BridgeConfig
from bridge.profiles import discover_worker_profiles, stage_profile


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)


def replace_backed_up(path, original, staged, label):
    if original == staged:
        return
    if original is not None:
        backup = ROOT / '.local/backups' / (label + '-' + hashlib.sha256(original).hexdigest()[:12] + path.suffix)
        if not backup.exists():
            private_write(backup, original)
        if path.read_bytes() != original:
            raise RuntimeError('configuration changed after staging')
    temp = path.with_name('.' + path.name + '.kanban-owner.tmp')
    private_write(temp, staged)
    os.chmod(temp, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    roles = discover_worker_profiles(home)
    if 'work-pm' not in roles:
        raise RuntimeError('work-pm profile is not installed')
    gateway = home / 'profiles/work-pm'
    gateway_config = yaml.safe_load((gateway / 'config.yaml').read_bytes())
    discord = gateway_config.get('discord', {})
    owners = discord.get('allow_from', [])
    if (len(owners) != 1 or not str(owners[0]).isdecimal()
            or discord.get('allowed_roles') or discord.get('allow_all_users')):
        raise RuntimeError('work-pm does not have exactly one explicit owner')
    staged, links = {}, {gateway / 'hooks/kanban-owner': ROOT / 'hooks/kanban-owner'}
    for role in roles:
        profile = home / 'profiles' / role
        path = profile / 'config.yaml'
        original = path.read_bytes()
        staged[path] = (original, stage_profile(original.decode()).encode(), role)
        links[profile / 'plugins/kanban-owner'] = ROOT / 'plugins/kanban-owner'
    for target, source in links.items():
        if target.exists() or target.is_symlink():
            if not target.is_symlink() or target.resolve() != source.resolve():
                raise RuntimeError('existing plugin/hook path belongs to another installation')
    private_config = ROOT / '.local/config.json'
    original = None
    local = {'db_path': str(home / 'role-profiles/kanban/kanban.db'),
             'socket_path': str(ROOT / '.local/run/owner.sock'), 'owner_id': str(owners[0]),
             'notifier_profile': 'work-pm', 'max_timeout': 300, 'max_pending': 8}
    if private_config.exists():
        mode = private_config.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
            raise RuntimeError('existing bridge config is not private')
        original = private_config.read_bytes()
        existing = json.loads(original)
        for key in ('db_path', 'socket_path', 'owner_id', 'notifier_profile'):
            if existing.get(key) != local[key]:
                raise RuntimeError('existing bridge identity differs; review required')
        local.update(existing)
    # Explicit roster enrollment is shared with doctor. No runtime wildcard grants.
    if set(local.get('worker_profiles', ())) - set(roles):
        raise RuntimeError('configured non-work role needs explicit installation review')
    local.pop('worker_profile', None)
    local.update(worker_profiles=list(roles), notifier_state_db=str(gateway / 'state.db'), board_name='default')
    BridgeConfig(**local)
    staged[private_config] = (original, (json.dumps(local, indent=2) + '\n').encode(), 'bridge-config')
    summary = {'mode': 'apply' if args.apply else 'stage', 'worker_profiles': list(roles),
               'changed_files': [str(p) for p, (old, new, _) in staged.items() if old != new],
               'plugin_links': len(roles), 'pm_history_configured': True,
               'settings': ['plugins.enabled += kanban-owner', 'security.approval.kanban_transport = kanban-owner'],
               'owner_pinned': True}
    if args.apply:
        # Preflight every file/link before the first mutation; write private backups.
        for path, (old, new, label) in staged.items():
            replace_backed_up(path, old, new, label)
        for target, source in links.items():
            if not target.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
