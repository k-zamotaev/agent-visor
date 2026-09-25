"""Opt-in real LM Studio unload/recovery check with an isolated, controlled agent."""
import argparse
import json
from pathlib import Path
import sys
import time

from agentvisor.models import ModelRuntime
from agentvisor.store import Store
from agentvisor.supervisor import Supervisor
from agentvisor.tasks import NewTask, Profile, state_dir


def wait_for(predicate, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise TimeoutError('Watchdog smoke check did not complete in time')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--context', type=int, default=32768)
    args = parser.parse_args()
    root = (Path('.agentvisor-data') / ('watchdog-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    store = Store(root / 'state')
    runtime = ModelRuntime(root / 'runtime')
    profile = Profile(model=args.model, profile_mode='manual', context=args.context,
                      flash_attention='on', cache_type_k='q8_0', cache_type_v='q8_0')
    task = store.create(NewTask(name='Real unload recovery', workspace=str(workspace),
                        goal='Preserve work and recover after a real model unload.', profile=profile,
                        max_iterations=2, max_failures=2, timeout_seconds=120, backoff_seconds=0.1,
                        verification=[sys.executable, '-c',
                            'from pathlib import Path; assert Path("saved-work.txt").read_text()=="preserved"']).model_dump())
    fake = Path(__file__).with_name('fake_agent.py').resolve()

    def command(current):
        if current['iteration'] == 1:
            (workspace / 'saved-work.txt').write_text('preserved')
        return [sys.executable, str(fake), str(state_dir(current)),
                'hang' if current['iteration'] == 1 else 'complete']

    engine = Supervisor(store, runtime, command)
    try:
        engine.start(task['id'])
        wait_for(lambda: store.get(task['id']).get('runtime_health', {}).get('ok'))
        before = store.get(task['id'])
        assert runtime.health(before['resolved_profile'], before['runtime_instance']) is None
        print('Unloading the active model through the real LM Studio API...', flush=True)
        unloaded_at = time.monotonic()
        runtime.unload(before['resolved_profile'])
        wait_for(lambda: not engine.busy)
        result = store.get(task['id'])
        events = store.events(task['id'], limit=500)
        report = {'task': result, 'events': events, 'recovery_seconds': time.monotonic() - unloaded_at}
        (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        assert result['status'] == 'succeeded', result['reason']
        assert result['recoveries'] == 1 and result['iteration'] == 2
        assert any(event['kind'] == 'runtime_lost' for event in events)
        assert runtime.health(result['resolved_profile'], result['runtime_instance']) is None
        print(json.dumps({'status': result['status'], 'recoveries': result['recoveries'],
                          'recovery_seconds': round(report['recovery_seconds'], 1), 'report': str(root / 'result.json')}))
    finally:
        engine.shutdown()


if __name__ == '__main__':
    main()
