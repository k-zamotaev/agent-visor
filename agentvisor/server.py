"""Run the dashboard; a managed restart exits for the launcher or Docker to restart it."""
import argparse
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .i18n import translate
from .network import HOSTS, read_network


RESTART_EXIT_CODE = 75


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8420)
    parser.add_argument('--host', choices=HOSTS)
    parser.add_argument('--language', choices=['ru', 'en'], default='ru')
    args = parser.parse_args()
    directory = Path(os.environ.get('AGENTVISOR_DATA', '.agentvisor-data/runtime')).resolve()
    host = args.host or read_network(directory)['host']
    app = create_app(directory)
    app.state.bind_host = host
    app.state.bind_port = args.port
    app.state.bind_override = args.host is not None
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=args.port, use_colors=False,
                                          proxy_headers=False, timeout_graceful_shutdown=15))
    restart_requested = False

    def request_restart():
        nonlocal restart_requested
        restart_requested = True
        server.should_exit = True

    if os.environ.get('AGENTVISOR_MANAGED_RESTART') == '1':
        app.state.request_restart = request_restart

    if os.environ.get('AGENTVISOR_CONTAINER') == '1':
        def startup_notice():
            message = 'Код доступа к панели: ' + app.state.network['access_code']
            print(translate(message, args.language), flush=True)
        app.state.startup_notice = startup_notice

    server.run()
    if restart_requested:
        return RESTART_EXIT_CODE
    return 0 if server.started else 1


if __name__ == '__main__':
    raise SystemExit(main())
