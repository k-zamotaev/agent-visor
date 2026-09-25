"""Bootstrap a local environment and start AgentVisor on loopback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import threading
import urllib.request
import venv
import webbrowser


def main():
    parser = argparse.ArgumentParser(description='Run AgentVisor locally')
    parser.add_argument('--port', type=int)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise SystemExit('AgentVisor requires Python 3.10 or newer (3.12 recommended).')
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    root = Path(__file__).resolve().parent
    os.chdir(root)
    from agentvisor.version import build_id
    fingerprint_source = build_id()
    data_directory = Path(os.environ.get('AGENTVISOR_DATA', '.agentvisor-data/runtime')).resolve()
    ports = [args.port] if args.port else range(8420, 8441)
    available = []
    for port in ports:
        candidate = f'http://127.0.0.1:{port}'
        try:
            with urllib.request.urlopen(candidate + '/api/health', timeout=0.15) as response:
                health = json.load(response)
            if (health.get('build_id') == fingerprint_source
                    and health.get('data_directory') == str(data_directory)):
                print(f'AgentVisor is already running: {candidate}')
                if not args.no_browser:
                    webbrowser.open(candidate)
                return 0
        except (OSError, ValueError):
            pass
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
                available.append(port)
            except OSError:
                pass
    if not available:
        raise SystemExit('No free local port. Specify another with --port.')
    port = available[0]
    executable = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.exists():
        print('Creating local Python environment...', flush=True)
        venv.EnvBuilder(with_pip=True).create(root / '.venv')
    requirements = root / 'requirements.txt'
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = root / '.venv' / 'agentvisor-requirements.sha256'
    if not marker.exists() or marker.read_text() != fingerprint:
        print('Installing AgentVisor dependencies...', flush=True)
        subprocess.run([str(executable), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
        marker.write_text(fingerprint)
    url = f'http://127.0.0.1:{port}'
    print(f'AgentVisor: {url}\nPress Ctrl+C to stop. Running tasks will be paused.', flush=True)

    def open_when_ready():
        import time
        for _ in range(40):
            try:
                with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except OSError:
                time.sleep(0.25)

    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    child = subprocess.Popen([str(executable), '-m', 'uvicorn', 'agentvisor.app:app',
                              '--host', '127.0.0.1', '--port', str(port)])
    try:
        return child.wait()
    except KeyboardInterrupt:
        try:
            return child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.terminate()
            return child.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
