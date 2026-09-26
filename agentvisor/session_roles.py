"""Sequential session contracts for the currently loaded model, without routing it."""


CONTRACTS = {
    'executor': {
        'purpose': 'Implement and verify the next concrete step using the latest diagnosis.',
        'inputs': ['current goal and user constraints', 'next unchecked step',
                   'task memory and observed recovery evidence'],
        'outputs': ['bounded implementation', 'actual verification results',
                    'updated progress and task memory'],
    },
    'diagnostician': {
        'purpose': 'Investigate one recurring blocker and hand a testable repair to the executor.',
        'inputs': ['current goal and user constraints', 'recorded failure and attempted approaches',
                   'current files, tool results and environment'],
        'outputs': ['hypothesis with supporting and contradicting evidence',
                    'bounded reproduction result', 'next repair and its acceptance check'],
    },
    'reviewer': {
        'purpose': 'Independently verify claimed milestones using fresh observed evidence.',
        'inputs': ['current goal and user constraints', 'requested milestone identities',
                   'current files and fresh checks'],
        'outputs': ['the requested review report with evidence or a concrete rejection'],
    },
}


def session_role(task, review=False):
    """Select a role without changing the profile, runtime, task or its budgets.

    The supervisor records each attempted *working* role in last_session_role;
    reviewer sessions must not replace it. A sticky recovery flag then
    alternates investigation and implementation instead of diagnosing forever.
    """
    version = task['goal_version']
    recovery = task.get('recovery_context') or {}
    previous = task.get('last_session_role') or {}
    current_recovery = recovery.get('goal_version') == version
    needs_diagnosis = current_recovery and (
        recovery.get('repair') or (recovery.get('failure_cause') or {}).get('attempts', 0) >= 3)
    after_diagnosis = previous.get('goal_version') == version and previous.get('name') == 'diagnostician'
    name = 'reviewer' if review else 'diagnostician' if needs_diagnosis and not after_diagnosis else 'executor'
    contract = CONTRACTS[name]
    return {
        'name': name, 'goal_version': version, 'purpose': contract['purpose'],
        'inputs': list(contract['inputs']), 'outputs': list(contract['outputs']),
        'execution': 'sequential_same_loaded_model', 'uses_existing_task_budgets': True,
        'after_diagnosis': bool(after_diagnosis and name == 'executor'),
    }


def role_prompt(role, relative, goal_version):
    """Give each role a bounded outcome; execution limits remain supervisor-owned."""
    name = role['name']
    shared = (
        f'\nSESSION ROLE: {name}. Use the same loaded model in this sequential session. '
        'Stay within the existing task, iteration and command budgets; do not start another model '
        'or delegate parallel inference. Preserve the current goal, user constraints and permissions. '
        f'Read {relative}/GOAL.md, {relative}/PROGRESS.md and the project AGENTS.md. '
    )
    if name == 'diagnostician':
        return shared + (
            'Investigate ONE recorded blocker. First inspect the exact failure and previous attempts; '
            'then choose the smallest bounded probe that can confirm or disprove a specific cause. '
            'Run the probe, inspect its real result, and stop this investigation once the next concrete '
            'repair is supported or the remaining uncertainty is identified. Do not repeat an unchanged '
            'probe, wait interactively, implement the next feature or edit product code. '
            'A successful probe proves only what it tested; do not claim that the task is repaired. '
            f'Write a compact handoff to {relative}/MEMORY.md with goal_version: {goal_version} '
            'on its own line: observed failure; hypothesis; exact probe and result; what remains '
            'uncertain; recommended repair; the check that will decide whether that repair works. '
            f'Update diagnostic notes in {relative}/PROGRESS.md without adding completed checkmarks '
            'or weakening the checklist. Keep MEMORY.md within 2000 characters. Never write DONE.md '
            'or claim milestone acceptance in this role. End the session for the executor to act.\n'
        )
    if name == 'reviewer':
        return shared + (
            'Act only as the independent reviewer. Follow the requested review scope and report format. '
            'Use fresh observed checks; reject insufficient evidence. Do not implement repairs or '
            'advance the plan. A separate executor handles any rejected result.\n'
        )
    if name != 'executor':
        raise ValueError('Unknown session role')
    return shared + (
        f'Read the latest handoff in {relative}/MEMORY.md when present. Treat any proposed diagnosis '
        'as a hypothesis and verify its assumptions before acting. Implement one concrete repair or '
        'the next unchecked step, run its meaningful acceptance check, and preserve the actual result. '
        'A diagnosis alone does not complete a step; record unresolved failures for the next session.\n'
    )
