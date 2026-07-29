import hashlib
import json
import logging
import re
from datetime import datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from apps.ai.client import chat_completion
from apps.ai.models import PartnerStyleProfile, UserStyleProfile

logger = logging.getLogger(__name__)

MAX_SAMPLES = 80
SAMPLE_MAX_LEN = 280
MIN_SAMPLE_LEN = 12
TRAITS_EVERY_N = 25
MAX_DAY_MESSAGES = 60
MAX_DAY_CHARS = 4500


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
        score = index
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
    return list(reversed(picked))


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


def _cache_key(user_id, draft: str, chat_id: str | None) -> str:
    digest = hashlib.sha256(f'{user_id}:{chat_id or ""}:{draft}'.encode('utf-8')).hexdigest()[:32]
    return f'ai:complete:{digest}'


def strip_draft_prefix(suggestion: str, draft: str) -> str:
    suggestion = suggestion or ''
    draft = draft or ''
    if not suggestion:
        return ''
    if suggestion.startswith(draft):
        suggestion = suggestion[len(draft):]
    draft_stripped = draft.rstrip()
    if draft_stripped and suggestion.startswith(draft_stripped):
        suggestion = suggestion[len(draft_stripped):]
    return suggestion.lstrip('\n')


_META_SUGGESTION_RE = re.compile(
    r'(?i)^\s*('
    r'хорошо[,!]?\s|'
    r'мне нужно|'
    r'нужно понять|'
    r'пользователь|'
    r'продолжить черновик|'
    r'верн[уи]\s+только|'
    r'в черновике|'
    r'thinking|'
    r'final\s*:'
    r')'
)


def sanitize_suggestion(suggestion: str) -> str:
    """Drop CoT / prompt leaks that must never reach the composer ghost."""
    text = (suggestion or '').replace('\r\n', '\n')
    if not text:
        return ''
    # Model sometimes appends CoT after a good first line.
    text = text.split('\n', 1)[0].strip()
    if not text or _META_SUGGESTION_RE.search(text):
        return ''
    if len(text) > 180:
        text = text[:180].rstrip()
    return text


def _local_day_bounds(now=None):
    tz_name = getattr(settings, 'TIME_ZONE', 'Europe/Moscow')
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.get_current_timezone()
    now = timezone.localtime(now or timezone.now(), tz)
    start = datetime.combine(now.date(), time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end, now.date().isoformat()


def format_day_transcript(messages, current_user_id) -> str:
    lines = []
    total = 0
    for msg in messages:
        sender = msg.sender
        mine = str(sender.id) == str(current_user_id)
        label = 'Я' if mine else (f'@{sender.nickname}' if getattr(sender, 'nickname', None) else 'Собеседник')
        text = normalize_sample(msg.content or '')
        if not text:
            continue
        if msg.message_type != 'text':
            continue
        line = f'{label}: {text}'
        if total + len(line) > MAX_DAY_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    return '\n'.join(lines)


def load_today_messages(chat_id, user) -> tuple[list, str]:
    from apps.chats.models import Message, MessageType
    from apps.chats.services import user_in_chat
    from apps.chats.models import Chat

    if not chat_id:
        return [], ''
    try:
        chat = Chat.objects.get(id=chat_id)
    except (Chat.DoesNotExist, ValueError, TypeError):
        return [], ''
    if not user_in_chat(chat, user):
        return [], ''

    start, end, _ = _local_day_bounds()
    qs = (
        Message.objects.filter(
            chat=chat,
            deleted_at__isnull=True,
            message_type=MessageType.TEXT,
            sent_at__gte=start,
            sent_at__lt=end,
        )
        .exclude(hidden_for__user=user)
        .select_related('sender')
        .order_by('sent_at')
    )
    messages = list(qs[:MAX_DAY_MESSAGES])
    return messages, format_day_transcript(messages, user.id)


def get_partner_for_chat(chat, user):
    from apps.chats.models import ChatType

    if chat.chat_type != ChatType.DIRECT:
        return None
    peer = (
        chat.participants.exclude(user=user)
        .select_related('user')
        .first()
    )
    return peer.user if peer else None


def get_partner_style(user, partner) -> PartnerStyleProfile | None:
    if not partner:
        return None
    return (
        PartnerStyleProfile.objects.filter(user=user, partner=partner)
        .only('notes', 'traits', 'updated_at')
        .first()
    )


def build_completion_messages(
    draft: str,
    samples: list[str],
    traits: dict | None,
    day_transcript: str = '',
    partner_notes: str = '',
    partner_traits: dict | None = None,
) -> list[dict[str, str]]:
    traits = traits or {}
    partner_traits = partner_traits or {}
    traits_bits = []
    for key in ('tone', 'formality', 'emoji', 'length', 'notes'):
        value = traits.get(key)
        if value:
            traits_bits.append(f'{key}: {value}')
    traits_line = '; '.join(traits_bits) if traits_bits else 'недостаточно данных'

    partner_bits = []
    for key in ('tone', 'formality', 'emoji', 'length', 'notes'):
        value = partner_traits.get(key)
        if value:
            partner_bits.append(f'{key}: {value}')
    partner_line = '; '.join(partner_bits) if partner_bits else ''
    if partner_notes:
        partner_line = (partner_line + '; ' if partner_line else '') + partner_notes

    # Keep prompts short: long Russian system prompts make qwen3-*-thinking
    # return empty content. Compact context + assistant-prefill of the draft
    # yields a real suffix in `content`.
    sample_bits = ' | '.join(
        s.replace('\n', ' ').strip()[:80]
        for s in (samples or [])[:4]
        if isinstance(s, str) and s.strip()
    ) or '(нет)'
    day_compact = ' / '.join(
        line.strip()
        for line in (day_transcript or '').splitlines()
        if line.strip()
    )[:320] or '(пусто)'

    system = (
        'Continue assistant draft in Russian, suffix only. '
        'No quotes, no explanations, no markdown.'
    )
    context = (
        f'style={traits_line}; '
        f'partner={partner_line or "n/a"}; '
        f'samples={sample_bits}; '
        f'today={day_compact}'
    )
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': context},
        {'role': 'assistant', 'content': draft},
    ]


def complete_draft(user, draft: str, chat_id: str | None = None) -> dict:
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

    cache_key = _cache_key(user.id, draft, chat_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return {
            'suggestion': cached,
            'request_id': request_id,
            'cached': True,
        }

    samples = select_style_samples(profile, draft)
    _, day_transcript = load_today_messages(chat_id, user)

    partner_notes = ''
    partner_traits = {}
    if chat_id:
        from apps.chats.models import Chat
        try:
            chat = Chat.objects.get(id=chat_id)
            partner = get_partner_for_chat(chat, user)
            partner_style = get_partner_style(user, partner)
            if partner_style:
                partner_notes = (partner_style.notes or '').strip()
                if isinstance(partner_style.traits, dict):
                    partner_traits = partner_style.traits
        except (Chat.DoesNotExist, ValueError, TypeError):
            pass

    messages = build_completion_messages(
        draft,
        samples,
        profile.traits if isinstance(profile.traits, dict) else {},
        day_transcript=day_transcript,
        partner_notes=partner_notes,
        partner_traits=partner_traits,
    )

    token_budget = max(int(settings.AI_MAX_TOKENS), 48)
    try:
        suggestion = ''
        # qwen3-*-thinking often returns empty content; short retries help.
        # disable_thinking knobs are ignored by this proxy and can hurt hit-rate.
        for temperature in (0.75, 0.9, 0.55, 0.8, 0.65):
            raw = chat_completion(
                messages,
                max_tokens=token_budget,
                temperature=temperature,
                disable_thinking=False,
            )
            suggestion = sanitize_suggestion(strip_draft_prefix(raw, draft))
            if suggestion:
                break
    except Exception as exc:
        logger.exception('AI complete failed for user=%s', user.id)
        detail = 'llm_unavailable' if 'unavailable' in str(exc).lower() else 'llm_error'
        return {'suggestion': '', 'request_id': request_id, 'error': True, 'detail': detail}

    # Never cache empty / failed answers — otherwise a downtime sticks for 30s.
    if suggestion:
        cache.set(cache_key, suggestion, timeout=30)
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
        raw = chat_completion(messages, max_tokens=200, temperature=0.3, timeout=30)
    except Exception:
        logger.exception('traits refresh failed user=%s', profile.user_id)
        return

    traits = {}
    try:
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


def _parse_style_payload(raw: str) -> tuple[dict, str]:
    traits = {}
    notes = ''
    try:
        match = re.search(r'\{[\s\S]*\}', raw or '')
        if match:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                for key in ('tone', 'formality', 'emoji', 'length', 'notes'):
                    if data.get(key):
                        traits[key] = str(data[key])[:200]
                notes = str(data.get('summary') or data.get('notes') or '').strip()[:800]
    except Exception:
        notes = (raw or '').strip()[:800]
    if not notes and traits.get('notes'):
        notes = traits['notes']
    return traits, notes


def load_partner_transcript(user, partner, chat, *, limit: int = MAX_DAY_MESSAGES):
    """Recent text exchange between user and partner (prefer today, fall back to latest)."""
    from apps.chats.models import Message, MessageType

    start, end, day_key = _local_day_bounds()
    base = Message.objects.filter(
        chat=chat,
        deleted_at__isnull=True,
        message_type=MessageType.TEXT,
    ).filter(Q(sender=user) | Q(sender=partner)).select_related('sender')

    today_qs = base.filter(sent_at__gte=start, sent_at__lt=end).order_by('sent_at')
    messages = list(today_qs[:limit])
    if len(messages) < 4:
        # Not enough today — take a rolling recent window so style keeps adapting.
        messages = list(base.order_by('-sent_at')[:limit])
        messages.reverse()
    return messages, day_key


def analyze_partner_day_style(user, partner, chat, day_key: str | None = None) -> PartnerStyleProfile | None:
    """Adapt partner-specific style from the latest real conversation."""
    messages, today_key = load_partner_transcript(user, partner, chat)
    day_key = day_key or today_key

    own_count = sum(1 for m in messages if m.sender_id == user.id)
    if own_count < 1 or len(messages) < 2:
        return None

    transcript = format_day_transcript(messages, user.id)
    if not transcript.strip():
        return None

    existing = PartnerStyleProfile.objects.filter(user=user, partner=partner).first()
    prev_notes = (existing.notes or '').strip() if existing else ''
    prev_traits = existing.traits if existing and isinstance(existing.traits, dict) else {}

    my_name = getattr(user, 'nickname', None) or 'user'
    partner_name = getattr(partner, 'nickname', None) or 'partner'
    if prev_notes:
        prev_block = prev_notes
    elif prev_traits:
        prev_block = json.dumps(prev_traits, ensure_ascii=False)
    else:
        prev_block = 'пока нет'

    prompt_messages = [
        {
            'role': 'system',
            'content': (
                'Ты обновляешь профиль стиля общения пользователя с КОНКРЕТНЫМ собеседником. '
                'Опирайся на реальную переписку: как пользователь пишет именно этому человеку '
                '(тон, ты/вы, сленг, длина фраз, emoji, шутки, формальность). '
                'Если есть предыдущий профиль — адаптируй его под свежие сообщения, не игнорируй недавние изменения. '
                'Ответь одним JSON без markdown со ключами: '
                'tone, formality, emoji, length, notes, summary. '
                'summary — 1–3 предложения на русском про актуальные особенности общения с этим человеком.'
            ),
        },
        {
            'role': 'user',
            'content': (
                f'Пользователь: @{my_name}\n'
                f'Собеседник: @{partner_name}\n'
                f'Предыдущий профиль стиля с этим собеседником:\n{prev_block}\n\n'
                f'Актуальная переписка (метка «Я» = сообщения пользователя):\n'
                f'{transcript}'
            ),
        },
    ]
    try:
        raw = chat_completion(prompt_messages, max_tokens=350, temperature=0.35, timeout=45)
    except Exception:
        logger.exception(
            'partner style analyze failed user=%s partner=%s',
            user.id,
            partner.id,
        )
        return None

    traits, notes = _parse_style_payload(raw)
    if not notes and not traits:
        return None

    profile, _ = PartnerStyleProfile.objects.update_or_create(
        user=user,
        partner=partner,
        defaults={
            'chat': chat,
            'notes': notes,
            'traits': traits,
            'last_day_key': day_key,
            'messages_since_refresh': 0,
        },
    )
    return profile


def analyze_user_day_partner_styles(user_id: str) -> dict:
    """Offline: refresh partner-specific style notes for today's active DMs."""
    from apps.chats.models import Chat, ChatType, Message, MessageType
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return {'ok': False, 'reason': 'user_missing'}

    start, end, day_key = _local_day_bounds()
    chat_ids = (
        Message.objects.filter(
            deleted_at__isnull=True,
            message_type=MessageType.TEXT,
            sent_at__gte=start,
            sent_at__lt=end,
        )
        .filter(Q(sender=user) | Q(chat__participants__user=user))
        .values_list('chat_id', flat=True)
        .distinct()
    )

    updated = 0
    skipped = 0
    for chat in Chat.objects.filter(id__in=chat_ids, chat_type=ChatType.DIRECT).distinct():
        partner = get_partner_for_chat(chat, user)
        if not partner:
            skipped += 1
            continue
        result = analyze_partner_day_style(user, partner, chat, day_key=day_key)
        if result:
            updated += 1
        else:
            skipped += 1

    return {'ok': True, 'updated': updated, 'skipped': skipped, 'day': day_key}
