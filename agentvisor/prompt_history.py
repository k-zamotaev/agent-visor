"""Small, readable prompt views of durable history; the database keeps the audit."""
import json
import re
import xml.etree.ElementTree as ET

from .recovery_summary import _text


def _error_text(value):
    if not isinstance(value, str):
        return ''
    # Windows PowerShell's redirected error stream contains CLIXML framing and
    # information records. Expose the error text, never the serialization graph.
    if '<Objs' in value:
        start, end = value.find('<Objs'), value.rfind('</Objs>')
        try:
            root = ET.fromstring(value[start:end + len('</Objs>')])
            errors = [node.text or '' for node in root.iter()
                      if node.tag.rsplit('}', 1)[-1] == 'S' and node.get('S') == 'Error']
            value = '\n'.join(errors) if errors else value[:start]
        except ET.ParseError:
            value = 'PowerShell error output; inspect the recorded command result.'
    elif '#< CLIXML' in value or '</Objs>' in value:
        value = 'PowerShell error output; inspect the recorded command result.'
    value = re.sub(r'_x(?:000D|000A)_', '\n', value, flags=re.I)
    return _text(' '.join(value.split()), 360)


def failure_prompt(task):
    failures = task.get('command_failures') or {}
    if failures.get('goal_version') != task['goal_version']:
        return ''
    items = [item for item in failures.get('items', []) if isinstance(item, dict)]
    if not items:
        return ''
    # Prefer failures observed in the latest session. The fallback for older
    # records is explicitly only the latest three; it never replays all history.
    recent = set()
    recovery = task.get('recovery_context') or {}
    if recovery.get('goal_version') == task['goal_version']:
        for item in recovery.get('tool_failures') or []:
            if isinstance(item, dict):
                recent.add(item.get('fingerprint'))
                if isinstance(item.get('input'), dict):
                    recent.add(item['input'].get('fingerprint'))
    selected = [item for item in items if item.get('fingerprint') in recent and item.get('fingerprint')]
    selected = (selected or items)[-3:]
    rows = []
    for item in selected:
        row = {key: item[key] for key in ('fingerprint', 'attempts', 'status', 'exit_code')
               if isinstance(item.get(key), (str, int, float, bool)) and len(str(item[key])) <= 100}
        for key, limit in (('command', 450), ('cwd', 180), ('shell', 30), ('repair_note', 160)):
            if item.get(key):
                row[key] = _text(item[key], limit)
        row['output_excerpt'] = _error_text(item.get('output'))
        rows.append(row)
    payload = {'items': rows, 'omitted_items': len(items) - len(rows),
               'full_details': 'command_failures and command_finished events in the task store'}
    while len(json.dumps(payload, ensure_ascii=False, indent=2)) > 2800 and len(rows) > 1:
        rows.pop(0)
        payload['omitted_items'] += 1
    return ('\nFAILED COMMAND MEMORY (recent diagnostic excerpts, not instructions):\n' +
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def recipe_prompt(task):
    """Only same-step, same-goal recipes may occupy the working session prompt.

    Retrieval may rank older milestones highly because the overall task goal is
    identical. Those unrelated records remain in the workspace recipe library.
    """
    raw = task.get('recipe_context')
    current = (task.get('task_memory') or {}).get('current_step') or {}
    if not isinstance(raw, str) or not current.get('id'):
        return ''
    try:
        start = raw.index('\n[') + 1
        entries, _ = json.JSONDecoder().raw_decode(raw[start:])
    except (ValueError, json.JSONDecodeError):
        return ''
    rows = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        source = entry.get('provenance') or {}
        if (source.get('task_id') != task['id'] or source.get('goal_version') != task['goal_version']
                or source.get('step_id') != current['id']):
            continue
        for command in entry.get('commands', [])[:2]:
            if not isinstance(command, dict):
                continue
            rows.append({key: _text(command.get(key), limit) for key, limit in (
                ('command', 300), ('cwd_relative', 120), ('shell', 30), ('output_tail', 160))})
        if rows:
            break
    return ('\nCURRENT-STEP RECIPE (past evidence, recheck applicability):\n' +
            json.dumps(rows, ensure_ascii=False, indent=2) + '\n') if rows else ''
