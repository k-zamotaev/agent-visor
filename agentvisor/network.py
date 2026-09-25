"""Persisted listener settings and a private credential for network access."""
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile


HOSTS = ('127.0.0.1', '0.0.0.0')
COOKIE = 'agentvisor-access'


def read_network(directory):
    path = Path(directory) / 'network.json'
    if not path.exists():
        return {'host': '127.0.0.1', 'access_code': ''}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('host') not in HOSTS:
        raise ValueError('Некорректные настройки сетевого доступа')
    if not isinstance(value.get('access_code'), str) or len(value['access_code']) < 24:
        raise ValueError('Некорректный код сетевого доступа')
    return {'host': value['host'], 'access_code': value['access_code']}


def write_network(directory, value):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='network-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / 'network.json')
    finally:
        Path(name).unlink(missing_ok=True)


def ensure_network(directory):
    value = read_network(directory)
    if not value['access_code']:
        value['access_code'] = secrets.token_urlsafe(24)
        write_network(directory, value)
    return value


def is_loopback(host):
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def local_addresses():
    import psutil
    addresses = set()
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.family == socket.AF_INET and not is_loopback(entry.address):
                address = ipaddress.ip_address(entry.address)
                if not address.is_unspecified and not address.is_link_local:
                    addresses.add(str(address))
    return sorted(addresses)


def trusted_hosts():
    return ['localhost', '127.0.0.1', '[::1]', 'testserver', socket.gethostname(),
            *local_addresses(),
            *filter(None, (host.strip() for host in os.environ.get('AGENTVISOR_ALLOWED_HOSTS', '').split(',')))]
