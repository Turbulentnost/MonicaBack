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
TRIM_MARKER = '[earlier context trimmed]\n'


def estimate_tokens(value: Any) -> int:
    """
    Conservative tokenizer-free estimate.

    UTF-8 bytes / 2 intentionally overestimates most Latin text and is close
    enough for Cyrillic. This keeps requests below the configured hard limit
    without coupling the backend to a model-specific tokenizer.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, (len(value.encode('utf-8')) + 1) // 2)
    if isinstance(value, dict):
        return 2 + sum(estimate_tokens(key) + estimate_tokens(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(estimate_tokens(item) for item in value)
    return estimate_tokens(str(value))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    # Small per-message allowance covers chat-template role/separator tokens.
    return 3 + sum(6 + estimate_tokens(message) for message in messages)


def should_continue_final_message(messages: list[dict[str, Any]]) -> bool:
    """LM Studio must be told explicitly to continue a non-empty assistant prefill."""
    if not messages:
        return False
    last = messages[-1]
    return (
        last.get('role') == 'assistant'
        and bool(last.get('content'))
    )


def _trim_text_start(text: str, tokens_to_remove: int) -> str:
    if not text:
        return text
    # Our estimator uses at most two UTF-8 bytes per estimated token.
    target_bytes = max(1, tokens_to_remove * 2)
    removed = 0
    cut = 0
    for index, char in enumerate(text):
        removed += len(char.encode('utf-8'))
        cut = index + 1
        if removed >= target_bytes:
            break
    tail = text[cut:].lstrip()
    return f'{TRIM_MARKER}{tail}' if tail else TRIM_MARKER.rstrip()


def fit_messages_to_token_budget(
    messages: list[dict[str, Any]],
    *,
    completion_tokens: int,
    context_window_tokens: int | None = None,
    reserve_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """
    Fit the request into the model context window.

    Old non-system context is trimmed first. The final assistant-prefill
    message (the user's complete current draft) is protected.
    """
    window = int(
        context_window_tokens
        if context_window_tokens is not None
        else getattr(settings, 'AI_CONTEXT_WINDOW_TOKENS', 125000)
    )
    reserve = int(
        reserve_tokens
        if reserve_tokens is not None
        else getattr(settings, 'AI_CONTEXT_RESERVE_TOKENS', 512)
    )
    prompt_budget = max(256, window - max(0, int(completion_tokens)) - max(0, reserve))
    fitted = [dict(message) for message in messages]
    estimated = estimate_messages_tokens(fitted)
    if estimated <= prompt_budget:
        return fitted

    protected_last = (
        len(fitted) - 1
        if fitted and fitted[-1].get('role') == 'assistant'
        else None
    )
    candidates = [
        index
        for index, message in enumerate(fitted)
        if message.get('role') != 'system' and index != protected_last
    ]

    for index in candidates:
        if estimated <= prompt_budget:
            break
        content = fitted[index].get('content')
        overflow = estimated - prompt_budget
        if isinstance(content, str):
            fitted[index]['content'] = _trim_text_start(content, overflow + 16)
        else:
            # Old VL/non-text context is safer to drop as a whole than to
            # corrupt its OpenAI content-part structure.
            fitted[index]['content'] = TRIM_MARKER.rstrip()
        estimated = estimate_messages_tokens(fitted)

    if estimated > prompt_budget:
        logger.warning(
            'AI prompt remains over budget after trimming: estimated=%s budget=%s',
            estimated,
            prompt_budget,
        )
    else:
        logger.info(
            'AI prompt trimmed to token budget: estimated=%s budget=%s',
            estimated,
            prompt_budget,
        )
    return fitted


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

    fitted_messages = fit_messages_to_token_budget(
        messages,
        completion_tokens=token_budget,
    )
    payload: dict[str, Any] = {
        'model': FORCED_MODEL,
        'messages': fitted_messages,
        'max_tokens': token_budget,
        'temperature': temperature if temperature is not None else 0.6,
        'stream': False,
    }
    if should_continue_final_message(fitted_messages):
        # Without this LM Studio treats the assistant prefill as complete and
        # immediately emits EOS (one completion token, empty content).
        payload['continue_final_message'] = True
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
    # Never use reasoning_content for autocomplete — Qwen-thinking dumps CoT there
    # ("Хорошо, мне нужно продолжить черновик…") and it leaks into ghost text.
    if not content:
        content = choices[0].get('text') or ''
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get('type') == 'text':
                parts.append(item.get('text') or '')
        content = ''.join(parts)
    return clean_completion_text(str(content))


def embed_texts(texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
    """
    Call OpenAI-compatible embeddings via LM Studio proxy.
    Proxy forces LM_STUDIO_EMBEDDING_MODEL; client model field is ignored.
    """
    if not texts:
        return []
    cleaned = [str(item or '').replace('\n', ' ').strip() for item in texts]
    if not any(cleaned):
        return [[] for _ in cleaned]

    base = (getattr(settings, 'OPENAI_BASE_URL', '') or '').rstrip('/')
    if not base:
        raise RuntimeError('OPENAI_BASE_URL is not configured')

    url = f'{base}/embeddings/'
    payload: dict[str, Any] = {
        'input': cleaned if len(cleaned) > 1 else cleaned[0],
        'encoding_format': 'float',
    }
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
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
        logger.warning('AI embeddings HTTP %s: %s', exc.code, detail)
        raise RuntimeError(f'LLM embeddings HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        logger.warning('AI embeddings network error: %s', exc)
        raise RuntimeError('LLM embeddings unavailable') from exc

    data = json.loads(raw)
    items = data.get('data') if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [[] for _ in cleaned]

    by_index: dict[int, list[float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get('index', len(by_index)))
        except (TypeError, ValueError):
            index = len(by_index)
        vector = item.get('embedding')
        if isinstance(vector, list):
            by_index[index] = [float(x) for x in vector]

    return [by_index.get(i, []) for i in range(len(cleaned))]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))
