"""Start llmster and its HTTP server independently of agent process lifetimes."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from urllib.parse import urlparse

import httpx
import psutil

from .processes import capture, executable


def parse_cli_json(text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in '[{':
            try:
                return decoder.raw_decode(text[index:])[0]
            except json.JSONDecodeError:
                pass
    raise ValueError('CLI не вернул JSON')


def local(profile):
    return urlparse(profile['base_url']).hostname in {'localhost', '127.0.0.1', '::1'}


def check_cancel(cancel):
    if cancel and cancel.is_set():
        raise InterruptedError('Операция отменена')


class LMStudioService:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('AGENTVISOR_DATA', '.agentvisor-data/runtime'))
        self.lock = threading.RLock()
        self.stage = ''

    def notify(self, message, report=None):
        self.stage = message
        if report:
            report(message)

    def command(self, *args, cancel=None, timeout=60):
        cli = executable('lms')
        if not cli:
            raise RuntimeError('LM Studio CLI не установлен')
        return capture([cli, *args], timeout=timeout, cancel=cancel, service=True)

    def daemon(self):
        if not executable('lms'):
            return {'status': 'not-installed'}
        return parse_cli_json(self.command('daemon', 'status', '--json', timeout=10))

    def matches(self, profile):
        if not local(profile) or self.daemon().get('status') != 'running':
            return False
        server = parse_cli_json(self.command('server', 'status', '--json', timeout=10))
        parsed = urlparse(profile['base_url'])
        return parsed.scheme == 'http' and server.get('running') and server.get('port') == (parsed.port or 80)

    def owned(self, daemon):
        if not daemon.get('isDaemon') or daemon.get('status') != 'running':
            return False
        try:
            owner = json.loads((self.directory / 'lmstudio-owner.json').read_text(encoding='utf-8'))
            return (owner['pid'] == daemon['pid'] and
                    abs(psutil.Process(daemon['pid']).create_time() - owner['created']) < 0.1)
        except (OSError, ValueError, KeyError, psutil.Error):
            return False

    def remember(self, daemon):
        if daemon.get('isDaemon') and daemon.get('pid'):
            owner = {'pid': daemon['pid'], 'created': psutil.Process(daemon['pid']).create_time()}
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / 'lmstudio-owner.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(owner), encoding='utf-8')
            temporary.replace(target)

    def status(self, profile, online=False):
        if profile['runtime'] != 'lmstudio' or not local(profile):
            return {'kind': 'remote', 'manageable': False, 'owned': False, 'online': online,
                    'stage': '', 'error': None}
        try:
            daemon = self.daemon()
            kind = ('headless' if daemon.get('isDaemon') else 'desktop') if daemon['status'] == 'running' else daemon['status']
            return {'kind': kind, 'manageable': True, 'owned': self.owned(daemon),
                    'online': online, 'stage': self.stage, 'error': None}
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            return {'kind': 'error', 'manageable': True, 'owned': False, 'online': online,
                    'stage': self.stage, 'error': str(error)}

    def install(self, cancel=None, report=None):
        check_cancel(cancel)
        destination = Path.home() / '.lmstudio'
        pointer = Path.home() / '.lmstudio-home-pointer'
        if pointer.is_file():
            destination = Path(pointer.read_text(encoding='utf-8').strip())
        while not destination.exists() and destination != destination.parent:
            destination = destination.parent
        if shutil.disk_usage(destination).free < 3 * 1024**3:
            raise RuntimeError('Для установки LM Studio требуется минимум 3 ГБ свободного места')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.notify('Установка headless LM Studio с официального сайта...', report)
        suffix = 'ps1' if os.name == 'nt' else 'sh'
        with tempfile.TemporaryDirectory(prefix='llmster-install-', dir=self.directory) as directory:
            script = Path(directory) / ('install.' + suffix)
            with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=True) as client:
                for attempt in range(3):
                    check_cancel(cancel)
                    try:
                        response = client.get('https://lmstudio.ai/install.' + suffix)
                        break
                    except httpx.TransportError:
                        if attempt == 2:
                            raise
                        if cancel:
                            cancel.wait(2)
                        else:
                            time.sleep(2)
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    raise RuntimeError('Неожиданный размер установщика LM Studio')
                script.write_bytes(response.content)
            check_cancel(cancel)
            if os.name == 'nt':
                shell = executable('powershell') or executable('pwsh')
                if not shell:
                    raise RuntimeError('PowerShell не найден для установки LM Studio')
                argv = [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
            else:
                argv = ['sh', str(script), '--no-modify-path']
            capture(argv, timeout=900, cancel=cancel, service=True,
                    env=dict(os.environ, LMS_NO_MODIFY_PATH='1', LMS_PRINT_QUIET='1',
                             TEMP=directory, TMP=directory, TMPDIR=directory))
        if not executable('lms'):
            raise RuntimeError('Установщик завершился, но LM Studio CLI не найден')

    @staticmethod
    def probe(profile, request):
        try:
            value = request(profile, '/api/v1/models', timeout=2)
            if not isinstance(value, dict) or not isinstance(value.get('models'), list):
                raise RuntimeError('Адрес не вернул список моделей LM Studio')
            return True
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                raise RuntimeError('LM Studio отклонил доступ. Проверьте AGENTVISOR_MODEL_TOKEN.') from error
            raise

    def ensure(self, profile, request, cancel=None, report=None, force=False):
        if profile['runtime'] != 'lmstudio' or not local(profile):
            return
        with self.lock:
            check_cancel(cancel)
            if self.probe(profile, request):
                self.notify('Сервер LM Studio готов', report)
                return
            if not force and not profile.get('manage_runtime', True):
                raise RuntimeError('Автозапуск LM Studio выключен в профиле')
            try:
                if not executable('lms'):
                    self.install(cancel, report)
                daemon = self.daemon()
                if daemon.get('status') != 'running':
                    self.notify('Запуск headless LM Studio...', report)
                    daemon = parse_cli_json(self.command('daemon', 'up', '--json', cancel=cancel, timeout=180))
                    self.remember(daemon)
                check_cancel(cancel)
                server = parse_cli_json(self.command('server', 'status', '--json', cancel=cancel))
                parsed = urlparse(profile['base_url'])
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                if parsed.scheme != 'http':
                    raise RuntimeError('Для автоматического запуска LM Studio укажите локальный HTTP-адрес')
                if server.get('running') and server.get('port') != port:
                    raise RuntimeError(f'LM Studio уже слушает порт {server.get("port")}. Укажите этот порт в профиле.')
                self.notify('Запуск API LM Studio...', report)
                bind = '::1' if parsed.hostname == '::1' else '127.0.0.1'
                self.command('server', 'start', '--port', str(port), '--bind', bind, cancel=cancel)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    check_cancel(cancel)
                    if self.probe(profile, request):
                        self.notify('Сервер LM Studio готов', report)
                        return
                    if cancel:
                        cancel.wait(0.3)
                    else:
                        time.sleep(0.3)
                raise TimeoutError('API LM Studio не стал доступен после запуска')
            except Exception as error:
                self.stage = str(error)
                raise

    def stop(self, profile, request):
        if profile['runtime'] != 'lmstudio' or not local(profile):
            raise RuntimeError('Остановка доступна только для локального headless LM Studio')
        with self.lock:
            if not self.owned(self.daemon()):
                raise RuntimeError('Этот сервис LM Studio запущен вне AgentVisor')
            server = parse_cli_json(self.command('server', 'status', '--json', timeout=10))
            if server.get('running') and server.get('port') != (urlparse(profile['base_url']).port or 80):
                raise RuntimeError(f'LM Studio уже слушает порт {server.get("port")}. Укажите этот порт в профиле.')
            rows = parse_cli_json(self.command('ps', '--json', timeout=15))
            if any(not row.get('identifier', '').startswith('agentvisor-') for row in rows):
                raise RuntimeError('В LM Studio загружены модели других приложений. Сервис оставлен работающим.')
            self.command('daemon', 'down', timeout=60)
            if self.daemon().get('status') == 'running':
                raise RuntimeError('LM Studio не подтвердил остановку')
            (self.directory / 'lmstudio-owner.json').unlink(missing_ok=True)
            self.notify('Headless LM Studio остановлен')
            return {'text': self.stage}
