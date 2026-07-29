import json
import logging
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

FORCED_MODEL = 'qwen3-vl-8b-thinking'


def upstream_base() -> str:
    return (getattr(settings, 'LMSTUDIO_UPSTREAM_BASE_URL', '') or 'http://127.0.0.1:1234/v1').rstrip('/')


def upstream_api_key() -> str:
    return (getattr(settings, 'LMSTUDIO_UPSTREAM_API_KEY', '') or 'lm-studio').strip()


def _request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> tuple[int, Any]:
    url = f'{upstream_base()}{path}'
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    api_key = upstream_api_key()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    request_timeout = timeout if timeout is not None else float(
        getattr(settings, 'AI_REQUEST_TIMEOUT_SEC', 45)
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            raw = response.read().decode('utf-8')
            status = getattr(response, 'status', 200)
            if not raw:
                return status, {}
            return status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        try:
            body = json.loads(detail) if detail else {'detail': detail}
        except json.JSONDecodeError:
            body = {'detail': detail[:1000] or f'Upstream HTTP {exc.code}'}
        return exc.code, body
    except urllib.error.URLError as exc:
        logger.warning('LM Studio upstream unavailable: %s', exc)
        return 503, {'detail': 'LM Studio upstream unavailable'}


def health() -> tuple[int, dict[str, Any]]:
    status, body = _request_json('GET', '/models', timeout=8)
    if status == 200:
        models = body.get('data') if isinstance(body, dict) else None
        count = len(models) if isinstance(models, list) else 0
        return 200, {
            'status': 'ok',
            'upstream': upstream_base(),
            'model': FORCED_MODEL,
            'models_count': count,
        }
    return status, {
        'status': 'error',
        'upstream': upstream_base(),
        'model': FORCED_MODEL,
        'detail': body.get('detail') if isinstance(body, dict) else body,
    }


def list_models() -> tuple[int, Any]:
    status, body = _request_json('GET', '/models', timeout=15)
    if status != 200 or not isinstance(body, dict):
        return status, body

    data = body.get('data')
    if not isinstance(data, list):
        data = []

    # Prefer the forced model; keep others for diagnostics.
    forced = {
        'id': FORCED_MODEL,
        'object': 'model',
        'owned_by': 'lmstudio-proxy',
    }
    others = [item for item in data if isinstance(item, dict) and item.get('id') != FORCED_MODEL]
    return 200, {
        'object': 'list',
        'data': [forced, *others],
    }


def chat_completions(payload: dict[str, Any]) -> tuple[int, Any]:
    if not isinstance(payload, dict):
        return 400, {'detail': 'JSON object expected'}

    messages = payload.get('messages')
    if not isinstance(messages, list) or not messages:
        return 400, {'detail': 'messages is required'}

    # OpenAI text + VL content parts are passed through as-is.
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return 400, {'detail': f'messages[{index}] must be an object'}
        if 'role' not in message or 'content' not in message:
            return 400, {'detail': f'messages[{index}] requires role and content'}
        content = message.get('content')
        if not isinstance(content, (str, list)):
            return 400, {'detail': f'messages[{index}].content must be string or array'}

    upstream_payload = dict(payload)
    upstream_payload['model'] = FORCED_MODEL
    upstream_payload.setdefault('stream', False)
    if upstream_payload.get('stream'):
        return 400, {'detail': 'stream is not supported by this proxy'}

    return _request_json(
        'POST',
        '/chat/completions',
        payload=upstream_payload,
        timeout=float(getattr(settings, 'AI_REQUEST_TIMEOUT_SEC', 45)),
    )
