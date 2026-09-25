import os
import secrets
import httpx
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .hardware import hardware
from .models import DEFAULT_PROFILE, ModelRuntime
from .store import Store
from .supervisor import Supervisor
from .tasks import NewTask, Profile, checklist, read_document, state_dir
from .version import build_id
from .i18n import event_view, language_from_header, model_view, task_view, translate
from .instance import lock_instance
from .network import ensure_network, trusted_hosts
from .access import authenticated, register_network


class TaskEdit(BaseModel):
    goal: str | None = Field(default=None, min_length=3, max_length=20000)
    max_iterations: int | None = Field(default=None, ge=1, le=1000)
    timeout_seconds: int | None = Field(default=None, ge=5, le=21600)
    max_hours: float | None = Field(default=None, ge=0.01, le=168)


def create_app(data_dir=None):
    directory = Path(data_dir or os.environ.get('AGENTVISOR_DATA', '.agentvisor-data/runtime')).resolve()
    store = Store(directory)
    runtime = ModelRuntime()
    token = secrets.token_urlsafe(32)
    current_build = build_id()
    def language(request):
        return language_from_header(request.headers.get('accept-language'))

    def error_response(request, message, status):
        return JSONResponse({'detail': translate(message, language(request))}, status_code=status,
                            headers={'Content-Language': language(request), 'Vary': 'Accept-Language',
                                     'Cache-Control': 'no-store'})
    default_profile = Profile(runtime=os.environ.get('AGENTVISOR_MODEL_RUNTIME', 'lmstudio'),
                              base_url=os.environ.get('AGENTVISOR_MODEL_URL', DEFAULT_PROFILE['base_url']),
                              model=os.environ.get('AGENTVISOR_MODEL', '')).model_dump()

    @asynccontextmanager
    async def lifespan(app):
        lock = lock_instance(directory)
        try:
            app.state.network = ensure_network(directory)
            app.state.engine = Supervisor(store, runtime)
            try:
                if callable(getattr(app.state, 'startup_notice', None)):
                    app.state.startup_notice()
                yield
            finally:
                app.state.engine.shutdown()
        finally:
            lock.close()

    app = FastAPI(title='AgentVisor', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.store = store
    register_network(app, directory)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts())

    @app.middleware('http')
    async def protect(request: Request, call_next):
        login_request = request.url.path == '/api/auth/login' and request.method == 'POST'
        if request.url.path.startswith('/api/') and not login_request and not authenticated(request):
            return error_response(request, 'Введите код доступа к AgentVisor', 401)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return error_response(request, 'Недопустимый источник запроса', 403)
            if not login_request and not secrets.compare_digest(request.headers.get('x-agentvisor-token', '').encode(), token.encode()):
                return error_response(request, 'Обновите страницу: сессия управления изменилась', 403)
            if app.state.restarting:
                return error_response(request, 'Панель перезапускается', 503)
            if int(request.headers.get('content-length', '0') or 0) > 65536:
                return error_response(request, 'Слишком большой запрос', 413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Content-Language'] = language(request)
            response.headers['Vary'] = 'Accept-Language'
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return error_response(request, 'Задача не найдена', 404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return error_response(request, str(error), 400)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(request, error.detail, error.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_fields(request, error):
        details = []
        for item in error.errors():
            message = item['msg'].removeprefix('Value error, ')
            details.append({'loc': item['loc'], 'type': item['type'],
                            'msg': translate(message, language(request))})
        return JSONResponse({'detail': details}, status_code=422)

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'version': app.version, 'build_id': current_build,
                'data_directory': str(directory), 'bind_host': app.state.bind_host}

    @app.get('/api/session')
    def session():
        return {'token': token, 'profile': store.setting('profile', default_profile),
                'version': app.version, 'data_directory': str(directory)}

    @app.get('/api/system')
    def system():
        return hardware()

    @app.get('/api/tasks')
    def tasks(request: Request):
        return [task_view(task, language(request)) for task in store.list()]

    @app.post('/api/tasks', status_code=201)
    def create(body: NewTask, request: Request):
        values = body.model_dump()
        if 'language' not in body.model_fields_set:
            values['language'] = language(request)
        if body.mode == 'demo':
            workspace = directory / 'demo-workspace'
            workspace.mkdir(exist_ok=True)
            values.update(workspace=str(workspace), backoff_seconds=1,
                          verification=[], auto_permissions=False)
        else:
            workspace = Path(body.workspace).expanduser().resolve()
            if not workspace.is_dir():
                raise ValueError('Укажите существующий каталог проекта (в Docker — путь внутри контейнера)')
            values['workspace'] = str(workspace)
        task = store.create(values)
        state_dir(task)
        return task_view(task, language(request))

    @app.get('/api/tasks/{task_id}')
    def task(task_id: str, request: Request):
        value = task_view(store.get(task_id), language(request))
        return dict(value, checklist=checklist(value), done=read_document(value, 'DONE.md'),
                    documents={name: read_document(value, name) for name in ('GOAL.md', 'PROGRESS.md')})

    @app.patch('/api/tasks/{task_id}')
    def edit(task_id: str, body: TaskEdit, request: Request):
        with app.state.engine.lock:
            current = store.get(task_id)
            if current['status'] in {'succeeded', 'completed_unverified'}:
                raise ValueError('Завершённая задача неизменна. Создайте новую задачу.')
            values = body.model_dump(exclude_none=True)
            if 'goal' in values:
                if not values['goal'].strip():
                    raise ValueError('Цель не может быть пустой')
                values['goal_version'] = current['goal_version'] + 1
            result = store.update(task_id, **values)
            store.event(task_id, 'task_updated', 'Настройки сохранены. Новая цель применяется со следующей итерации.',
                        data={'goal_version': result['goal_version']})
            return task_view(result, language(request))

    @app.post('/api/tasks/{task_id}/{action}')
    def control(task_id: str, action: str, request: Request):
        try:
            with app.state.engine.lock:
                if app.state.restarting:
                    raise HTTPException(503, 'Панель перезапускается')
                return task_view(app.state.engine.control(task_id, action), language(request))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get('/api/tasks/{task_id}/events')
    def events(task_id: str, request: Request, after: int = 0):
        store.get(task_id)
        return [event_view(event, language(request)) for event in store.events(task_id, max(0, after))]

    @app.get('/api/profile')
    def profile():
        return store.setting('profile', default_profile)

    @app.put('/api/profile')
    def save_profile(body: Profile):
        store.save_setting('profile', body.model_dump())
        return body.model_dump()

    @app.get('/api/models')
    def models(request: Request):
        result = runtime.inventory(store.setting('profile', default_profile))
        return model_view(dict(result, benchmark=store.setting('benchmark')), language(request))

    @app.post('/api/models/{action}')
    def model_action(action: str, body: Profile, request: Request):
        if action not in {'load', 'estimate', 'benchmark'}:
            raise HTTPException(404)
        engine = app.state.engine
        if not engine.lock.acquire(blocking=False):
            raise HTTPException(409, 'Операция с моделью уже выполняется')
        try:
            if app.state.restarting:
                raise HTTPException(503, 'Панель перезапускается')
            if engine.busy:
                raise HTTPException(409, 'Сначала поставьте задачу на паузу')
            values = body.model_dump()
            if not values['model']:
                raise ValueError('Сначала выберите модель')
            result = runtime.ensure(values) if action == 'load' else getattr(runtime, action)(values)
            if action == 'benchmark':
                store.save_setting('benchmark', result)
            return model_view(result, language(request))
        except (RuntimeError, TimeoutError, OSError, httpx.HTTPError) as error:
            raise HTTPException(400, str(error)[:2500]) from error
        finally:
            engine.lock.release()

    static = Path(__file__).parent / 'static'
    static.mkdir(exist_ok=True)

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    app.mount('/static', StaticFiles(directory=static), name='static')
    return app


app = create_app()
