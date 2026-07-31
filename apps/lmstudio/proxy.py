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


def upstream_host() -> str:
    """LM Studio root (without trailing /v1) for native REST like /api/v1/models/load."""
    base = upstream_base()
    if base.endswith('/v1'):
        return base[:-3].rstrip('/') or 'http://127.0.0.1:1234'
    return base


def upstream_api_key() -> str:
    return (getattr(settings, 'LMSTUDIO_UPSTREAM_API_KEY', '') or 'lm-studio').strip()


def embedding_model() -> str:
    return (
        getattr(settings, 'LM_STUDIO_EMBEDDING_MODEL', '')
        or 'Content-AI/USER-bge-m3-Q8_0-GGUF'
    ).strip()


def embedding_context_length() -> int:
    return int(getattr(settings, 'LM_STUDIO_EMBEDDING_CONTEXT_LENGTH', 8192))


def auto_load_enabled() -> bool:
    return bool(getattr(settings, 'LM_STUDIO_AUTO_LOAD_MODEL', True))


def _request_json(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
    absolute_url: str | None = None,
) -> tuple[int, Any]:
    url = absolute_url or f'{upstream_base()}{path}'
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


def ensure_embedding_model_loaded() -> tuple[int, Any] | None:
    """Optionally load the embedding model into LM Studio (not the chat model)."""
    if not auto_load_enabled():
        return None
    model = embedding_model()
    if not model:
        return None
    url = f'{upstream_host()}/api/v1/models/load'
    payload = {
        'model': model,
        'context_length': embedding_context_length(),
    }
    status, body = _request_json(
        'POST',
        '/api/v1/models/load',
        payload=payload,
        timeout=120,
        absolute_url=url,
    )
    if status >= 400:
        logger.warning(
            'LM Studio embedding auto-load failed model=%s status=%s body=%s',
            model,
            status,
            body,
        )
    return status, body


def health() -> tuple[int, dict[str, Any]]:
    status, body = _request_json('GET', '/models', timeout=8)
    embed_id = embedding_model()
    if status == 200:
        models = body.get('data') if isinstance(body, dict) else None
        count = len(models) if isinstance(models, list) else 0
        return 200, {
            'status': 'ok',
            'upstream': upstream_base(),
            'model': FORCED_MODEL,
            'embedding_model': embed_id,
            'models_count': count,
        }
    return status, {
        'status': 'error',
        'upstream': upstream_base(),
        'model': FORCED_MODEL,
        'embedding_model': embed_id,
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


def embeddings(payload: dict[str, Any]) -> tuple[int, Any]:
    """Proxy embeddings; always force LM_STUDIO_EMBEDDING_MODEL (ignore client model)."""
    if not isinstance(payload, dict):
        return 400, {'detail': 'JSON object expected'}

    raw_input = payload.get('input')
    if isinstance(raw_input, str):
        if not raw_input.strip():
            return 400, {'detail': 'input must be a non-empty string or array'}
    elif isinstance(raw_input, list):
        if not raw_input:
            return 400, {'detail': 'input must be a non-empty string or array'}
        for index, item in enumerate(raw_input):
            if not isinstance(item, str):
                return 400, {'detail': f'input[{index}] must be a string'}
    else:
        return 400, {'detail': 'input must be a string or array of strings'}

    ensure_embedding_model_loaded()

    upstream_payload: dict[str, Any] = {
        'model': embedding_model(),
        'input': raw_input,
        'encoding_format': payload.get('encoding_format') or 'float',
    }
    return _request_json(
        'POST',
        '/embeddings',
        payload=upstream_payload,
        timeout=float(getattr(settings, 'AI_REQUEST_TIMEOUT_SEC', 45)),
    )
