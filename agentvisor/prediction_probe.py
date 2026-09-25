"""Short calibration request isolated so task cancellation can close its connection."""
import json
import os
import sys

import httpx


def main():
    url, encoded = sys.argv[1:]
    token = os.environ.get('AGENTVISOR_MODEL_TOKEN')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with httpx.Client(timeout=120, trust_env=False, headers=headers) as client:
        result = client.post(url, json=json.loads(encoded))
        result.raise_for_status()
        print(json.dumps(result.json()))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
