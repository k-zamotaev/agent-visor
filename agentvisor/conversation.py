"""Read-only, paginated task conversation over the existing durable journal."""
import base64
import binascii
import json
import math
import re

from .i18n import event_view
from .user_instructions import statuses


TOOLS = ('tool', 'tool_started', 'tool_finished')
VISIBLE = ('text', 'reasoning', *TOOLS, 'task_created', 'task_updated', 'preparing',
           'running', 'verifying', 'recovering', 'paused', 'blocked', 'failed', 'stopped',
           'succeeded', 'completed_unverified', 'service_restarted', 'session_handoff',
           'step_review_accepted', 'step_review_rejected', 'verification_finished',
           'instructions_planned', 'context_delivered', 'agent_error', 'runtime_lost')
TEXT_LIMIT = 16000


def _cursor(task_id, key):
    value = json.dumps([1, task_id, *key], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def _decode(task_id, value):
    if not value:
        return None
    try:
        if len(value) > 1000 or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise ValueError()
        data = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        version, identity, timestamp, key = data
        if (version != 1 or identity != task_id or type(timestamp) not in (float, int) or
                not math.isfinite(timestamp) or not isinstance(key, str) or
                not re.fullmatch(r'[EU]:[0-9]{20}', key)):
            raise ValueError()
        return timestamp, key
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise ValueError('Invalid conversation cursor') from None


def _actor(db, task_id, event):
    data = event['data']
    role = data.get('actor') or data.get('role')
    if isinstance(role, dict):
        role = role.get('name')
    if role not in {'executor', 'reviewer', 'diagnostician'}:
        row = db.execute("SELECT data FROM events WHERE task_id=? AND kind='session_role' AND id<=? "
                         'ORDER BY id DESC LIMIT 1', (task_id, event['id'])).fetchone()
        role = json.loads(row['data']).get('name') if row else None
    return role if role in {'executor', 'reviewer', 'diagnostician'} else None


def _group(event):
    data, kind = event['data'], event['kind']
    if kind == 'reasoning' and isinstance(data.get('request_id'), str) and data['request_id']:
        return 'request_id', data['request_id'], ('reasoning',)
    if kind in TOOLS and isinstance(data.get('call_id'), str) and data['call_id']:
        return 'call_id', data['call_id'], TOOLS
    return None


def _details(data):
    result = {key: value for key, value in data.items() if key != '_i18n'}
    rendered = json.dumps(result, ensure_ascii=False)
    return result if len(rendered) <= TEXT_LIMIT else {'preview': rendered[:TEXT_LIMIT], 'truncated': True}


def _entry(db, task_id, row, language):
    event = dict(row, data=json.loads(row['data']))
    kind = event['kind']
    identity, text, updated, details = f'event:{event["id"]}', event['message'], event['time'], _details(event['data'])
    group = _group(event)
    state = event['data'].get('status')
    if group:
        field, value, kinds = group
        where = f"task_id=? AND json_extract(data, '$.{field}')=? AND kind IN ({','.join('?' for _ in kinds)})"
        parameters = (task_id, value, *kinds)
        latest = db.execute(f'SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT 1', parameters).fetchone()
        updated = latest['time']
        if kind == 'reasoning':
            identity, remaining, fragments, count = 'reasoning:' + value, TEXT_LIMIT, [], 0
            rows = db.execute(f'SELECT message FROM events WHERE {where} ORDER BY id LIMIT 257', parameters)
            for fragment in rows:
                if remaining <= 0 or count >= 256:
                    break
                fragments.append(fragment['message'][:remaining])
                remaining -= len(fragments[-1])
                count += 1
            text = ''.join(fragments)
            total = db.execute(f'SELECT COUNT(*),SUM(length(message)) FROM events WHERE {where}', parameters).fetchone()
            details.update(fragment_count=total[0], truncated=(total[1] or 0) > len(text))
        else:
            identity = 'tool:' + value
            # Trace results contain the authoritative input and exit status. CLI
            # title events can arrive afterwards and must not erase that result.
            authoritative = db.execute(f"SELECT * FROM events WHERE {where} AND kind='tool_finished' "
                                       'ORDER BY id DESC LIMIT 1', parameters).fetchone()
            chosen = authoritative or latest
            merged = dict(event['data'], **json.loads(chosen['data']))
            details, state = _details(merged), merged.get('status')
            text = merged.get('tool') or chosen['message']
    role = 'assistant' if kind in ('text', 'reasoning', *TOOLS) else 'system'
    if kind not in ('text', 'reasoning', *TOOLS):
        text = event_view(event, language)['message']
    text_limit = 64000 if kind == 'text' else TEXT_LIMIT
    if len(text) > text_limit:
        text, details['truncated'] = text[:text_limit], True
    if kind in ('reasoning', *TOOLS):
        details['collapsed'] = True
    details['first_event_id'] = event['id']
    return {'id': identity, 'role': role, 'actor': _actor(db, task_id, event) if role == 'assistant' else None,
            'kind': 'tool' if kind in TOOLS else kind if kind in ('text', 'reasoning') else 'status',
            'text': text, 'time': event['time'], 'updated_at': updated, 'state': state,
            'step_ids': [], 'details': details, 'event_kind': kind, 'level': event['level'],
            '_key': (event['time'], f'E:{event["id"]:020d}')}


def event_detail(store, task_id, event_id, *, language='ru'):
    """Read an original journal entry or its complete bounded group by ID."""
    with store.connect() as db:
        db.execute('BEGIN')
        if db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone() is None:
            raise KeyError(task_id)
        row = db.execute('SELECT * FROM events WHERE task_id=? AND id=?', (task_id, event_id)).fetchone()
        if row is None:
            raise KeyError(event_id)
        event = dict(row, data=json.loads(row['data']))
        group = _group(event)
        event.update(first_event_id=event['id'], last_event_id=event['id'], fragment_count=1)
        if group:
            field, value, kinds = group
            where = f"task_id=? AND json_extract(data, '$.{field}')=? AND kind IN ({','.join('?' for _ in kinds)})"
            parameters = (task_id, value, *kinds)
            first = db.execute(f'SELECT * FROM events WHERE {where} ORDER BY id LIMIT 1', parameters).fetchone()
            last = db.execute(f'SELECT id FROM events WHERE {where} ORDER BY id DESC LIMIT 1', parameters).fetchone()
            total = db.execute(f'SELECT COUNT(*),SUM(length(message)) FROM events WHERE {where}', parameters).fetchone()
            event = dict(first, data=json.loads(first['data']), first_event_id=first['id'],
                         last_event_id=last['id'], fragment_count=total[0])
            if event['kind'] == 'reasoning':
                remaining, fragments = 128000, []
                # At most 128001 rows, even for malformed streams of empty
                # fragments. Only this indexed response group is visited.
                for fragment in db.execute(f'SELECT message FROM events WHERE {where} ORDER BY id LIMIT 128001', parameters):
                    if remaining <= 0:
                        break
                    value = fragment['message'][:remaining]
                    fragments.append(value)
                    remaining -= len(value)
                event['message'] = ''.join(fragments)
                event['data'].update(truncated=(total[1] or 0) > len(event['message']), original_chars=total[1] or 0)
            else:
                merged = _entry(db, task_id, first, language)
                event.update(message=merged['text'], data=merged['details'])
        event['truncated'] = bool(event['data'].get('truncated'))
        return event_view(event, language)


def conversation(store, task_id, *, before=None, limit=60, language='ru'):
    """Return the latest page, or older items, without syncing task documents."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Conversation limit must be between 1 and 100')
    boundary = _decode(task_id, before)
    with store.connect() as db:
        # One SQLite snapshot keeps messages, their current statuses and events
        # consistent while the running supervisor writes new journal records.
        db.execute('BEGIN')
        row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = json.loads(row['body'])
        items = []
        for note in statuses(task):
            key = (note['created'], f'U:{note["version"]:020d}')
            if boundary is None or key < boundary:
                items.append({'id': f'context:{note["version"]}', 'role': 'user', 'actor': None,
                              'kind': note['kind'], 'text': note['text'], 'time': note['created'],
                              'updated_at': task['updated'], 'state': note['state'], 'step_ids': note['step_ids'],
                              'details': {'version': note['version'], 'recheck': note.get('recheck', False),
                                          'client_message_id': note.get('client_message_id')}, '_key': key})
        clauses = ['e.task_id=?', f"e.kind IN ({','.join('?' for _ in VISIBLE)})"]
        parameters = [task_id, *VISIBLE]
        if boundary:
            clauses.append("(e.time<? OR (e.time=? AND printf('E:%020d',e.id)<?))")
            parameters.extend((boundary[0], boundary[0], boundary[1]))
        # Select only each group's first immutable event. This keeps pagination
        # stable as more reasoning fragments or tool results arrive later.
        for field, kinds in (('request_id', ('reasoning',)), ('call_id', TOOLS)):
            names = ','.join('?' for _ in kinds)
            clauses.append(f"""NOT (e.kind IN ({names}) AND COALESCE(json_type(e.data,'$.{field}')='text',0)
                AND json_extract(e.data,'$.{field}')!='' AND EXISTS (
                    SELECT 1 FROM events p WHERE p.task_id=e.task_id
                    AND json_extract(p.data,'$.{field}')=json_extract(e.data,'$.{field}')
                    AND p.kind IN ({names}) AND p.id<e.id))""")
            parameters.extend((*kinds, *kinds))
        rows = db.execute(f"SELECT e.* FROM events e WHERE {' AND '.join(clauses)} "
                          'ORDER BY e.time DESC,e.id DESC LIMIT ?', (*parameters, limit + 1)).fetchall()
        items.extend(_entry(db, task_id, row, language) for row in rows)
    items.sort(key=lambda item: item['_key'])
    has_more = len(items) > limit
    items = items[-limit:]
    next_before = _cursor(task_id, items[0]['_key']) if items and has_more else None
    for item in items:
        item.pop('_key')
    additions = task.get('context_additions', [])
    used = sum(len(item['text']) for item in additions)
    immutable = task['status'] in {'succeeded', 'completed_unverified'}
    return {'items': items, 'next_before': next_before, 'has_more': has_more,
            'composer': {'message_max_chars': 6000, 'max_messages': 40, 'max_total_chars': 20000,
                         'messages_used': len(additions), 'chars_used': used,
                         'messages_remaining': max(0, 40 - len(additions)), 'chars_remaining': max(0, 20000 - used),
                         'can_send': not immutable and len(additions) < 40 and used < 20000,
                         'paused': task['status'] in {'draft', 'paused', 'blocked', 'failed', 'stopped'}}}
