"""Preflight the complete prompt; never trim a conversation to make it fit.

The fallback is deliberately an estimate, not a tokenizer: compact JSON length
at four ASCII characters per token, plus a surcharge for UTF-8 and message/tool
framing. A separate 20% window reserve covers estimation/template uncertainty.
Provider usage can only make this policy stricter: usage after server truncation
must never authorize a larger prompt. A provider-rendered token count may replace
the estimate when the caller can count *all* messages and tool schemas reliably.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import threading


PROMPT_FIELDS = ('messages', 'tools', 'functions', 'function_call', 'tool_choice',
                 'response_format')


def static_body(body):
    """Return the irreducible prefix for counting, not for sending to the model.

    All system/developer instructions, tool definitions, and initial user input
    survive a new session. A single huge user summarization request is therefore
    a static failure, whereas a huge tool result calls for a supervised handoff.
    """
    initial = True
    messages = []
    for message in body.get('messages') or []:
        role = message.get('role')
        if role in {'assistant', 'tool', 'function'}:
            initial = False
        if role in {'system', 'developer'} or initial and role == 'user':
            messages.append(message)
    result = {key: body[key] for key in PROMPT_FIELDS if key in body}
    result['messages'] = messages
    return result


def _text_payload(value):
    """Separate media from text; base64 is wire data, not prompt text tokens."""
    images, unsupported = 0, False
    if isinstance(value, dict):
        if value.get('type') in {'image_url', 'input_image'}:
            return {'type': 'image_budget_placeholder'}, 1, False
        if value.get('type') in {'input_audio', 'video', 'file'}:
            return {'type': 'unsupported_media'}, 0, True
        result = {}
        for key, item in value.items():
            result[key], found, unknown = _text_payload(item)
            images, unsupported = images + found, unsupported or unknown
        return result, images, unsupported
    if isinstance(value, list):
        result = []
        for item in value:
            text, found, unknown = _text_payload(item)
            result.append(text)
            images, unsupported = images + found, unsupported or unknown
        return result, images, unsupported
    return value, images, unsupported


def _measure(body, image_reserve):
    prompt = {key: body[key] for key in PROMPT_FIELDS if key in body}
    encoded = json.dumps(prompt, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    # Only message content contains media. Schema dictionaries may themselves
    # describe image/file parts and must remain fully counted as ordinary text.
    text = dict(prompt)
    text['messages'], images, unsupported = _text_payload(prompt.get('messages', []))
    counted = json.dumps(text, ensure_ascii=False, separators=(',', ':'))
    chars, text_bytes = len(counted), len(counted.encode('utf-8'))
    messages = len(body.get('messages') or [])
    tools = len(body.get('tools') or []) + len(body.get('functions') or [])
    tokens = math.ceil(chars / 4 + (text_bytes - chars) / 2) + 16 * messages + 8 * tools + 64
    return (tokens + images * image_reserve, len(encoded), messages, tools,
            hashlib.sha256(encoded).hexdigest()[:24], images, unsupported)


def _positive_integer(value):
    return type(value) is int and value > 0


@dataclass(frozen=True)
class BudgetDecision:
    action: str
    reason: str
    metrics: dict


class ContextBudget:
    """One controller per loaded model/session; assess after every prompt injection.

    ``rotate`` asks the supervisor to preserve verified state and open a fresh
    session. ``blocked`` is not retryable with the same prompt/configuration.
    The caller must bound repeated handoffs with no useful progress; this class
    intentionally has no side effects, does not restart processes, and cannot
    silently discard a tool result, instruction, or conversation turn.
    """

    def __init__(self, context, output_limit=4096, *, safety_fraction=0.20, image_reserve=8192):
        if not isinstance(safety_fraction, (int, float)) or not 0.05 <= safety_fraction <= 0.5:
            raise ValueError('safety_fraction must be between 0.05 and 0.5')
        self.context, self.output_limit = context, output_limit
        if not _positive_integer(image_reserve):
            raise ValueError('image_reserve must be a positive integer')
        self.image_reserve = image_reserve
        self.safety_fraction = safety_fraction
        self._calibration = 1.0
        self._lock = threading.Lock()

    def assess(self, body, *, prompt_tokens=None, static_prompt_tokens=None):
        """Assess complete JSON; optional counts must include provider templating.

        Ordinary response ``usage.prompt_tokens`` is NOT a preflight count. Pass
        it to ``observe`` instead, because the server may have truncated input.
        """
        raw, size, messages, tools, fingerprint, images, unsupported = _measure(body, self.image_reserve)
        static_raw, static_size, _, _, static_fingerprint, _, _ = _measure(static_body(body), self.image_reserve)
        with self._lock:
            calibration = self._calibration
        estimate = math.ceil(raw * calibration)
        static_estimate = math.ceil(static_raw * calibration)
        counted = _positive_integer(prompt_tokens)
        if counted:
            estimate = prompt_tokens
            # The irreducible subset cannot exceed a counted complete prompt.
            static_estimate = min(static_estimate, estimate)
        if _positive_integer(static_prompt_tokens):
            static_estimate = static_prompt_tokens
        requested = [body[field] for field in ('max_tokens', 'max_completion_tokens')
                     if _positive_integer(body.get(field))]
        output = max(requested) if requested else self.output_limit
        valid = _positive_integer(self.context) and _positive_integer(output)
        safety = math.ceil(self.context * self.safety_fraction) if valid else 0
        available = max(0, self.context - output - safety) if valid else 0
        metrics = {
            'context_limit': self.context, 'output_reserve': output,
            'safety_reserve': safety, 'input_limit': available,
            'estimated_tokens': estimate, 'static_tokens': static_estimate,
            'raw_estimated_tokens': raw, 'prompt_bytes': size, 'static_bytes': static_size,
            'message_count': messages, 'tool_count': tools,
            'method': 'provider_count' if counted else 'utf8_estimate',
            'calibration': round(calibration, 4), 'fingerprint': fingerprint,
            'static_fingerprint': static_fingerprint,
            'image_tokens_estimate': images * self.image_reserve,
            'uncertain_images': images if not counted else 0,
            'headroom_tokens': available - estimate,
            'static_headroom_tokens': available - static_estimate,
        }
        if not valid or available <= 0:
            return BudgetDecision('blocked', 'invalid_context_budget', metrics)
        if not counted and unsupported:
            return BudgetDecision('blocked', 'unsupported_media_requires_token_count', metrics)
        if static_estimate > available:
            return BudgetDecision('blocked', 'static_context_overflow', metrics)
        if estimate > available:
            return BudgetDecision('rotate', 'context_budget_exhausted', metrics)
        return BudgetDecision('allow', 'within_budget', metrics)

    def observe(self, decision, prompt_tokens):
        """Return calibration diagnostics, ignoring low/truncated usage for growth.

        Calibration is monotonic and model-local. A caller should persist it
        across its controlled handoffs if it creates a new gateway per session.
        """
        if not _positive_integer(prompt_tokens) or decision.action != 'allow':
            return None
        raw = decision.metrics['raw_estimated_tokens']
        ratio = prompt_tokens / max(1, raw)
        with self._lock:
            self._calibration = max(self._calibration, ratio)
            calibrated = self._calibration
        return {'observed_prompt_tokens': prompt_tokens, 'raw_estimated_tokens': raw,
                'calibration': round(calibrated, 4),
                'underestimated': prompt_tokens > decision.metrics['estimated_tokens']}

    def restore_calibration(self, value):
        """Retain stricter estimates across fresh sessions for this same model."""
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 1:
            with self._lock:
                self._calibration = max(self._calibration, value)
