"""Group observed failure causes across command variations, without another model call."""
import hashlib
import json
import re


def failure_cause(entry, step):
    # Command text and repair notes are deliberately excluded: changing the wrapper
    # does not change a missing dependency or a failing acceptance check.
    tools = entry.get('tool_failures', [])
    tools = tools[-1:]
    evidence = '\n'.join(str(tool.get(key) or '') for tool in tools
                         for key in ('output', 'error'))
    evidence += '\n' + entry.get('output_tail', '') + '\n' + entry.get('error', '')
    evidence = re.sub(r'\x1b\[[0-9;]*m', '', evidence)[-12000:]
    patterns = (
        ('missing_dependency', r"(?:No module named|Cannot find (?:module|package))\s+['\"]([^'\"]+)"),
        ('missing_command', r"(?:The term|command not found:)\s*['\"]?([^'\"\s]+)"),
        ('port_in_use', r'(?:EADDRINUSE|address already in use)[^\n]*?([\w.\[\]:-]+:\d{2,5})\b'),
        ('connection_refused', r'(?:ECONNREFUSED|connection refused)[^\n]*?([\w.\[\]:-]+:\d{2,5})\b'),
    )
    category, detail = '', ''
    for kind, pattern in patterns:
        match = re.search(pattern, evidence, re.I)
        if match:
            category, detail = kind, match[1].lower()
            break
    if not category:
        # Preserve actual test names, paths and numbers. Only volatile timestamp
        # and PID fields are removed, avoiding broad 'all errors are alike' groups.
        lines = [line.strip() for line in evidence.splitlines()
                 if re.search(r'\b(?:\w+Error|\w+Exception|AssertionError):|^FAILED\s|\berror [A-Z]+\d+:', line)]
        if lines:
            category, detail = 'diagnostic', '\n'.join(lines[-3:])[:1200]
            detail = re.sub(r'\b\d{4}-\d\d-\d\d[T ][\d:.+Z-]+', '<time>', detail)
            detail = re.sub(r'\bpid[=: ]+\d+\b', 'pid=<id>', detail, flags=re.I)
    if not category:
        return None
    arguments = tools[0].get('input') if tools else {}
    cwd = arguments.get('cwd', '') if isinstance(arguments, dict) else ''
    scope = [entry.get('failure_layer'), step, cwd, category, detail]
    return {'key': hashlib.sha256(json.dumps(scope, ensure_ascii=False).encode()).hexdigest()[:20],
            'category': category, 'detail': detail, 'step': step[:1500]}


def detect_loop(entry, history, step):
    cause = failure_cause(entry, step)
    if not cause:
        return None
    matches = [item['failure_cause'] for item in history
               if item.get('failure_cause', {}).get('key') == cause['key']]
    cause['attempts'] = max((item.get('attempts', 1) for item in matches), default=0)
    if not matches or matches[-1].get('iteration') != entry.get('iteration'):
        cause['attempts'] += 1
    cause['iteration'] = entry.get('iteration')
    cause['strategy'] = ('inspect' if cause['attempts'] < 3 else
                         'isolate' if cause['attempts'] < 5 else 'alternative')
    return cause


def strategy_prompt(cause):
    if not cause or cause.get('attempts', 0) < 3:
        return ''
    action = (
        'Reproduce the failure with the smallest bounded probe. Inspect its actual output, '
        'state a falsifiable cause and repair that cause before retrying the original operation.'
        if cause['strategy'] == 'isolate' else
        'The previous diagnosis did not remove the failure. Recheck its assumptions and choose '
        'a materially different implementation or supported tool after inspecting availability. '
        'Keep the original acceptance criteria; do not bypass the failing check.'
    )
    return ('\nSTRATEGY CHANGE REQUIRED: different commands have reproduced the same recorded cause. '
            + action + ' Record the hypothesis, probe result and changed condition in PROGRESS.md. '
            'A renamed command, new explanation or longer timeout alone is not evidence of repair.\n')
