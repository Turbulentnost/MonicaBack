import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

THINK_RE = re.compile(
    r'<think>[\s\S]*?</think>|'
    r'<thinking>[\s\S]*?</thinking>|'
    r'<think>[\s\S]*?</think>',
    re.IGNORECASE,
)
# Unclosed thinking block at the start (common when max_tokens cuts mid-think)
THINK_UNCLOSED_RE = re.compile(
    r'^\s*<(?:think|thinking|redacted_reasoning)>[\s\S]*$',
    re.IGNORECASE,
)


def clean_completion_text(text: str) -> str:
    cleaned = THINK_RE.sub('', text or '')
    cleaned = THINK_UNCLOSED_RE.sub('', cleaned)
    cleaned = cleaned.strip().strip('"').strip("'")
    return cleaned.strip()


FORCED_MODEL = 'qwen3-vl-8b-thinking'


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    disable_thinking: bool = True,
) -> str:
    """Call OpenAI-compatible chat completions via LM Studio proxy (/v1)."""
    base = (getattr(settings, 'OPENAI_BASE_URL', '') or '').rstrip('/')
    if not base:
        raise RuntimeError('OPENAI_BASE_URL is not configured')

    # Trailing slash required by the isolated Django proxy (APPEND_SLASH).
    url = f'{base}/chat/completions/'
    token_budget = max_tokens if max_tokens is not None else settings.AI_MAX_TOKENS
    # Thinking models burn tokens on chain-of-thought; keep a safer floor for completions.
    if disable_thinking:
        token_budget = max(int(token_budget), 80)
    else:
        token_budget = max(int(token_budget), 256)

    payload: dict[str, Any] = {
        'model': FORCED_MODEL,
        'messages': messages,
        'max_tokens': token_budget,
        'temperature': temperature if temperature is not None else 0.6,
        'stream': False,
    }
    if disable_thinking:
        # LM Studio / Qwen3: try common knobs to skip CoT and return the answer only.
        payload['enable_thinking'] = False
        payload['chat_template_kwargs'] = {'enable_thinking': False}

    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    # Proxy itself does not require JWT; optional key only if upstream/proxy expects it.
    api_key = (getattr(settings, 'OPENAI_API_KEY', '') or '').strip()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    request_timeout = timeout if timeout is not None else settings.AI_REQUEST_TIMEOUT_SEC
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:500]
        logger.warning('AI completion HTTP %s: %s', exc.code, detail)
        # Some servers reject unknown fields — retry once without thinking knobs.
        if disable_thinking and exc.code in (400, 422):
            return chat_completion(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                disable_thinking=False,
            )
        raise RuntimeError(f'LLM HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        logger.warning('AI completion network error: %s', exc)
        raise RuntimeError('LLM unavailable') from exc

    data = json.loads(raw)
    choices = data.get('choices') or []
    if not choices:
        return ''
    message = choices[0].get('message') or {}
    content = message.get('content') or ''
    if not content:
        # Some thinking builds put the final answer elsewhere.
        content = (
            message.get('reasoning_content')
            or message.get('reasoning')
            or choices[0].get('text')
            or ''
        )
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get('type') == 'text':
                parts.append(item.get('text') or '')
        content = ''.join(parts)
    return clean_completion_text(str(content))
