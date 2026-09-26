"""Opt-in: healthy LM Studio + silent agent, followed by real OpenCode repair."""
import argparse
import json
from pathlib import Path
import sys
import time

import psutil

from agentvisor.models import ModelRuntime
from agentvisor.store import Store
from agentvisor.supervisor import Supervisor
from agentvisor.tasks import NewTask, Profile, state_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--context', type=int, default=65536)
    args = parser.parse_args()
    root = (Path('.agentvisor-data') / ('idle-recovery-' + time.strftime('%Y%m%d-%H%M%S'))).resolve()
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    store = Store(root / 'state')
    runtime = ModelRuntime(root / 'runtime')
    profile = Profile(model=args.model, profile_mode='manual', context=args.context, output_limit=4096)
    task = store.create(NewTask(
        name='Real idle recovery', workspace=str(workspace),
        goal='Create recovered.txt with exactly "AgentVisor recovered" and a newline. '
             'Read it back and record a one-step completed checklist and DONE.md. '
             'Do not install packages or access the network.',
        profile=profile, max_iterations=4, max_failures=1, stall_limit=2,
        timeout_seconds=180, idle_timeout_seconds=30, backoff_seconds=0.1,
        auto_permissions=True, autonomous_recovery=True,
        verification=[sys.executable, '-c',
                      'from pathlib import Path; assert Path("recovered.txt").read_bytes()==b"AgentVisor recovered\\n"']
    ).model_dump())
    engine = Supervisor(store, runtime)
    real_command = engine.command

    def command(current, prompt, ready):
        if current['iteration'] == 1:
            return [sys.executable, str(Path(__file__).with_name('fake_agent.py').resolve()),
                    str(state_dir(current)), 'step_hang']
        (root / f'prompt-{current["iteration"]}.txt').write_text(prompt, encoding='utf-8')
        return real_command(current, prompt, ready)

    engine.command = command
    try:
        engine.start(task['id'])
        deadline, cursor = time.monotonic() + 480, 0
        while engine.busy and time.monotonic() < deadline:
            for event in store.events(task['id'], after=cursor):
                cursor = event['id']
                if event['kind'] in {'running', 'agent_idle', 'recovering', 'succeeded', 'blocked'}:
                    print(event['kind'], event['message'], flush=True)
            time.sleep(0.2)
        if engine.busy:
            raise TimeoutError('Real recovery check exceeded its deadline')
        result, events = store.get(task['id']), store.events(task['id'], limit=1000)
        child = int((state_dir(task) / 'child.pid').read_text())
        child_stopped = not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        report = {'task': result, 'events': events, 'child_stopped': child_stopped}
        (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        assert result['status'] == 'succeeded', result['reason']
        assert child_stopped and any(event['kind'] == 'agent_idle' for event in events)
        assert not any(event['kind'] == 'runtime_lost' for event in events)
        assert result['iteration'] >= 2 and result['recoveries'] >= 1
        assert result['generation_sample']['tokens_per_second'] > 0
        assert any(event['kind'] == 'reasoning' for event in events)
        print(json.dumps({'status': result['status'], 'iteration': result['iteration'],
                          'recoveries': result['recoveries'], 'report': str(root / 'result.json')}), flush=True)
    finally:
        engine.shutdown()


if __name__ == '__main__':
    main()
