"""Localize product messages; never translate arbitrary agent or command output."""
import json
import re
from pathlib import Path

ENGLISH = json.loads((Path(__file__).parent / 'static/locales/en.json').read_text(encoding='utf-8'))
REVERSE = {value: key for key, value in ENGLISH.items()}


def _pattern(text):
    parts = re.split(r'(\{\d+\})', text)
    return re.compile('^' + ''.join('(.+?)' if re.fullmatch(r'\{\d+\}', p) else re.escape(p)
                                  for p in parts) + '$', re.S)


TEMPLATES = [(source, target, _pattern(source), _pattern(target))
             for source, target in ENGLISH.items() if re.search(r'\{\d+\}', source)]
RAW_EVENTS = {'text', 'reasoning', 'tool', 'agent_error', 'agent_event', 'output', 'verification_output'}


def language_from_header(header):
    candidates = []
    for item in (header or '').split(','):
        parts = item.strip().lower().split(';')
        language = parts[0].split('-')[0]
        try:
            quality = float(next((p.strip()[2:] for p in parts[1:] if p.strip().startswith('q=')), '1'))
        except ValueError:
            continue
        if language in {'ru', 'en'} and 0 < quality <= 1:
            candidates.append((quality, language))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else 'ru'


def translate(message, language='ru', depth=0):
    if not isinstance(message, str) or not message or depth > 6:
        return message
    source = message if message in ENGLISH else REVERSE.get(message)
    if source is not None:
        return ENGLISH[source] if language == 'en' else source
    for source, target, source_pattern, target_pattern in TEMPLATES:
        match = source_pattern.fullmatch(message) or target_pattern.fullmatch(message)
        if match:
            values = [translate(value, language, depth + 1) for value in match.groups()]
            template = target if language == 'en' else source
            return re.sub(r'\{(\d+)\}', lambda found: values[int(found[1])], template)
    return message


def task_view(task, language):
    return dict(task, reason=translate(task.get('reason_source', task.get('reason', '')), language))


def event_view(event, language):
    value = dict(event)
    if event['kind'] not in RAW_EVENTS:
        source = event['data'].get('_i18n', {}).get('source', event['message'])
        value['message_original'] = event['message']
        value['message'] = translate(source, language)
        value['data'] = {key: item for key, item in event['data'].items() if key != '_i18n'}
    return value


def model_view(result, language):
    value = dict(result)
    for key in ('error', 'note'):
        if value.get(key):
            value[key] = translate(value[key], language)
    if value.get('text') and value.get('kind') != 'runtime_estimate':
        value['text'] = translate(value['text'], language)
    if value.get('service'):
        value['service'] = dict(value['service'])
        for key in ('stage', 'error'):
            value['service'][key] = translate(value['service'].get(key), language)
    if value.get('recommendation'):
        value['recommendation'] = dict(value['recommendation'], reason=translate(value['recommendation']['reason'], language))
    if value.get('benchmark'):
        value['benchmark'] = model_view(value['benchmark'], language)
    return value
