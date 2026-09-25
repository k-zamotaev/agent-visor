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
