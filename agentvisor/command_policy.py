"""Preserve existing OpenCode command restrictions when changing tool transport."""
import os
import base64
import fnmatch
import json
from pathlib import Path
import re
import shutil
import time

from .models import parse_cli_json
from .processes import capture, executable
from .tasks import document_path


ACTIONS = {'allow', 'ask', 'deny'}


def decision(permission, reason='', denied_patterns=None, external_directory='ask'):
    return {'allowed': permission != 'deny', 'permission': permission, 'reason': reason,
            'denied_patterns': denied_patterns or [], 'external_directory': external_directory}


def _action(value, inherited):
    if isinstance(value, str) and value in ACTIONS:
        return decision(value)
    if not isinstance(value, dict):
        return decision('deny', 'Unsupported OpenCode command permission format')
    base = value.get('*', inherited)
    if base not in ACTIONS:
        return decision('deny', 'Unsupported OpenCode command permission action')
    denied = []
    for pattern, action in value.items():
        if action not in ACTIONS:
            return decision('deny', 'Unsupported OpenCode command permission action')
        if pattern != '*':
            if action != 'deny' or any(char in pattern for char in '?[\\\n\r'):
                return decision('deny', 'Only simple deny patterns can be transferred to native commands')
            denied.append(pattern)
    return decision(base, denied_patterns=denied)


def _configured(permission, inherited='ask'):
    if permission is None:
        return decision(inherited)
    if isinstance(permission, str):
        return _action(permission, inherited)
    if not isinstance(permission, dict):
        return decision('deny', 'Unsupported OpenCode permission format')
    # Do not invent a glob matcher for tool selectors or shell commands.
    if any(key != '*' and any(char in str(key) for char in '*?[') for key in permission):
        return decision('deny', 'Pattern-based OpenCode tool restrictions require the original permission evaluator')
    base = permission.get('*', inherited)
    result = _action(permission.get('bash', base), inherited)
    external = permission.get('external_directory', permission.get('*', 'ask'))
    if isinstance(external, dict):
        external = 'deny' if 'deny' in external.values() else external.get('*', 'ask')
    result['external_directory'] = external if isinstance(external, str) and external in ACTIONS else 'deny'
    return result


def _effective_rules(rules):
    if not isinstance(rules, list):
        return decision('deny', 'Cannot read effective OpenCode agent permissions')
    action, denied, external = 'ask', [], 'ask'
    for rule in rules:
        if not isinstance(rule, dict):
            return decision('deny', 'Unsupported effective OpenCode agent permission')
        tool = rule.get('permission')
        if tool == 'external_directory':
            if rule.get('pattern') == '*' and rule.get('action') in ACTIONS:
                external = 'deny' if external == 'deny' else rule['action']
            elif rule.get('action') == 'deny':
                external = 'deny'
            continue
        if tool not in {'*', 'bash'}:
            if not isinstance(tool, str) or any(char in tool for char in '*?['):
                return decision('deny', 'Pattern-based OpenCode tool restrictions require the original permission evaluator')
            continue
        pattern, value = rule.get('pattern'), rule.get('action')
        if value not in ACTIONS or not isinstance(pattern, str):
            return decision('deny', 'Unsupported effective OpenCode command permission')
        if pattern != '*':
            if value != 'deny' or any(char in pattern for char in '?[\\\n\r'):
                return decision('deny', 'Only simple deny patterns can be transferred to native commands')
            denied.append(pattern)
        else:
            action = value
    return decision(action, denied_patterns=denied, external_directory=external)


def _stricter(left, right):
    order = {'allow': 0, 'ask': 1, 'deny': 2}
    selected = right if order[right['permission']] > order[left['permission']] else left
    external = max((left['external_directory'], right['external_directory']), key=order.get)
    return dict(selected, denied_patterns=list(dict.fromkeys(left['denied_patterns'] + right['denied_patterns'])),
                external_directory=external)


def resolve_command_policy(task, cancel=None):
    """Read resolved config without writing it or exposing provider credentials."""
    if cancel and cancel.is_set():
        raise InterruptedError('Command permission inspection cancelled')
    cli = executable('opencode')
    if not cli:
        return decision('deny', 'OpenCode is unavailable; native command permissions were not verified')
    environment = os.environ.copy()
    configured = environment.get('OPENCODE_CONFIG')
    if configured:
        managed = os.path.normcase(str(document_path(task, 'opencode.json').resolve()))
        if os.path.normcase(str(Path(configured).resolve())) == managed:
            # Inspect the user's effective config, not our bash transport disable.
            environment.pop('OPENCODE_CONFIG')
    started = time.monotonic()

    def read(*arguments):
        remaining = 20 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('Command permission inspection timed out')
        return parse_cli_json(capture([cli, 'debug', *arguments], timeout=remaining,
                                      cancel=cancel, env=environment, cwd=task['workspace']))

    try:
        config = read('config')
        if not isinstance(config, dict):
            return decision('deny', 'Cannot read effective OpenCode configuration')
        policy = _configured(config.get('permission'))
        if not policy['allowed']:
            return dict(policy, reason=policy['reason'] or 'OpenCode denies shell commands')
        selected = task.get('agent') or config.get('default_agent') or 'build'
        if not isinstance(selected, str) or not selected or selected.startswith('-'):
            return decision('deny', 'Cannot identify the selected OpenCode agent')
        agents = config.get('agent') or {}
        if not isinstance(agents, dict):
            return decision('deny', 'Unsupported OpenCode agent configuration')
        configured_agent = agents.get(selected) or {}
        if not isinstance(configured_agent, dict) or configured_agent.get('disable'):
            return decision('deny', 'Selected OpenCode agent is unavailable')
        policy = _stricter(policy, _configured(configured_agent.get('permission'), policy['permission']))
        if not policy['allowed']:
            return dict(policy, reason=policy['reason'] or 'OpenCode agent denies shell commands')
        effective = read('agent', selected)
        if not isinstance(effective, dict) or effective.get('name') != selected:
            return decision('deny', 'Cannot verify the selected OpenCode agent')
        policy = _stricter(policy, _effective_rules(effective.get('permission')))
        if policy['external_directory'] == 'deny':
            return dict(policy, allowed=False, permission='deny', reason=
                        'External-directory denial requires the original OpenCode permission evaluator')
        return dict(policy, reason=policy['reason'] or (
            'OpenCode shell permissions require approval' if policy['permission'] == 'ask' else
            'OpenCode permits shell commands' if policy['allowed'] else 'OpenCode agent denies shell commands'))
    except InterruptedError:
        raise
    except Exception:
        # CLI errors can contain config/provider data. Never persist their output.
        return decision('deny', 'Could not verify effective OpenCode command permissions')


AST_SCRIPT = r'''
$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:AGENTVISOR_POLICY_SOURCE))
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
$unsafe = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true))
$commands = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object {
    $parts = @($_.CommandElements | ForEach-Object {
        if ($_ -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
            $_ -is [System.Management.Automation.Language.ConstantExpressionAst]) {
            @{literal=$true; value=[string]$_.Value}
        } elseif ($_ -is [System.Management.Automation.Language.CommandParameterAst]) {
            @{literal=$true; value=$_.Extent.Text}
        } elseif ($_ -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and
                  $_.NestedExpressions.Count -eq 0) {
            @{literal=$true; value=[string]$_.Value}
        } else { @{literal=$false; value=''} }
    })
    @{name=$_.GetCommandName(); text=$_.Extent.Text; elements=$parts; invocation=[string]$_.InvocationOperator}
})
@{parse_errors=@($parseErrors).Count; unsafe_definitions=$unsafe.Count; commands=$commands} | ConvertTo-Json -Depth 8 -Compress
'''


def _normalize(value):
    return re.sub(r'\s+', ' ', value).strip().casefold()


def check_command_policy(policy, command, shell, cwd, workspace, cancel=None):
    """Check command syntax as data; never execute it to decide permission."""
    if not policy.get('allowed') or policy.get('permission') not in {'ask', 'allow'}:
        return decision('deny', policy.get('reason') or 'Native command permissions are unavailable')
    if policy.get('external_directory') == 'deny':
        return decision('deny', 'External-directory denial requires the original OpenCode permission evaluator')
    result = decision(policy['permission'])
    try:
        if not Path(cwd or workspace).resolve().is_relative_to(Path(workspace).resolve()):
            external = policy.get('external_directory', 'ask')
            if external == 'deny':
                return decision('deny', 'OpenCode denies commands outside the task workspace')
            if external != 'allow':
                result['permission'] = 'ask'
    except (OSError, ValueError):
        return decision('deny', 'Cannot verify the command working directory')
    patterns = [_normalize(pattern) for pattern in policy.get('denied_patterns', [])]
    if not patterns:
        return result
    if not isinstance(command, str) or not command.strip():
        return decision('deny', 'Cannot verify empty or invalid command source')
    if shell != 'powershell':
        return decision('deny', 'Restricted commands require native PowerShell permission inspection')
    binary = shutil.which('powershell.exe' if os.name == 'nt' else 'pwsh')
    if not binary:
        return decision('deny', 'PowerShell command parser is unavailable')
    environment = os.environ.copy()
    environment['AGENTVISOR_POLICY_SOURCE'] = base64.b64encode(command.encode('utf-8')).decode()
    encoded = base64.b64encode(AST_SCRIPT.encode('utf-16le')).decode()
    try:
        parsed = json.loads(capture([binary, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                   timeout=5, cancel=cancel, env=environment))
        if parsed.get('parse_errors') or parsed.get('unsafe_definitions'):
            return decision('deny', 'Unsupported PowerShell syntax in restricted command')
        commands = parsed.get('commands')
        if not isinstance(commands, list):
            return decision('deny', 'Cannot inspect PowerShell command structure')
        for item in commands:
            name = item.get('name')
            if not isinstance(name, str) or not name or item.get('invocation') == 'Dot':
                return decision('deny', 'Dynamic command invocation is not permitted by restricted command policy')
            name = name.replace('\\', '/').rsplit('/', 1)[-1].casefold()
            name = re.sub(r'\.(exe|cmd|bat|ps1)$', '', name)
            if name in {'powershell', 'pwsh', 'cmd', 'bash', 'sh', 'wsl', 'iex', 'invoke-expression',
                        'invoke-command', 'start-process', 'saps', 'start', 'set-alias', 'new-alias', 'sal', 'nal'}:
                return decision('deny', 'Shell forwarding and alias changes cannot preserve command-specific restrictions')
            elements = item.get('elements') or []
            if not elements or not elements[0].get('literal'):
                return decision('deny', 'Dynamic command invocation is not permitted by restricted command policy')
            normalized = ' '.join([name] + [_normalize(element['value']) for element in elements[1:]
                                           if element.get('literal')])
            texts = (_normalize(command), _normalize(item.get('text', '')), normalized)
            for pattern in patterns:
                if any(fnmatch.fnmatchcase(text, pattern) for text in texts):
                    return decision('deny', 'Command is denied by existing OpenCode shell policy')
                target = pattern.split(' ', 1)[0]
                if (fnmatch.fnmatchcase(name, target) and
                        any(not element.get('literal') for element in elements[1:])):
                    return decision('deny', 'Dynamic arguments cannot be checked against existing command restrictions')
        return result
    except InterruptedError:
        raise
    except Exception:
        return decision('deny', 'Could not inspect PowerShell command permissions')
