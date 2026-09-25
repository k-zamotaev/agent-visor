"""Opt-in real-model smoke test in an isolated workspace; not collected by pytest."""
import argparse
import json
from pathlib import Path
import sys
import time

from agentvisor.models import ModelRuntime
from agentvisor.store import Store
from agentvisor.supervisor import Supervisor
from agentvisor.tasks import NewTask, Profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--runtime', default='lmstudio', choices=['lmstudio', 'ollama'])
    parser.add_argument('--url', default='http://127.0.0.1:1234')
    parser.add_argument('--context', type=int, default=16384)
    parser.add_argument('--auto-profile', action='store_true')
    args = parser.parse_args()
    root = Path('.agentvisor-data') / ('smoke-' + time.strftime('%Y%m%d-%H%M%S'))
    workspace = root.resolve() / 'workspace'
    workspace.mkdir(parents=True)
    store = Store(root / 'data')
    task = store.create(NewTask(
        name='Проверка реального OpenCode', workspace=str(workspace),
        goal='Create greeting.txt with exactly "AgentVisor ready" followed by one newline. '
             'Use a one-step checklist in the supplied PROGRESS.md. Read the file to check it. '
             'Then write DONE.md as requested. Do not install packages or access the network.',
        profile=Profile(runtime=args.runtime, base_url=args.url, model=args.model,
                        profile_mode='auto' if args.auto_profile else 'manual',
                        context=args.context, output_limit=2048),
        max_iterations=4, timeout_seconds=180, max_failures=2,
        max_hours=0.2, backoff_seconds=1, auto_permissions=True,
        verification=[sys.executable, '-c',
                      'from pathlib import Path; assert Path("greeting.txt").read_bytes() == b"AgentVisor ready\\n"']
    ).model_dump())
    engine = Supervisor(store, ModelRuntime(root / 'runtime'))
    engine.start(task['id'])
    last = None
    deadline = time.monotonic() + 400
    while engine.busy and time.monotonic() < deadline:
        value = store.get(task['id'])
        if (value['status'], value['iteration']) != last:
            print(value['status'], value['iteration'], flush=True)
            last = (value['status'], value['iteration'])
        time.sleep(0.5)
    if engine.busy:
        engine.shutdown()
    result = store.get(task['id'])
    report = {'task': result, 'events': store.events(task['id'], limit=500)}
    path = root / 'result.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'reason': result['reason'],
                      'report': str(path.resolve())}, ensure_ascii=True), flush=True)
    return 0 if result['status'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
