import hashlib
import logging
import re
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache

from apps.ai.client import chat_completion
from apps.ai.models import UserStyleProfile

logger = logging.getLogger(__name__)

MAX_SAMPLES = 80
SAMPLE_MAX_LEN = 280
MIN_SAMPLE_LEN = 12
TRAITS_EVERY_N = 25


def get_or_create_style_profile(user) -> UserStyleProfile:
    profile, _ = UserStyleProfile.objects.get_or_create(user=user)
    return profile


def normalize_sample(text: str) -> str:
    text = (text or '').strip()
    text = re.sub(r'\s+', ' ', text)
    if len(text) > SAMPLE_MAX_LEN:
        text = text[:SAMPLE_MAX_LEN].rstrip()
    return text


def is_usable_sample(text: str) -> bool:
    text = normalize_sample(text)
    if len(text) < MIN_SAMPLE_LEN:
        return False
    # Skip code-like / sticker payloads / storage paths
    if text.startswith('monica-sticker'):
        return False
    if re.match(r'^https?://', text, re.I):
        return False
    if re.search(r'[{};]|def |class |import |function ', text):
        return False
    return True


def append_style_sample(user, text: str) -> UserStyleProfile | None:
    if not is_usable_sample(text):
        return None
    sample = normalize_sample(text)
    profile = get_or_create_style_profile(user)
    samples = list(profile.samples or [])
    if samples and samples[-1] == sample:
        return profile
    samples.append(sample)
    if len(samples) > MAX_SAMPLES:
        samples = samples[-MAX_SAMPLES:]
    profile.samples = samples
    profile.messages_since_traits = int(profile.messages_since_traits or 0) + 1
    profile.save(update_fields=['samples', 'messages_since_traits', 'updated_at'])
    return profile


def select_style_samples(profile: UserStyleProfile, draft: str, limit: int = 16) -> list[str]:
    samples = [normalize_sample(s) for s in (profile.samples or []) if isinstance(s, str)]
    samples = [s for s in samples if s]
    if not samples:
        return []

    draft_l = (draft or '').strip().lower()
    prefix = draft_l[:24]

    scored: list[tuple[int, int, str]] = []
    for index, sample in enumerate(samples):
        score = index  # prefer recent (higher index)
        sample_l = sample.lower()
        if prefix and prefix in sample_l:
            score += 50
        elif draft_l and sample_l.startswith(draft_l[:8]):
            score += 30
        scored.append((score, index, sample))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked = []
    seen = set()
    for _, _, sample in scored:
        if sample in seen:
            continue
        seen.add(sample)
        picked.append(sample)
        if len(picked) >= limit:
            break
    return list(reversed(picked))  # chronological-ish for prompt


def _rate_limit_ok(user_id) -> bool:
    key = f'ai:rate:{user_id}'
    current = cache.get(key)
    limit = int(getattr(settings, 'AI_RATE_PER_MINUTE', 20))
    if current is None:
        cache.set(key, 1, timeout=60)
        return True
    if int(current) >= limit:
        return False
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=60)
    return True


def _cache_key(user_id, draft: str) -> str:
    digest = hashlib.sha256(f'{user_id}:{draft}'.encode('utf-8')).hexdigest()[:32]
    return f'ai:complete:{digest}'


def strip_draft_prefix(suggestion: str, draft: str) -> str:
    suggestion = suggestion or ''
    draft = draft or ''
    if not suggestion:
        return ''
    # Model sometimes repeats the whole draft
    if suggestion.startswith(draft):
        suggestion = suggestion[len(draft):]
    draft_stripped = draft.rstrip()
    if draft_stripped and suggestion.startswith(draft_stripped):
        suggestion = suggestion[len(draft_stripped):]
    return suggestion.lstrip('\n')


def build_completion_messages(draft: str, samples: list[str], traits: dict | None) -> list[dict[str, str]]:
    traits = traits or {}
    traits_bits = []
    for key in ('tone', 'formality', 'emoji', 'length', 'notes'):
        value = traits.get(key)
        if value:
            traits_bits.append(f'{key}: {value}')
    traits_line = '; '.join(traits_bits) if traits_bits else 'недостаточно данных'

    examples = '\n'.join(f'- {s}' for s in samples) if samples else '- (примеров пока нет)'

    system = (
        'Ты помощник автодополнения сообщений в мессенджере Monica. '
        'Продолжи черновик пользователя до конца фразы или короткого сообщения. '
        'Пиши строго от лица пользователя, в его стиле общения. '
        'Верни ТОЛЬКО продолжение текста (суффикс), без кавычек, без пояснений, '
        'без повтора уже набранного черновика. '
        'Не используй markdown. Язык — тот же, что в черновике (обычно русский).'
    )
    user = (
        f'Стиль пользователя (признаки): {traits_line}\n'
        f'Примеры его сообщений:\n{examples}\n\n'
        f'Черновик:\n{draft}\n\n'
        f'Продолжение:'
    )
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]


def complete_draft(user, draft: str, chat_id: str | None = None) -> dict:
    del chat_id  # reserved for future reply-context
    request_id = str(uuid4())
    draft = (draft or '').rstrip()

    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'suggestion': '', 'request_id': request_id, 'disabled': True}

    min_len = int(getattr(settings, 'AI_MIN_DRAFT_LEN', 8))
    if len(draft.strip()) < min_len:
        return {'suggestion': '', 'request_id': request_id}

    profile = get_or_create_style_profile(user)
    if not profile.enabled:
        return {'suggestion': '', 'request_id': request_id, 'disabled': True}

    if not _rate_limit_ok(user.id):
        return {'suggestion': '', 'request_id': request_id, 'rate_limited': True}

    cached = cache.get(_cache_key(user.id, draft))
    if cached is not None:
        return {
            'suggestion': cached,
            'request_id': request_id,
            'cached': True,
        }

    samples = select_style_samples(profile, draft)
    messages = build_completion_messages(draft, samples, profile.traits if isinstance(profile.traits, dict) else {})

    try:
        raw = chat_completion(messages)
    except Exception:
        logger.exception('AI complete failed for user=%s', user.id)
        return {'suggestion': '', 'request_id': request_id, 'error': True}

    suggestion = strip_draft_prefix(raw, draft)
    # Keep suggestion reasonably short for ghost text
    if len(suggestion) > 400:
        suggestion = suggestion[:400].rstrip()

    cache.set(_cache_key(user.id, draft), suggestion, timeout=30)
    return {'suggestion': suggestion, 'request_id': request_id}


def maybe_refresh_traits(profile: UserStyleProfile) -> None:
    if int(profile.messages_since_traits or 0) < TRAITS_EVERY_N:
        return
    samples = [s for s in (profile.samples or []) if isinstance(s, str)][-30:]
    if len(samples) < 5:
        return
    examples = '\n'.join(f'- {s}' for s in samples)
    messages = [
        {
            'role': 'system',
            'content': (
                'По примерам сообщений пользователя кратко опиши стиль. '
                'Ответь одним JSON-объектом без markdown со ключами '
                'tone, formality, emoji, length, notes (короткие строки на русском).'
            ),
        },
        {'role': 'user', 'content': f'Примеры:\n{examples}'},
    ]
    try:
        raw = chat_completion(messages, max_tokens=200, temperature=0.3, timeout=20)
    except Exception:
        logger.exception('traits refresh failed user=%s', profile.user_id)
        return

    traits = {}
    try:
        import json
        # Extract JSON object if model added noise
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            traits = json.loads(match.group(0))
    except Exception:
        traits = {'notes': raw[:200]} if raw else {}

    if isinstance(traits, dict) and traits:
        profile.traits = {
            k: str(v)[:120]
            for k, v in traits.items()
            if k in ('tone', 'formality', 'emoji', 'length', 'notes') and v
        }
        profile.messages_since_traits = 0
        profile.save(update_fields=['traits', 'messages_since_traits', 'updated_at'])
