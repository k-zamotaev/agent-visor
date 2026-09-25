"""Bootstrap AgentVisor and reopen the instance that owns its data directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
import urllib.request
import venv
import webbrowser

from agentvisor.i18n import translate
from agentvisor.instance import InstanceInUseError, lock_instance
from agentvisor.version import build_id
from agentvisor.network import read_network


DEFAULT_PORTS = range(8420, 8441)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_health(url):
    try:
        with HTTP.open(url + '/api/health', timeout=0.2) as response:
            value = json.loads(response.read(65536))
        if isinstance(value, dict) and value.get('status') == 'ok':
            return value
    except (OSError, ValueError):
        pass
    return None


def owns_directory(health, directory):
    value = health.get('data_directory') if health else None
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).resolve() == directory.resolve()
    except (OSError, ValueError):
        return False


def discovery_ports(directory, requested_port):
    ports = []
    try:
        port = json.loads((directory / 'launcher.json').read_text(encoding='utf-8'))['port']
        if type(port) is int and 1024 <= port <= 65535:
            ports.append(port)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if requested_port is not None:
        ports.append(requested_port)
    return list(dict.fromkeys([*ports, *DEFAULT_PORTS]))


def find_existing(directory, ports):
    for port in ports:
        url = f'http://127.0.0.1:{port}'
        health = read_health(url)
        if owns_directory(health, directory):
            return url, health
    return None


def reopen(existing, fingerprint, no_browser, say):
    url, health = existing
    say(f'AgentVisor уже работает: {url}')
    if health.get('build_id') != fingerprint:
        say('Исходники обновлены. Для применения изменений остановите работающий AgentVisor через Ctrl+C и запустите его снова.')
    if not no_browser:
        webbrowser.open(url)
    return 0


def choose_port(ports, host='127.0.0.1'):
    for port in ports:
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                pass
    return None


def bootstrap(root, say):
    executable = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.exists():
        say('Создание окружения Python...')
        venv.EnvBuilder(with_pip=True).create(root / '.venv')
    requirements = root / 'requirements.txt'
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = root / '.venv' / 'agentvisor-requirements.sha256'
    if not marker.exists() or marker.read_text() != fingerprint:
        say('Установка зависимостей AgentVisor...')
        subprocess.run([str(executable), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
        marker.write_text(fingerprint)
    return executable


def stop_child(child):
    if child.poll() is not None:
        return
    if os.name == 'nt':
        # The venv executable may own another Python process on Windows.
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def wait_ready(child, url, directory, fingerprint, timeout=30):
    deadline = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < deadline:
        health = read_health(url)
        if owns_directory(health, directory) and health.get('build_id') == fingerprint:
            return True
        time.sleep(0.1)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run AgentVisor locally')
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--language', choices=['ru', 'en'], default='ru')
    args = parser.parse_args(argv)
    say = lambda message: print(translate(message, args.language), flush=True)
    if sys.version_info < (3, 10):
        parser.error('AgentVisor requires Python 3.10 or newer (3.12 recommended).')
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    root = Path(__file__).resolve().parent
    os.chdir(root)
    fingerprint = build_id()
    directory = Path(os.environ.get('AGENTVISOR_DATA', '.agentvisor-data/runtime')).resolve()
    ports = discovery_ports(directory, args.port)
    existing = find_existing(directory, ports)
    if existing:
        return reopen(existing, fingerprint, args.no_browser, say)
    try:
        with lock_instance(directory):
            pass
    except InstanceInUseError as error:
        # Another launcher may still be bringing its server online.
        existing = find_existing(directory, ports)
        if existing:
            return reopen(existing, fingerprint, args.no_browser, say)
        say(str(error))
        say('Панель не найдена. Дождитесь запуска или закройте прежний AgentVisor через Ctrl+C. Не удаляйте файл блокировки.')
        return 1
    except OSError as error:
        say(f'Не удалось открыть каталог данных: {directory}')
        print(str(error), file=sys.stderr)
        return 1
    try:
        host = read_network(directory)['host']
    except (OSError, ValueError) as error:
        say(str(error))
        return 1
    port = choose_port([args.port] if args.port is not None else DEFAULT_PORTS, host)
    if port is None:
        say('Нет свободного локального порта. Укажите другой порт через --port.')
        return 1
    executable = bootstrap(root, say)
    while True:
        result = serve_once(executable, port, directory, build_id(), args, say)
        if result != 75:
            return result
        say('Перезапуск панели с сохранёнными сетевыми настройками...')


def serve_once(executable, port, directory, fingerprint, args, say):
    url = f'http://127.0.0.1:{port}'
    say('Запуск AgentVisor...')
    child = subprocess.Popen([str(executable), '-m', 'agentvisor.server',
                              '--port', str(port), '--language', args.language],
                             env=dict(os.environ, AGENTVISOR_MANAGED_RESTART='1'))
    try:
        if not wait_ready(child, url, directory, fingerprint):
            if child.poll() == 75:
                # A client can request another restart before readiness is observed.
                return 75
            stop_child(child)
            existing = find_existing(directory, discovery_ports(directory, port))
            if existing:
                return reopen(existing, fingerprint, args.no_browser, say)
            say('AgentVisor не запустился. Причина указана в журнале выше.')
            return 1
        cache = directory / f'launcher-{os.getpid()}.tmp'
        try:
            cache.write_text(json.dumps({'port': port}), encoding='utf-8')
            cache.replace(directory / 'launcher.json')
        except OSError:
            # Discovery in the default port range still works if caching fails.
            try:
                cache.unlink(missing_ok=True)
            except OSError:
                pass
        say(f'AgentVisor: {url}')
        if read_network(directory)['host'] == '0.0.0.0':
            say('Доступ: все сетевые интерфейсы (0.0.0.0). Ссылки и код доступа — в настройках панели.')
        else:
            say('Доступ: только этот компьютер (127.0.0.1).')
        say('Для остановки нажмите Ctrl+C. Выполняющиеся задачи будут приостановлены.')
        if not args.no_browser:
            webbrowser.open(url)
        return child.wait()
    except KeyboardInterrupt:
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stop_child(child)
        return 0
    finally:
        stop_child(child)


if __name__ == '__main__':
    raise SystemExit(main())
