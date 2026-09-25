import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import DEFAULT_PROFILE


class Profile(BaseModel):
    runtime: Literal['lmstudio', 'ollama'] = 'lmstudio'
    base_url: str = DEFAULT_PROFILE['base_url']
    model: str = Field(default='', max_length=400)
    context: int = Field(default=16384, ge=4096, le=262144)
    output_limit: int = Field(default=4096, ge=256, le=32768)
    manage_runtime: bool = True

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Нужен HTTP(S)-адрес сервера без логина и пароля')
        if parsed.query or parsed.fragment or parsed.path.strip('/'):
            raise ValueError('Укажите корневой адрес сервера, без /v1 и параметров')
        return value.rstrip('/')

    @model_validator(mode='after')
    def budget(self):
        if self.output_limit >= self.context // 2:
            raise ValueError('Лимит ответа должен быть меньше половины контекста')
        return self


class NewTask(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workspace: str = Field(min_length=1, max_length=1000)
    goal: str = Field(min_length=3, max_length=20000)
    profile: Profile = Field(default_factory=Profile)
    max_iterations: int = Field(default=40, ge=1, le=1000)
    timeout_seconds: int = Field(default=1800, ge=5, le=21600)
    max_failures: int = Field(default=3, ge=1, le=10)
    stall_limit: int = Field(default=5, ge=2, le=30)
    backoff_seconds: float = Field(default=30, ge=0.1, le=300)
    max_hours: float = Field(default=12, ge=0.01, le=168)
    auto_permissions: bool = False
    auto_tune: bool = True
    verification: list[str] = Field(default_factory=list, max_length=40)
    mode: Literal['opencode', 'demo'] = 'opencode'
    language: Literal['ru', 'en'] = 'ru'

    @field_validator('name', 'goal')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Поле не может состоять из пробелов')
        return value.strip()


def state_dir(task):
    workspace = Path(task['workspace']).resolve()
    directory = workspace / '.agentvisor' / 'tasks' / task['id']
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(workspace):
        raise ValueError('Каталог состояния выходит за пределы проекта')
    return directory


def document_path(task, name):
    if name not in {'GOAL.md', 'PROGRESS.md', 'DONE.md', 'opencode.json'}:
        raise ValueError('Неизвестный документ')
    root = state_dir(task)
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Документ состояния не может быть символической ссылкой')
    return target


def read_document(task, name):
    path = document_path(task, name)
    if not path.exists():
        return ''
    with path.open(encoding='utf-8-sig', errors='replace') as file:
        return file.read(200_000)


def write_document(task, name, text):
    target = document_path(task, name)
    temp = target.with_suffix(target.suffix + '.tmp')
    if temp.is_symlink():
        raise ValueError('Временный файл не может быть ссылкой')
    temp.write_text(text, encoding='utf-8')
    os.replace(temp, target)


def checklist(task):
    content = read_document(task, 'PROGRESS.md')
    return [{'done': match[0].lower() == 'x', 'text': match[1].strip()}
            for match in re.findall(r'^\s*(?:[-*]|\d+[.)])\s+\[([ xX])\]\s+(.+)$', content, re.M)]


def prepare_documents(task, ready):
    version = task['goal_version']
    write_document(task, 'GOAL.md', f'# Goal\n\ngoal_version: {version}\n\n{task["goal"]}\n')
    if task['applied_goal_version'] != version:
        old = read_document(task, 'DONE.md')
        if old:
            (state_dir(task) / f'DONE-before-v{version}.md').write_text(old, encoding='utf-8')
            document_path(task, 'DONE.md').unlink()
    if not read_document(task, 'PROGRESS.md'):
        write_document(task, 'PROGRESS.md', '# Progress\n\nCreate a short numbered checklist from GOAL.md, then do one step.\n')
    profile = task.get('resolved_profile') or task['profile']
    model = ready['instance']
    provider = {'npm': '@ai-sdk/openai-compatible', 'name': 'AgentVisor local runtime',
                'options': {'baseURL': profile['base_url'] + '/v1'},
                'models': {model: {'name': model, 'limit': {'context': ready['context'],
                           'output': profile['output_limit']}}}}
    config = {'$schema': 'https://opencode.ai/config.json', 'provider': {'agentvisor': provider},
              'model': 'agentvisor/' + model, 'share': 'disabled'}
    write_document(task, 'opencode.json', json.dumps(config, ensure_ascii=False, indent=2))
    relative = state_dir(task).relative_to(Path(task['workspace'])).as_posix()
    return (
        f'Work in small verified steps. Read {relative}/GOAL.md and {relative}/PROGRESS.md. '
        f'Current goal_version: {version}. If the goal version changed, reconcile the checklist first. '
        'Take only the NEXT unchecked concrete step: implement, run a meaningful verification, '
        'record evidence, then stop this session. Keep reasoning short and use tools. '
        f'Before stopping update {relative}/PROGRESS.md with a markdown checklist, results, '
        'the goal version, blockers and the next step. Respect all project AGENTS.md rules. '
        'Never mark a failed check as passed. Do not deploy, publish or send messages without authorization. '
        f'Only if the entire current goal is achieved write {relative}/DONE.md with '
        f'goal_version: {version} on its own line and a summary of verification. '
        'Do not overwrite unrelated root GOAL.md, PROGRESS.md or DONE.md. '
        'No human is waiting in this CLI session. If blocked, record the blocker and end.'
        f' Write progress notes and explanations in {"English" if task.get("language") == "en" else "Russian"}. '
        'Preserve exact user text, code, paths and command output; do not translate them.'
    )
