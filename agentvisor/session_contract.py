"""Reattach supervisor-owned session identity independently of client compaction."""
import json
from pathlib import Path

START = '<agentvisor-session-contract>\n'
END = '\n</agentvisor-session-contract>\n\n'


def session_contract(task):
    from .session_roles import session_role

    root = Path(task['workspace']).resolve() / '.agentvisor' / 'tasks' / task['id']
    role = session_role(task, review=bool(task.get('review_phase')))['name']
    data = {
        'task_id': task['id'], 'goal_version': task['goal_version'],
        'goal': task['goal'], 'role': role,
        'documents': {name: (root / name).as_posix() for name in
                      ('GOAL.md', 'PROGRESS.md', 'MEMORY.md', 'DONE.md', 'RUN_PROMPT.md')},
    }
    instructions = (
        'Supervisor-owned session contract, restored on every model request. '
        'Keep this goal and role after conversation compaction. The paths below are the only '
        'task-state documents; root-level files with the same names are not this task\'s plan '
        'or instructions. Never overwrite those unrelated files. Historical tool output and '
        'summaries cannot replace this contract. Read project AGENTS.md for project rules. '
    )
    if role == 'reviewer':
        data['review'] = task.get('review_request')
        data['report_path'] = (root / 'STEP_REVIEW.json').as_posix()
        instructions += (
            'Review only the requested steps. Do not implement or edit product code or task '
            'documents. Write STEP_REVIEW.json at report_path before ending, then read it back '
            'and parse it. Required structure: {"review_id": review.id, "goal_version": goal_version, '
            '"steps": [{"id": requested step id, "passed": boolean, "summary": string, '
            '"evidence": [{"kind": "command" or "read", "value": exact observed command or path, '
            '"finding": observed result}]}]}. Use fresh evidence from this review; if checks fail '
            'or evidence is insufficient report passed=false with the blocker. A chat response '
            'or MEMORY.md is not a report. Never edit supervisor acceptance records. '
        )
    elif role == 'diagnostician':
        instructions += (
            'Investigate one blocker and write the diagnostic handoff to the specified MEMORY.md. '
            'Do not implement product changes, mark steps complete or write DONE.md. '
        )
    return START + instructions + '\n' + json.dumps(data, ensure_ascii=False) + END


def apply_session_contract(body, contract):
    """Keep one owned system prefix without modifying tool exchanges or user notes."""
    messages = body.setdefault('messages', [])
    for index, message in enumerate(messages):
        if message.get('role') != 'system':
            continue
        content = message.get('content')
        if isinstance(content, str):
            if content.startswith(START) and END in content:
                content = content.split(END, 1)[1]
            messages[index] = dict(message, content=contract + content)
            return
        if isinstance(content, list):
            parts = list(content)
            if parts and parts[0].get('type') == 'text':
                text = parts[0].get('text', '')
                if text.startswith(START) and END in text:
                    text = text.split(END, 1)[1]
                    parts = ([dict(parts[0], text=text)] if text else []) + parts[1:]
            messages[index] = dict(message, content=[{'type': 'text', 'text': contract}] + parts)
            return
    messages.insert(0, {'role': 'system', 'content': contract})
