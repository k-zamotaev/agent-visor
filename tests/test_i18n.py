import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentvisor.app import create_app
from agentvisor.i18n import event_view, language_from_header, translate
from agentvisor.store import Store
from agentvisor.tasks import NewTask, prepare_documents, read_document, state_dir


@pytest.mark.parametrize(('header', 'expected'), [
    ('en-US,en;q=0.9,ru;q=0.8', 'en'), ('en;q=0.1,ru;q=1', 'ru'),
    ('de,en-GB;q=0.5', 'en'), ('en;q=0', 'ru'), ('en;q=oops', 'ru'), ('', 'ru'),
])
def test_language_negotiation(header, expected):
    assert language_from_header(header) == expected


def test_nested_error_round_trip_preserves_runtime_payload():
    raw = '{"error": "request (20566 tokens) exceeds context", "path": "D:\\Тест"}'
    source = 'Повтор 1/3: Ошибка итерации: ' + raw
    translated = translate(source, 'en')
    assert translated == 'Retry 1/3: Iteration error: ' + raw
    assert translate(translated, 'ru') == source


def test_stored_english_logs_and_legacy_russian_history(tmp_path):
    store = Store(tmp_path)
    task = store.create(NewTask(name='Модель', workspace=str(tmp_path), goal='Цель без перевода',
                               language='en').model_dump())
    store.update(task['id'], reason='Подготовка модели и контекста')
    assert store.get(task['id'])['reason'] == 'Preparing model and context'
    assert store.get(task['id'])['name'] == 'Модель'
    store.event(task['id'], 'running', 'Итерация 2: следующий шаг')
    store.event(task['id'], 'text', 'Подготовка модели и контекста')
    events = store.events(task['id'])
    assert events[0]['message'] == 'Task created'
    assert events[1]['message'] == 'Iteration 2: next step'
    assert event_view(events[1], 'ru')['message'] == 'Итерация 2: следующий шаг'
    assert event_view(events[2], 'en')['message'] == 'Подготовка модели и контекста'
    legacy = {'kind': 'recovering', 'message': 'Повтор 1/2: Ошибка итерации: timeout', 'data': {}}
    assert event_view(legacy, 'en')['message'] == 'Retry 1/2: Iteration error: timeout'
    assert legacy['message'].startswith('Повтор')


def test_api_language_does_not_change_task_or_raw_output(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.headers['Accept-Language'] = 'en-US'
        assert client.post('/api/tasks', json={}).json()['detail'].startswith('Refresh the page')
        client.headers['x-agentvisor-token'] = client.get('/api/session').json()['token']
        body = dict(name='Настройки', workspace=str(tmp_path), goal='Цель: сохранить русский текст')
        task = client.post('/api/tasks', json=body).json()
        assert task['language'] == 'en'
        assert task['name'] == body['name'] and task['goal'] == body['goal']
        task_id = task['id']
        app.state.store.update(task_id, reason='Достигнут лимит итераций')
        app.state.store.event(task_id, 'output', 'Задача создана')
        endpoint = f'/api/tasks/{task_id}'
        assert client.get(endpoint).json()['reason'] == 'Iteration limit reached'
        events = client.get(endpoint + '/events').json()
        assert events[0]['message'] == 'Task created'
        assert events[-1]['message'] == 'Задача создана'
        assert '_i18n' not in events[0]['data']
        client.headers['Accept-Language'] = 'ru'
        assert client.get(endpoint).json()['reason'] == 'Достигнут лимит итераций'
        assert client.get(endpoint + '/events').json()[0]['message'] == 'Задача создана'
        assert app.state.store.events(task_id)[0]['message'] == 'Task created'
        assert client.get('/api/tasks/missing').json()['detail'] == 'Задача не найдена'
        client.headers['Accept-Language'] = 'en'
        assert client.get('/api/tasks/missing').json()['detail'] == 'Task not found'
        bad = client.put('/api/profile', json={'context': 4096, 'output_limit': 4096})
        assert bad.status_code == 422
        assert bad.json()['detail'][0]['msg'] == 'The output limit must be less than half the context'
        assert bad.headers['content-language'] == 'en'


def test_english_demo_and_agent_prompt(tmp_path):
    store = Store(tmp_path / 'data')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    task = store.create(NewTask(name='Demo', workspace=str(workspace), goal='Keep исходный текст',
                               mode='demo', language='en').model_dump())
    prompt = prepare_documents(task, {'instance': 'demo', 'context': 16384})
    assert 'Write progress notes and explanations in English' in prompt
    assert 'Keep исходный текст' in read_document(task, 'GOAL.md')
    script = Path(__file__).parents[1] / 'agentvisor/demo_agent.py'
    env = dict(os.environ, AGENTVISOR_LANGUAGE='en', PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, str(script), str(state_dir(task))], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=10, check=True)
    assert json.loads(result.stdout.splitlines()[0])['part']['text'].startswith('DEMO: running step 1.')
    assert not re.search('[А-Яа-яЁё]', read_document(task, 'PROGRESS.md'))


def test_catalog_has_no_duplicate_keys_and_templates_keep_parameters():
    path = Path(__file__).parents[1] / 'agentvisor/static/locales/en.json'
    pairs = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=list)
    assert len(dict(pairs)) == len(pairs)
    for source, target in pairs:
        assert sorted(re.findall(r'\{\d+\}', source)) == sorted(re.findall(r'\{\d+\}', target)), source
        assert target.strip() and not re.search('[А-Яа-яЁё]', target), source
