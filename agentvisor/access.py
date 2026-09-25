"""Network authentication and listener settings exposed by the dashboard."""
import secrets
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .network import COOKIE, is_loopback, local_addresses, read_network, write_network


class NetworkEdit(BaseModel):
    host: Literal['127.0.0.1', '0.0.0.0']


class Login(BaseModel):
    code: str = Field(min_length=1, max_length=256)


def authenticated(request):
    if request.client and is_loopback(request.client.host):
        return True
    expected = request.app.state.network['access_code']
    supplied = request.cookies.get(COOKIE, '')
    if request.headers.get('authorization', '').startswith('Bearer '):
        supplied = request.headers['authorization'][7:]
    return bool(expected) and secrets.compare_digest(supplied.encode(), expected.encode())


def register_network(app, directory):
    app.state.network = read_network(directory)
    app.state.bind_host = app.state.network['host']
    app.state.bind_port = 8420
    app.state.bind_override = False
    app.state.request_restart = None
    app.state.restarting = False

    def snapshot(request):
        desired = read_network(directory)
        host = app.state.bind_host if app.state.bind_override else desired['host']
        return {'host': host, 'active_host': app.state.bind_host,
                'restart_required': host != app.state.bind_host,
                'managed': callable(app.state.request_restart),
                'bind_override': app.state.bind_override,
                'authentication_required': not (request.client and is_loopback(request.client.host)),
                'access_code': app.state.network['access_code'],
                'addresses': [f'http://{address}:{app.state.bind_port}' for address in local_addresses()],
                'local_url': f'http://127.0.0.1:{app.state.bind_port}'}

    @app.post('/api/auth/login')
    def login(body: Login, request: Request):
        expected = app.state.network['access_code']
        if not expected or not secrets.compare_digest(body.code.strip().encode(), expected.encode()):
            raise HTTPException(401, 'Неверный код доступа')
        response = JSONResponse({'status': 'ok'})
        response.set_cookie(COOKIE, expected, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=7 * 86400)
        return response

    @app.post('/api/auth/logout')
    def logout():
        response = JSONResponse({'status': 'ok'})
        response.delete_cookie(COOKIE)
        return response

    @app.get('/api/network')
    def network(request: Request):
        return snapshot(request)

    @app.put('/api/network')
    def save_network(body: NetworkEdit, request: Request):
        with app.state.engine.lock:
            if app.state.restarting:
                raise HTTPException(503, 'Панель перезапускается')
            if app.state.bind_override and body.host != app.state.bind_host:
                raise HTTPException(409, 'Адрес задан параметром запуска. Измените его в конфигурации запуска.')
            value = read_network(directory)
            value['host'] = body.host
            write_network(directory, value)
            return snapshot(request)

    @app.post('/api/restart')
    def restart():
        engine = app.state.engine
        if not engine.lock.acquire(blocking=False):
            raise HTTPException(409, 'Операция с моделью уже выполняется')
        try:
            if engine.busy:
                raise HTTPException(409, 'Сначала поставьте задачу на паузу')
            if app.state.restarting:
                raise HTTPException(409, 'Панель уже перезапускается')
            if not callable(app.state.request_restart):
                raise HTTPException(409, 'Для применения настроек перезапустите AgentVisor через скрипт запуска.')
            app.state.restarting = True
            return JSONResponse({'status': 'restarting'},
                                background=BackgroundTask(app.state.request_restart))
        finally:
            engine.lock.release()
