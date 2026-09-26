"""Wait for dependencies among already owned processes without another model call."""
import time


def wait_any(runner, payload, cancel=None):
    """Return when an existing process finishes, or after at most 1000 ms of waiting.

    Run independent tools while commands are running. Wait only when their
    results are dependencies; never resubmit a command just to check its state.
    Process cleanup and filesystem reads use the runner's existing poll bounds.
    """
    if not isinstance(payload, dict):
        raise ValueError('wait_any arguments must be an object')
    identifiers = payload.get('process_ids')
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 16 or
            any(not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers)):
        raise ValueError('process_ids must contain between 1 and 16 nonempty owned process IDs')
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('process_ids must be unique')
    milliseconds = payload.get('yield_ms', payload.get('timeout_ms', 1000))
    if (isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or
            not 0 <= milliseconds <= 1000):
        raise ValueError('yield_ms must be between 0 and 1000')
    if 'timeout_ms' in payload:
        timeout = payload['timeout_ms']
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not 0 <= timeout <= 1000 or timeout != milliseconds):
            raise ValueError('timeout_ms must be between 0 and 1000 and match yield_ms when both are supplied')

    events = [runner.cancel, runner.closing]
    if cancel is not None:
        events.append(cancel)
    wait_event = cancel if cancel is not None else runner.cancel
    started = time.monotonic()
    deadline = started + milliseconds / 1000
    while True:
        ready, running = [], []
        for identifier in identifiers:
            if any(event.is_set() for event in events):
                raise InterruptedError('Process wait cancelled')
            result = runner.poll({'process_id': identifier, 'yield_ms': 0})
            if result['status'] == 'running':
                running.append(identifier)
            else:
                ready.append(result)
        if any(event.is_set() for event in events):
            raise InterruptedError('Process wait cancelled')
        now = time.monotonic()
        if ready or now >= deadline:
            return {'ready': ready, 'running': running, 'timed_out': not bool(ready),
                    'waited_ms': round(max(0, now - started) * 1000, 1)}
        wait_event.wait(min(0.05, deadline - now))
