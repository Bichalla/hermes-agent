"""Discover roles at installation; authorize exact configured running roles."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import yaml


def discover_worker_profiles(home: Path) -> tuple[str, ...]:
    return tuple(sorted(p.parent.name for p in (home / 'profiles').glob('work-*/config.yaml')
                        if re.fullmatch(r'work-[a-z0-9-]+', p.parent.name)))


def _insert_lines(text, line, fragment):
    lines = text.splitlines(keepends=True)
    if line and lines[line - 1] and not lines[line - 1].endswith('\n'):
        lines[line - 1] += '\n'
    lines.insert(line, fragment)
    return ''.join(lines)


def _set(text, path, value):
    """Change one YAML value, retaining unrelated text and comments."""
    node = yaml.compose(text)
    data = yaml.safe_load(text) or {}
    if node is None:
        nested = value
        for key in reversed(path):
            nested = {key: nested}
        return yaml.safe_dump(nested, sort_keys=False)
    for position, key in enumerate(path):
        if not isinstance(node, yaml.MappingNode):
            raise ValueError('bridge setting requires a mapping')
        child = next((v for k, v in node.value if k.value == key), None)
        if child is None:
            nested = value
            for name in reversed(path[position:]):
                nested = {name: nested}
            if node.flow_style:
                fragment = json.dumps(dict(data, **nested))
                return text[:node.start_mark.index] + fragment + text[node.end_mark.index:]
            indent = node.value[0][0].start_mark.column if node.value else node.start_mark.column
            fragment = ''.join(' ' * indent + line for line in yaml.safe_dump(nested, sort_keys=False).splitlines(keepends=True))
            return _insert_lines(text, node.end_mark.line, fragment)
        if position == len(path) - 1:
            if isinstance(child, yaml.SequenceNode) and not child.flow_style and isinstance(value, list):
                current = data[key]
                if value[:len(current)] == current:
                    fragment = ''.join(' ' * child.start_mark.column + '- ' + json.dumps(v) + '\n'
                                       for v in value[len(current):])
                    return _insert_lines(text, child.end_mark.line, fragment)
            return text[:child.start_mark.index] + json.dumps(value) + text[child.end_mark.index:]
        node, data = child, data[key]
    raise ValueError('empty bridge setting path')


def stage_profile(text: str) -> str:
    config = yaml.safe_load(text) or {}
    if not isinstance(config, dict):
        raise ValueError('profile configuration must be a mapping')
    expected = copy.deepcopy(config)
    plugins = expected.setdefault('plugins', {})
    if 'kanban-owner' in plugins.get('disabled', []):
        raise ValueError('bridge is explicitly disabled; do not override it')
    enabled = plugins.setdefault('enabled', [])
    if not isinstance(enabled, list):
        raise ValueError('plugins.enabled must be a list')
    if 'kanban-owner' not in enabled:
        enabled.append('kanban-owner')
        text = _set(text, ('plugins', 'enabled'), enabled)
    approval = expected.setdefault('security', {}).setdefault('approval', {})
    if approval.get('kanban_transport') not in (None, 'kanban-owner'):
        raise ValueError('another approval transport is selected; do not overwrite it')
    if approval.get('kanban_transport') != 'kanban-owner':
        approval['kanban_transport'] = 'kanban-owner'
        text = _set(text, ('security', 'approval', 'kanban_transport'), 'kanban-owner')
    if yaml.safe_load(text) != expected:
        raise ValueError('staged changes exceed plugin and transport settings')
    return text


def profile_coverage(home: Path, root: Path, config, used_profiles=()) -> dict:
    required = set(discover_worker_profiles(home)) | set(used_profiles)
    enrolled = set(config.worker_profiles)
    profiles = {}
    for name in sorted(required | enrolled):
        profile = home / 'profiles' / name
        try:
            cfg = yaml.safe_load((profile / 'config.yaml').read_text()) or {}
            plugins = cfg.get('plugins', {})
            link = profile / 'plugins/kanban-owner'
            profiles[name] = {
                'enrolled': name in enrolled,
                'plugin': ('kanban-owner' in plugins.get('enabled', [])
                           and 'kanban-owner' not in plugins.get('disabled', [])
                           and link.is_symlink() and link.resolve() == (root / 'plugins/kanban-owner').resolve()),
                'transport': cfg.get('security', {}).get('approval', {}).get('kanban_transport') == 'kanban-owner',
            }
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            profiles[name] = {'enrolled': name in enrolled, 'plugin': False, 'transport': False}
    return {'profiles': profiles, 'missing_profiles': sorted(required - enrolled),
            'ready': bool(profiles) and all(all(p.values()) for p in profiles.values())}
