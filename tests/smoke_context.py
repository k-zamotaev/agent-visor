"""Opt-in delivery of a user addition during a real OpenCode iteration."""
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
    args = parser.parse_args()
    root = (Path('.agentvisor-data') / ('context-smoke-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    store = Store(root / 'state')
    task = store.create(NewTask(
        name='Live context smoke', workspace=str(workspace),
        goal='Create main.txt containing exactly "ready" and a newline. Read it back, '
             'record a checklist and DONE.md. Do not install anything or use the network.',
        profile=Profile(model=args.model, profile_mode='manual', context=65536, output_limit=4096),
        auto_permissions=True, max_iterations=3, timeout_seconds=180,
        idle_timeout_seconds=60, backoff_seconds=0.1,
        verification=[sys.executable, '-c', 'from pathlib import Path; '
                      'assert Path("main.txt").read_bytes()==b"ready\\n"; '
                      'assert Path("user-note.txt").read_bytes()==b"context received\\n"']
    ).model_dump())
    engine = Supervisor(store, ModelRuntime(root / 'runtime'))
    sent = False
    try:
        engine.start(task['id'])
        deadline = time.monotonic() + 360
        while engine.busy and time.monotonic() < deadline:
            current = store.get(task['id'])
            if not sent and current.get('generation_sample'):
                store.add_context(task['id'], 'Additional requirement: also create user-note.txt '
                                  'containing exactly "context received" plus one newline. '
                                  'Read it back and include this in the checklist before writing DONE.md.')
                sent = True
                print('Added context during iteration ' + str(current['iteration']), flush=True)
            time.sleep(0.1)
        result = store.get(task['id'])
        report = {'task': result, 'events': store.events(task['id'], limit=1000)}
        (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        assert not engine.busy and result['status'] == 'succeeded', result['reason']
        assert sent and result['applied_context_version'] == result['context_version'] == 1
        assert result['goal_version'] == 1 and result['generation_sample']['tokens_per_second'] > 0
        print(json.dumps({'status': result['status'], 'iteration': result['iteration'],
                          'context_version': result['applied_context_version'],
                          'generation_tps': result['generation_sample']['tokens_per_second'],
                          'report': str(root / 'result.json')}), flush=True)
    finally:
        engine.shutdown()


if __name__ == '__main__':
    main()
