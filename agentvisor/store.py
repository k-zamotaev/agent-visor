"""Durable task state. Each write is committed before it is exposed to the UI."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .i18n import RAW_EVENTS, translate


ACTIVE = {'preparing', 'running', 'verifying', 'recovering', 'pausing', 'stopping'}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'agentvisor.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    time REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL,
                    message TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_task ON events(task_id, id);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, values):
        task = dict(values, id=uuid.uuid4().hex[:12], status='draft', iteration=0,
                    created=time.time(), elapsed=0.0, output_tokens=0, recoveries=0,
                    goal_version=1, applied_goal_version=0, reason='', pid=None,
                    pid_created=None, started=None, updated=time.time())
        with self.connect() as db:
            db.execute('INSERT INTO tasks VALUES (?, ?, ?)',
                       (task['id'], json.dumps(task, ensure_ascii=False), task['updated']))
        self.event(task['id'], 'task_created', 'Задача создана')
        return task

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return json.loads(row['body'])

    def list(self):
        with self.connect() as db:
            return [json.loads(r['body']) for r in db.execute('SELECT body FROM tasks ORDER BY updated DESC')]

    def update(self, task_id, **values):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row['body'])
            if 'reason' in values:
                values['reason_source'] = values['reason']
                values['reason'] = translate(values['reason'], task.get('language', 'ru'))
            task.update(values, updated=time.time())
            db.execute('UPDATE tasks SET body=?, updated=? WHERE id=?',
                       (json.dumps(task, ensure_ascii=False), task['updated'], task_id))
        return task

    def event(self, task_id, kind, message, level='info', data=None):
        data = dict(data or {})
        if kind not in RAW_EVENTS:
            language = self.get(task_id).get('language', 'ru')
            data['_i18n'] = {'source': str(message), 'language': language}
            message = translate(str(message), language)
        with self.connect() as db:
            db.execute('INSERT INTO events(task_id,time,level,kind,message,data) VALUES(?,?,?,?,?,?)',
                       (task_id, time.time(), level, kind, str(message)[:6000],
                        json.dumps(data, ensure_ascii=False)))

    def add_context(self, task_id, text):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row['body'])
            additions = task.get('context_additions', [])
            if len(additions) >= 40 or sum(len(item['text']) for item in additions) + len(text) > 20000:
                raise ValueError('Дополнения превысили 20000 символов или 40 записей. Уточните основную цель.')
            version = task.get('context_version', 0) + 1
            additions.append({'version': version, 'text': text, 'created': time.time()})
            task.update(context_additions=additions, context_version=version, updated=time.time())
            db.execute('UPDATE tasks SET body=?, updated=? WHERE id=?',
                       (json.dumps(task, ensure_ascii=False), task['updated'], task_id))
        self.event(task_id, 'context_added', 'Дополнение сохранено и ожидает передачи модели', data={'version': version})
        return task

    def mark_context_delivered(self, task_id, version):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = json.loads(row['body'])
            if task.get('applied_context_version', 0) >= version:
                return
            task.update(applied_context_version=version, updated=time.time())
            db.execute('UPDATE tasks SET body=?, updated=? WHERE id=?',
                       (json.dumps(task, ensure_ascii=False), task['updated'], task_id))
        self.event(task_id, 'context_delivered', 'Дополнение передано модели', data={'version': version})

    def events(self, task_id, after=0, limit=200):
        with self.connect() as db:
            if after:
                rows = db.execute('SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?',
                                  (task_id, after, limit)).fetchall()
            else:
                rows = db.execute('SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?',
                                  (task_id, limit)).fetchall()[::-1]
        return [dict(row, data=json.loads(row['data'])) for row in rows]

    def metrics(self, task_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND kind IN "
                              "('iteration_finished', 'generation_sample', 'recovering') "
                              "ORDER BY id DESC LIMIT 400", (task_id,)).fetchall()[::-1]
        return [dict(row, data=json.loads(row['data'])) for row in rows]

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def save_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
