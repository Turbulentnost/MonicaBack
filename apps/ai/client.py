import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

THINK_RE = re.compile(
    r'<think>[\s\S]*?</think>|<thinking>[\s\S]*?</thinking>',
    re.IGNORECASE,
)


def clean_completion_text(text: str) -> str:
    cleaned = THINK_RE.sub('', text or '')
    cleaned = cleaned.strip().strip('"').strip("'")
    return cleaned.strip()


def chat_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> str:
    """Call OpenAI-compatible chat completions (LM Studio /v1)."""
    base = (getattr(settings, 'OPENAI_BASE_URL', '') or '').rstrip('/')
    if not base:
        raise RuntimeError('OPENAI_BASE_URL is not configured')

    url = f'{base}/chat/completions'
    payload: dict[str, Any] = {
        'model': getattr(settings, 'OPENAI_MODEL', 'qwen3-vl-8b-thinking'),
        'messages': messages,
        'max_tokens': max_tokens if max_tokens is not None else settings.AI_MAX_TOKENS,
        'temperature': temperature if temperature is not None else 0.6,
        'stream': False,
    }
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    api_key = getattr(settings, 'OPENAI_API_KEY', '') or ''
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
    if isinstance(content, list):
        # Some VL models return content parts
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get('type') == 'text':
                parts.append(item.get('text') or '')
        content = ''.join(parts)
    return clean_completion_text(str(content))
