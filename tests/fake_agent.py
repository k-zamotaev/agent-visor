import json
import re
import subprocess
import sys
import time
from pathlib import Path

directory, scenario = Path(sys.argv[1]), sys.argv[2]
version = re.search(r'goal_version: (\d+)', (directory / 'GOAL.md').read_text())[1]
(directory / 'started').write_text('started')
if scenario == 'hang':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (directory / 'child.pid').write_text(str(child.pid))
    time.sleep(60)
if scenario in {'step_hang', 'eof_hang', 'noisy_hang', 'start_chatter', 'active'}:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (directory / 'child.pid').write_text(str(child.pid))
    print(json.dumps({'type': 'step_start', 'part': {}}), flush=True)
    if scenario == 'eof_hang':
        import os
        os.close(1)
        os.close(2)
    if scenario == 'noisy_hang':
        while True:
            print('runtime still alive', file=sys.stderr, flush=True)
            time.sleep(0.05)
    if scenario == 'start_chatter':
        while True:
            print(json.dumps({'type': 'step_start', 'part': {}}), flush=True)
            time.sleep(0.05)
    if scenario == 'active':
        for step in range(30):
            print(json.dumps({'type': 'text', 'part': {'text': f'Working on {step}'}}), flush=True)
            time.sleep(0.1)
        child.terminate()
        child.wait(timeout=5)
    else:
        time.sleep(60)
if scenario == 'error':
    print(json.dumps({'type': 'error', 'error': {'message': 'provider disconnected'}}), flush=True)
    sys.exit(0)
if scenario == 'failure':
    sys.exit(3)
if scenario == 'stall':
    (directory / 'PROGRESS.md').write_text('# Progress\n- [ ] Same step\nUpdated prose ' + str(time.time()))
    sys.exit(0)
if scenario == 'slow':
    time.sleep(1)
(directory / 'PROGRESS.md').write_text('# Progress\n- [x] Implement the task\n- [x] Verify result\n')
(directory / 'DONE.md').write_text(f'goal_version: {"0" if scenario == "stale" else version}\nComplete\n')
print(json.dumps({'type': 'step_finish', 'part': {'reason': 'stop', 'tokens': {'output': 17}}}), flush=True)
