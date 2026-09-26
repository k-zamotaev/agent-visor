"""Observe only this task's inference stream and deliver optional user context."""
import asyncio
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class StreamMetrics:
    def __init__(self):
        self.first = self.last = None
        self.tokens = None
        self.native_rate = None
        self.reasoning = ''

    def accept(self, payload, now):
        active = False
        for choice in payload.get('choices', []):
            delta = choice.get('delta') or choice.get('message') or {}
            reasoning = delta.get('reasoning_content') or delta.get('reasoning') or ''
            if isinstance(reasoning, str):
                self.reasoning += reasoning
            if delta.get('content') or reasoning or delta.get('tool_calls'):
                self.first = now if self.first is None else self.first
                self.last = now
                active = True
        usage = payload.get('usage') or {}
        if isinstance(usage.get('completion_tokens'), int):
            self.tokens = usage['completion_tokens']
        rate = (payload.get('stats') or {}).get('tokens_per_second')
        if isinstance(rate, (int, float)) and 0 < rate < 100000:
            self.native_rate = rate
        return active

    def result(self):
        seconds = self.last - self.first if self.first is not None else 0
        # Fragments are NOT tokens. Only provider-reported usage is counted.
        # Exclude the first token, whose latency is outside the measured interval.
        rate = self.native_rate
        source = 'runtime' if rate is not None else 'stream'
        if rate is None and self.tokens is not None and self.tokens > 1 and seconds >= 0.05:
            rate = (self.tokens - 1) / seconds
        return {'tokens_per_second': rate, 'output_tokens': self.tokens,
                'generation_seconds': round(seconds, 4), 'source': source}


class InferenceGateway:
    def __init__(self, store, task, profile, cancel):
        self.store, self.task, self.profile, self.cancel = store, task, profile, cancel
        self.last_activity = time.monotonic()
        self.reasoning_seen = False
        self.stopping = threading.Event()
        self.requests = {}
        self.lock = threading.Lock()
        gateway = self
        route = '/' + secrets.token_urlsafe(24) + '/v1'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                if self.path != route + '/chat/completions':
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8 * 1024 * 1024:
                        self.send_error(413)
                        return
                    body = json.loads(self.rfile.read(length))
                    try:
                        asyncio.run(gateway.forward(self, body))
                    except asyncio.CancelledError:
                        pass
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception as error:
                    if not gateway.stopping.is_set() and not gateway.cancel.is_set():
                        gateway.store.event(task['id'], 'inference_error', str(error)[:1500], 'warning')
                    # The downstream connection is closed by HTTPServer. Do not
                    # append an HTTP error into an already started SSE response.

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f'http://127.0.0.1:{self.server.server_port}{route}'
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.1}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            requests = list(self.requests.items())
        for loop, (request, _) in requests:
            try:
                loop.call_soon_threadsafe(request.cancel)
            except RuntimeError:
                pass  # The request already completed and closed its loop.
        deadline = time.monotonic() + 2
        for _, (_, done) in requests:
            done.wait(max(0, deadline - time.monotonic()))
        self.thread.join(timeout=1)

    def activity(self):
        return self.last_activity

    async def forward(self, handler, body):
        task = self.store.get(self.task['id'])
        additions = task.get('context_additions', [])
        version = task.get('context_version', 0)
        if additions:
            body.setdefault('messages', []).append({'role': 'user', 'content':
                'Additional task context from the user (supplements the original goal):\n' +
                '\n\n'.join(f'[{entry["version"]}] {entry["text"]}' for entry in additions)})
        if body.get('stream'):
            body['stream_options'] = dict(body.get('stream_options') or {}, include_usage=True)
        headers = {'Content-Type': 'application/json'}
        if handler.headers.get('Authorization'):
            headers['Authorization'] = handler.headers['Authorization']
        metrics = StreamMetrics()
        flushed = time.monotonic()
        request_id = secrets.token_hex(6)
        finished = False
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(
            connect=15, read=self.task['timeout_seconds'], write=30, pool=15))
        loop, done = asyncio.get_running_loop(), threading.Event()
        with self.lock:
            self.requests[loop] = (asyncio.current_task(), done)
        try:
            if self.stopping.is_set() or self.cancel.is_set():
                return
            async with client, client.stream('POST', self.profile['base_url'] + '/v1/chat/completions',
                                             headers=headers, json=body) as response:
                handler.send_response(response.status_code)
                handler.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
                handler.send_header('Cache-Control', 'no-store')
                handler.end_headers()
                if response.is_success and version:
                    self.store.mark_context_delivered(task['id'], version)
                is_stream = 'text/event-stream' in response.headers.get('content-type', '')
                if not is_stream:
                    data = await response.aread()
                    handler.wfile.write(data)
                    return
                async for line in response.aiter_lines():
                    if self.stopping.is_set() or self.cancel.is_set():
                        break
                    if line.startswith('data:') and line[5:].strip() == '[DONE]':
                        finished = True
                    elif line.startswith('data:'):
                        try:
                            payload = json.loads(line[5:].strip())
                        except ValueError:
                            payload = {}
                        now = time.monotonic()
                        if metrics.accept(payload, now):
                            self.last_activity = now
                        if now - flushed >= 1:
                            self.publish_activity(metrics, request_id)
                            flushed = now
                    handler.wfile.write((line + '\n').encode('utf-8'))
                    handler.wfile.flush()
                self.publish_activity(metrics, request_id, finished=True)
                sample = dict(metrics.result(), time=time.time(), request_id=request_id,
                              iteration=task['iteration'])
                if finished and sample['tokens_per_second'] is not None:
                    self.store.update(task['id'], generation_sample=sample)
                    self.store.event(task['id'], 'generation_sample', 'Измерена скорость генерации', data=sample)
        finally:
            try:
                await client.aclose()
            finally:
                with self.lock:
                    self.requests.pop(loop, None)
                done.set()

    def publish_activity(self, metrics, request_id, finished=False):
        self.store.update(self.task['id'], generation_activity={
            'time': time.time() - max(0, time.monotonic() - self.last_activity),
            'active': not finished and metrics.first is not None,
            'request_id': request_id})
        while metrics.reasoning:
            text, metrics.reasoning = metrics.reasoning[:5000], metrics.reasoning[5000:]
            self.reasoning_seen = True
            self.store.event(self.task['id'], 'reasoning', text, data={
                'source': 'model_stream', 'request_id': request_id})
