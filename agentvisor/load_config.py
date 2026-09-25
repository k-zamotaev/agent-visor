"""Explicit LM Studio load configuration and bounded SDK subprocess entry point."""
import json
import sys
from urllib.parse import urlparse

LOAD_FIELDS = ('flash_attention', 'cache_type_k', 'cache_type_v')


def explicit_config(profile):
    return any(profile.get(key, 'auto') != 'auto' for key in LOAD_FIELDS)


def sdk_config(profile):
    config = {'contextLength': profile['context']}
    if profile.get('gpu', 'auto') != 'auto':
        config['gpu'] = {'ratio': 0 if profile['gpu'] == 'off' else 1}
    flash = profile.get('flash_attention', 'auto')
    if flash != 'auto':
        config['flashAttention'] = flash == 'on'
    for field, sdk_field in [('cache_type_k', 'llamaKCacheQuantizationType'),
                             ('cache_type_v', 'llamaVCacheQuantizationType')]:
        if profile.get(field, 'auto') != 'auto':
            config[sdk_field] = profile[field]
    if profile.get('cache_type_v', 'auto') in {'q8_0', 'q4_0'}:
        if flash == 'off':
            raise ValueError('Квантование V-кэша требует Flash Attention')
        config['flashAttention'] = True
    return config


def verify_config(profile, actual):
    expected = sdk_config(profile)
    for key, value in expected.items():
        found = actual.get(key)
        if key == 'gpu':
            found, value = (found or {}).get('ratio'), value['ratio']
        if found != value:
            raise RuntimeError('LM Studio не подтвердил параметр загрузки: ' + key)


def main():
    import lmstudio as lms
    action, encoded, identifier = sys.argv[1:]
    profile = json.loads(encoded)
    endpoint = urlparse(profile['base_url'])
    if endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('Настройки Flash Attention и KV-кэша доступны через локальный LM Studio SDK')
    lms.set_sync_api_timeout(300 if action == 'load' else 15)
    with lms.Client(endpoint.netloc) as client:
        if action == 'load':
            model = client.llm.load_new_instance(profile['model'], identifier,
                                                config=sdk_config(profile), ttl=None)
        else:
            # A read must never trigger just-in-time loading.
            model = next((model for model in client.llm.list_loaded() if model.identifier == identifier), None)
            if model is None:
                raise RuntimeError('Рабочий экземпляр модели выгружен из памяти')
        actual = model.get_load_config().to_dict()
        verify_config(profile, actual)
        print(json.dumps({'instance': identifier, 'context': actual['contextLength'], 'load_config': actual}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
