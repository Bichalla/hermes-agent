"""Read task scope and corroborate PM handoffs against native tool receipts.

The board's author label is not authentication. Only an exact call and successful
receipt in the configured PM profile's state database mark a PM-authored handoff.
This is a same-owner local trust boundary, not protection against hostile OS users.
All reads are bounded, redacted and read-only; no board or session writes occur.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

MAX_COMMENTS = 128
MAX_COMMENT_CHARS = 64000
MAX_CALL_ROWS = 256
MAX_HISTORY_BYTES = 4 * 1024 * 1024
PM_HISTORY_COLUMNS = {'id', 'session_id', 'role', 'tool_calls', 'tool_call_id', 'content', 'timestamp'}


def _json(value, fallback):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return fallback


def _pm_provenance(config, task_id, comments):
    from .broker import _ro_connect
    from agent.redact import redact_sensitive_text
    if not comments:
        return {}, 'available'
    if not config.notifier_state_db:
        return {}, 'unavailable'
    proofs, consumed = {}, 0
    by_id = {c['id']: c for c in comments}
    try:
        db = _ro_connect(config.notifier_state_db)
        try:
            # Native compaction can copy a truncated call into a later message. Search
            # bounded original messages as well, and require the full body to match.
            rows = db.execute('''SELECT id, session_id, substr(tool_calls, 1, ?) AS tool_calls, timestamp FROM messages
                WHERE role='assistant' AND instr(tool_calls, ?) > 0
                AND instr(tool_calls, 'kanban_comment') > 0 ORDER BY id DESC LIMIT ?''',
                (MAX_HISTORY_BYTES + 1, task_id, MAX_CALL_ROWS + 1))
            for index, row in enumerate(rows):
                consumed += len((row['tool_calls'] or '').encode())
                if index >= MAX_CALL_ROWS or consumed > MAX_HISTORY_BYTES:
                    return proofs, 'truncated'
                calls = _json(row['tool_calls'], [])
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get('function', {})
                    if not isinstance(function, dict) or function.get('name') != 'kanban_comment':
                        continue
                    args = _json(function.get('arguments', {}), {})
                    call_id = call.get('id') or call.get('call_id')
                    if (not isinstance(args, dict) or args.get('task_id') != task_id or not call_id
                            or (args.get('board') or 'default') != config.board_name):
                        continue
                    body = redact_sensitive_text(str(args.get('body', '')), force=True).strip()
                    if not any(c['body'] == body for c in comments):
                        continue
                    receipts = db.execute('''SELECT id, content, timestamp FROM messages
                        WHERE session_id=? AND role='tool' AND tool_call_id=? AND id>?
                        ORDER BY id LIMIT 16''', (row['session_id'], call_id, row['id']))
                    for receipt in receipts:
                        result = _json(receipt['content'], {})
                        if not isinstance(result, dict) or result.get('ok') is not True or result.get('task_id') != task_id:
                            continue
                        comment = by_id.get(result.get('comment_id'))
                        if (not comment or comment['body'] != body
                                or not float(row['timestamp']) <= float(receipt['timestamp'])
                                or abs(float(receipt['timestamp']) - comment['created_at']) > 60):
                            continue
                        proofs[comment['id']] = {'profile': config.notifier_profile,
                            'session_id': row['session_id'], 'call_id': call_id,
                            'message_id': row['id'], 'receipt_id': receipt['id']}
            return proofs, 'available'
        finally:
            db.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {}, 'unavailable'


def read_context(config, task_id):
    from .broker import _ro_connect
    from agent.redact import redact_sensitive_text
    db = _ro_connect(config.db_path)
    try:
        db.execute('BEGIN')  # One read snapshot for task, current role and comments.
        columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        selected = [n for n in ('title', 'body', 'description', 'workspace_path', 'current_run_id',
                               'current_step_key', 'completion_contract', 'workflow_template_id') if n in columns]
        row = db.execute('SELECT ' + ','.join(selected) + ' FROM tasks WHERE id=?', (task_id,)).fetchone() if selected else None
        if not row:
            return {}
        raw = dict(row)
        result: dict = {k: redact_sensitive_text(str(v or '')[:32000], force=True) for k, v in raw.items()}
        truncated = any(len(str(v or '')) > 32000 for v in raw.values())
        run_columns = {r[1] for r in db.execute('PRAGMA table_info(task_runs)')}
        run_fields = [n for n in ('profile', 'step_key') if n in run_columns]
        if raw.get('current_run_id') and run_fields:
            run = db.execute('SELECT ' + ','.join(run_fields) + ' FROM task_runs WHERE id=?', (raw['current_run_id'],)).fetchone()
            raw['current_run'] = dict(run) if run else {}
            result['current_run'] = {k: redact_sensitive_text(str(v or ''), force=True) for k, v in raw['current_run'].items()}
        comments = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_comments'").fetchone():
            rows = db.execute('''SELECT id, author, body, created_at FROM task_comments
                WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT ?''', (task_id, MAX_COMMENTS + 1)).fetchall()
            truncated = truncated or len(rows) > MAX_COMMENTS
            raw['comments'] = [dict(r) for r in rows]
            budget = MAX_COMMENT_CHARS
            for r in rows[:MAX_COMMENTS]:
                body = redact_sensitive_text(r['body'] or '', force=True)
                if len(body) > budget:
                    truncated = True
                    break  # Never pass a partial directive as complete authority.
                budget -= len(body)
                comments.append({'id': r['id'], 'author': redact_sensitive_text(r['author'] or '', force=True),
                                 'body': body, 'created_at': r['created_at']})
            comments.reverse()
        proofs, status = _pm_provenance(config, task_id, comments)
        raw['provenance'] = proofs
        raw['handoff_status'] = status
        result['pm_handoffs'] = [dict(c, provenance=proofs[c['id']]) for c in comments if c['id'] in proofs]
        result['comments'] = [c for c in comments if c['id'] not in proofs]
        result['handoff_status'] = status
        result['_truncated'] = truncated or status == 'truncated'
        result['_revision'] = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        return result
    finally:
        db.close()
