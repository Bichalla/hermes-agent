"""Bounded source inspection for PM review; never imports or executes reviewed code."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_CHARS = 512_000
MAX_FILES = 32
SOURCE_SUFFIXES = {'.py', '.sh', '.bash', '.zsh', '.js', '.mjs', '.cjs', '.ts', '.tsx',
                   '.jsx', '.rs', '.go', '.c', '.h', '.cpp', '.rb', '.toml', '.md', '.json', '.yaml', '.yml'}
PRIVATE_PARTS = {'.ssh', '.gnupg', '.git', '.local', 'credentials', 'auth.json',
                 'tokens.json', 'config.yaml', 'config.json'}


def _read_bytes(path: Path) -> bytes:
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_parent
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('not bounded regular source')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('source too large')
        return data
    finally:
        os.close(fd)


def _private(path: Path) -> bool:
    return any(p.lower() in PRIVATE_PARTS or p.lower().startswith('.env') for p in path.parts)


def verify_snapshot(snapshot: list[dict]) -> bool:
    """Check both path resolution and bytes immediately before using a decision."""
    if not isinstance(snapshot, list) or len(snapshot) > MAX_FILES:
        return False
    try:
        for item in snapshot:
            path = Path(item['path'])
            resolved = path.resolve(strict=True)
            if (not path.is_absolute() or str(resolved) != item['resolved'] or _private(resolved)
                    or resolved.suffix.lower() not in SOURCE_SUFFIXES
                    or hashlib.sha256(_read_bytes(resolved)).hexdigest() != item['sha256']):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def review_roots(data: dict) -> list[Path]:
    """Task source tree, the loaded core, and exact referenced source files only."""
    roots = []
    workspace = data.get('task_context', {}).get('workspace_path', '')
    if workspace and Path(workspace).is_absolute():
        roots.append(Path(workspace).resolve())
    from tools import approval
    roots.append(Path(approval.__file__).resolve().parents[1])
    # An absolute source filename in the command can be inspected, but does not
    # grant access to its parent directory or unrelated application data.
    for name in re.findall(r"/[^\s'\";<>|]+\.(?:py|sh|js|ts|mjs|rb)\b", data.get('command', '')):
        path = Path(name)
        if path.is_file() and not _private(path):
            roots.append(path.resolve())
    return list(dict.fromkeys(roots))


class SourceReader:
    def __init__(self, cwd: Path, roots: list[Path]):
        self.cwd = cwd.resolve()
        self.roots = [p.resolve() for p in roots if p.is_absolute()]
        self._snapshot: dict[str, dict] = {}
        self.used_chars = 0

    def snapshot(self) -> list[dict]:
        return list(self._snapshot.values())

    def read(self, request: dict) -> dict:
        try:
            name = request.get('path')
            if not isinstance(name, str) or len(name) > 4096:
                raise ValueError('invalid_path')
            path = Path(name)
            path = path if path.is_absolute() else self.cwd / path
            resolved = path.resolve(strict=True)
            if (_private(path) or _private(resolved) or resolved.suffix.lower() not in SOURCE_SUFFIXES
                    or not any(resolved == root or root.is_dir() and resolved.is_relative_to(root) for root in self.roots)):
                raise ValueError('source_outside_review_scope')
            key = str(path)
            if len(self._snapshot) >= MAX_FILES and key not in self._snapshot:
                raise ValueError('source_file_budget_exhausted')
            raw = _read_bytes(resolved)
            source = raw.decode('utf-8')
            fingerprint = {'path': key, 'resolved': str(resolved), 'sha256': hashlib.sha256(raw).hexdigest()}
            if key in self._snapshot and self._snapshot[key] != fingerprint:
                raise ValueError('source_changed_during_review')
            self._snapshot[key] = fingerprint
            start = request.get('start_line', 1)
            limit = request.get('max_lines', 400)
            if (type(start) is not int or type(limit) is not int or start < 1 or not 1 <= limit <= 600):
                raise ValueError('invalid_source_range')
            lines = source.splitlines()
            page = '\n'.join(f'{i + 1}: {line}' for i, line in enumerate(lines) if start - 1 <= i < start - 1 + limit)
            if len(page) > 60_000:
                raise ValueError('source_text_budget_exhausted')
            from agent.redact import redact_sensitive_text
            result = {**fingerprint, 'start_line': start, 'total_lines': len(lines),
                      'partial': start != 1 or len(lines) > limit,
                      'text': redact_sensitive_text(page, force=True)}
            if resolved.suffix == '.py':
                try:
                    tree = ast.parse(source)
                    result['definitions'] = [{'name': n.name, 'start': n.lineno, 'end': n.end_lineno}
                                             for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                    result['import_time_statements'] = [
                        {'start': n.lineno, 'end': n.end_lineno,
                         'text': redact_sensitive_text(ast.get_source_segment(source, n)[:1600], force=True)}
                        for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                                                                ast.AsyncFunctionDef, ast.ClassDef))
                        and any(isinstance(child, ast.Call) for child in ast.walk(n))
                    ][:40]
                except SyntaxError:
                    result['parse_error'] = True
            # Include indexes and import-time excerpts in the same total budget.
            size = len(json.dumps(result, ensure_ascii=False))
            if self.used_chars + size > MAX_TOTAL_CHARS:
                raise ValueError('source_text_budget_exhausted')
            self.used_chars += size
            return result
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # Exception text can contain source data; return fixed codes only.
            code = str(exc) if type(exc) is ValueError and re.fullmatch('[a-z_]+', str(exc)) else 'source_unavailable'
            return {'path': request.get('path', '') if isinstance(request, dict) else '', 'error': code}
