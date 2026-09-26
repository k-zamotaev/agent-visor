import base64
import json
import os
from pathlib import Path
import threading

import pytest

from agentvisor.command_policy import check_command_policy, resolve_command_policy
from agentvisor.tasks import document_path
from test_supervisor import make


def stub_config(monkeypatch, config, rules=None):
    calls = []

    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == 'config':
            return 'startup information\n' + json.dumps(config)
        return json.dumps({'name': argv[-1], 'permission': rules or [
            {'permission': '*', 'pattern': '*', 'action': 'allow'}]})

    monkeypatch.setattr('agentvisor.command_policy.executable', lambda _: 'opencode.exe')
    monkeypatch.setattr('agentvisor.command_policy.capture', capture)
    return calls


@pytest.mark.parametrize(('permissions', 'expected'), [
    (None, 'ask'), ('allow', 'allow'), ('deny', 'deny'),
    ({'*': 'deny', 'bash': 'allow', 'external_directory': 'allow'}, 'allow'),
    ({'bash': {'*': 'allow', 'git push*': 'deny'}}, 'allow'),
    ({'bash': {'*': 'ask', 'git push*': 'deny'}}, 'ask'),
    ({'bash': {'*': 'deny', 'git status': 'allow'}}, 'deny'),
    ({'bash': {'*': 'allow', 'git push*': 'ask'}}, 'deny'),
    ({'bash': {'*': 'allow', 'git p?sh*': 'deny'}}, 'deny'),
    ({'ba*': 'deny'}, 'deny'),
])
def test_resolved_policy_preserves_global_shell_rules(tmp_path, monkeypatch, permissions, expected):
    _, _, task = make(tmp_path)
    calls = stub_config(monkeypatch, {'permission': permissions})
    result = resolve_command_policy(task)
    assert result['permission'] == expected
    assert result['allowed'] == (expected != 'deny')
    assert all(call[1]['cwd'] == task['workspace'] and 0 < call[1]['timeout'] <= 20 for call in calls)
    if isinstance(permissions, dict) and isinstance(permissions.get('bash'), dict) and expected != 'deny':
        assert result['denied_patterns'] == ['git push*']


def test_default_and_effective_agent_restrictions_are_preserved(tmp_path, monkeypatch):
    _, _, task = make(tmp_path)
    calls = stub_config(monkeypatch, {'permission': 'allow', 'default_agent': 'review'}, rules=[
        {'permission': '*', 'pattern': '*', 'action': 'allow'},
        {'permission': 'bash', 'pattern': '*', 'action': 'deny'}])
    result = resolve_command_policy(task)
    assert result['permission'] == 'deny'
    assert calls[-1][0][-2:] == ['agent', 'review']


def test_agent_config_cannot_relax_global_command_blacklist(tmp_path, monkeypatch):
    _, _, task = make(tmp_path)
    stub_config(monkeypatch, {'permission': {'bash': {'*': 'allow', 'git push*': 'deny'}},
                             'agent': {'build': {'permission': {'bash': 'allow'}}}})
    assert resolve_command_policy(task)['denied_patterns'] == ['git push*']


def test_only_managed_config_env_is_removed(tmp_path, monkeypatch):
    _, _, task = make(tmp_path)
    monkeypatch.setenv('OPENCODE_CONFIG', str(document_path(task, 'opencode.json')))
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"permission":{"bash":"ask"}}')
    calls = stub_config(monkeypatch, {})
    resolve_command_policy(task)
    assert 'OPENCODE_CONFIG' not in calls[0][1]['env']
    assert 'OPENCODE_CONFIG_CONTENT' in calls[0][1]['env']
    assert 'OPENCODE_CONFIG' in os.environ
    custom = str(tmp_path / 'custom.json')
    monkeypatch.setenv('OPENCODE_CONFIG', custom)
    resolve_command_policy(task)
    assert calls[-1][1]['env']['OPENCODE_CONFIG'] == custom


def test_failed_config_inspection_is_closed_without_leaking_secrets(tmp_path, monkeypatch):
    _, _, task = make(tmp_path)
    monkeypatch.setattr('agentvisor.command_policy.executable', lambda _: 'opencode.exe')

    def failure(*args, **kwargs):
        raise RuntimeError('private-provider-secret')

    monkeypatch.setattr('agentvisor.command_policy.capture', failure)
    result = resolve_command_policy(task)
    assert not result['allowed']
    assert 'private-provider-secret' not in json.dumps(result)


def test_policy_inspection_propagates_user_cancellation(tmp_path):
    _, _, task = make(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        resolve_command_policy(task, cancel)


@pytest.fixture
def blacklist():
    return {'allowed': True, 'permission': 'ask', 'external_directory': 'ask',
            'denied_patterns': ['rm -rf *', 'git push*', 'git reset --hard*', 'shutdown*']}


@pytest.mark.skipif(os.name != 'nt', reason='Uses native PowerShell AST parser, never executes source')
@pytest.mark.parametrize('source', [
    'Write-Output ok; git push origin main',
    "Write-Output ok\ngit reset --hard HEAD",
    "& 'git' 'push' 'origin' 'main'",
    "& 'C:\\Program Files\\Git\\cmd\\git.exe' push origin main",
    'shutdown /s', 'rm -rf folder',
    "$command='git'; & $command push", "$verb='push'; git $verb origin main",
    "powershell -Command 'git push'", "Invoke-Expression 'git push'",
    'function g { git status }; g', 'Set-Alias g git; g push',
    'Write-Output (',
])
def test_forbidden_or_dynamic_commands_never_execute(tmp_path, blacklist, source):
    result = check_command_policy(blacklist, source, 'powershell', str(tmp_path), str(tmp_path))
    assert not result['allowed'], result


@pytest.mark.skipif(os.name != 'nt', reason='Uses native PowerShell AST parser, never executes source')
@pytest.mark.parametrize('source', [
    "$value='visor-$-literal'; Write-Output $value",
    "& 'C:\\Python\\python.exe' '-u' 'wire_server.py'",
    'git status --short', 'Get-Content README.md',
])
def test_native_safe_commands_remain_usable(tmp_path, blacklist, source):
    result = check_command_policy(blacklist, source, 'powershell', str(tmp_path), str(tmp_path))
    assert result['allowed'], result
    assert result['permission'] == 'ask'


def test_restricted_bash_fails_closed(tmp_path, blacklist):
    result = check_command_policy(blacklist, 'git status', 'bash', str(tmp_path), str(tmp_path))
    assert not result['allowed']


def test_external_working_directory_denial_and_approval(tmp_path, blacklist):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    denied = dict(blacklist, external_directory='deny')
    assert not check_command_policy(denied, 'echo ok', 'powershell', str(tmp_path), str(workspace))['allowed']
    asking = dict(blacklist, denied_patterns=[], permission='allow')
    result = check_command_policy(asking, 'echo ok', 'powershell', str(tmp_path), str(workspace))
    assert result['allowed'] and result['permission'] == 'ask'


def test_external_deny_disables_native_transport_even_with_workspace_cwd(tmp_path, monkeypatch, blacklist):
    _, _, task = make(tmp_path)
    stub_config(monkeypatch, {'permission': {'bash': 'allow', 'external_directory': 'deny'}})
    assert not resolve_command_policy(task)['allowed']
    command = "Get-Content 'C:\\outside-workspace\\private.txt'"
    policy = dict(blacklist, external_directory='deny')
    assert not check_command_policy(policy, command, 'powershell', str(tmp_path), str(tmp_path))['allowed']


def test_ast_source_is_only_passed_as_encoded_data(tmp_path, monkeypatch, blacklist):
    command = 'Write-Output "sensitive-input"'
    monkeypatch.setattr('agentvisor.command_policy.shutil.which', lambda _: 'powershell.exe')

    def parser(argv, **kwargs):
        assert command not in str(argv)
        assert 'sensitive-input' not in base64.b64decode(argv[-1]).decode('utf-16le')
        assert base64.b64decode(kwargs['env']['AGENTVISOR_POLICY_SOURCE']).decode() == command
        assert kwargs['timeout'] == 5
        return json.dumps({'parse_errors': 0, 'unsafe_definitions': 0, 'commands': []})

    monkeypatch.setattr('agentvisor.command_policy.capture', parser)
    assert check_command_policy(blacklist, command, 'powershell', str(tmp_path), str(tmp_path))['allowed']
