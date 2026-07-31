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
# How much of today's chat we put into the completion prompt (recent tail).
COMPLETION_DAY_CHARS = 1800
COMPLETION_DAY_LINES = 36
INTENT_CACHE_TTL_SEC = 60
INTENT_DAY_TAIL_LINES = 20
INTENT_DRAFT_PREFIX_LEN = 40
RECENT_FOCUS_TURNS = 3
EMPTY_INTENT = {'topic': '', 'reply_goal': ''}


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


def sanitize_suggestion(suggestion: str, *, max_chars: int = 280) -> str:
    """Drop CoT / prompt leaks; allow a short multi-line suffix when needed."""
    text = (suggestion or '').replace('\r\n', '\n').strip()
    if not text:
        return ''
    lines = []
    for raw_line in text.split('\n'):
        line = raw_line.rstrip()
        if _META_SUGGESTION_RE.search(line):
            break
        if line.strip() == '' and not lines:
            continue
        lines.append(line)
        if len(lines) >= 5:
            break
    text = '\n'.join(lines).strip()
    if not text or _META_SUGGESTION_RE.search(text):
        return ''
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def build_forced_continuation(
    messages: list[dict[str, str]],
    draft: str,
) -> tuple[list[dict[str, str]], str]:
    """
    Make a semantically complete short draft visibly unfinished for LM Studio.

    Qwen may legitimately emit EOS for text like "ну как". A virtual comma
    forces continuation; the same comma+space is restored in the UI suffix.
    """
    stripped = (draft or '').rstrip()
    if not stripped or stripped[-1] in ',.:;!?…—-':
        return messages, ''
    forced = [dict(message) for message in messages]
    forced[-1]['content'] = f'{stripped},'
    return forced, ', '


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


def format_day_transcript(messages, current_user_id, *, max_chars: int | None = MAX_DAY_CHARS) -> str:
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
        if max_chars is not None and total + len(line) > max_chars:
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
    # Take the *latest* messages of the day so the model sees the current stage.
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
        .order_by('-sent_at')
    )
    messages = list(reversed(list(qs[:MAX_DAY_MESSAGES])))
    # Completion uses the full loaded day. The global 125k token budget in the
    # client trims oldest context only when the complete request is too large.
    return messages, format_day_transcript(messages, user.id, max_chars=None)


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


def _tail_day_transcript(day_transcript: str) -> str:
    lines = [ln.strip() for ln in (day_transcript or '').splitlines() if ln.strip()]
    if not lines:
        return '(сообщений за сегодня пока нет)'
    if len(lines) > COMPLETION_DAY_LINES:
        lines = lines[-COMPLETION_DAY_LINES:]
    text = '\n'.join(lines)
    if len(text) > COMPLETION_DAY_CHARS:
        text = text[-COMPLETION_DAY_CHARS:]
        cut = text.find('\n')
        if 0 <= cut < 80:
            text = text[cut + 1:]
    return text


def _last_labeled_line(day_transcript: str, *, mine: bool) -> str:
    for line in reversed((day_transcript or '').splitlines()):
        line = line.strip()
        if not line:
            continue
        if mine:
            if line.startswith('Я:'):
                return line.split(':', 1)[1].strip()
        elif not line.startswith('Я:') and ':' in line:
            return line.split(':', 1)[1].strip()
    return ''


def day_transcript_to_messages(day_transcript: str) -> list[dict[str, str]]:
    """
    Convert today's labeled transcript to real chat roles.

    The current Monica user is represented as assistant because the model is
    continuing their message; the partner is represented as user.
    """
    result: list[dict[str, str]] = []
    for raw_line in (day_transcript or '').splitlines():
        line = raw_line.strip()
        if not line or ':' not in line:
            continue
        label, text = line.split(':', 1)
        text = text.strip()
        if not text:
            continue
        role = 'assistant' if label.strip() == 'Я' else 'user'
        # Merge consecutive messages from one sender while preserving their
        # order and message boundaries.
        if result and result[-1]['role'] == role:
            result[-1]['content'] = f"{result[-1]['content']}\n{text}"
        else:
            result.append({'role': role, 'content': text})
    return result


def recent_focus_turns(
    day_transcript: str,
    *,
    n: int = RECENT_FOCUS_TURNS,
) -> str:
    """Last N labeled lines — the immediate conversational focus."""
    lines = [ln.strip() for ln in (day_transcript or '').splitlines() if ln.strip() and ':' in ln]
    if not lines:
        return '(нет)'
    return '\n'.join(lines[-max(1, n):])


def parse_reply_intent(raw: str) -> dict[str, str]:
    """Parse `{topic, reply_goal}` JSON from an LLM response; never raise."""
    if not raw or not isinstance(raw, str):
        return dict(EMPTY_INTENT)
    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return dict(EMPTY_INTENT)
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            return dict(EMPTY_INTENT)
        topic = str(data.get('topic') or '').strip()[:160]
        reply_goal = str(data.get('reply_goal') or '').strip()[:200]
        return {'topic': topic, 'reply_goal': reply_goal}
    except Exception:
        return dict(EMPTY_INTENT)


def _intent_cache_key(user_id, chat_id, day_transcript: str, draft: str) -> str:
    lines = [ln.strip() for ln in (day_transcript or '').splitlines() if ln.strip()]
    day_tail = '\n'.join(lines[-INTENT_DAY_TAIL_LINES:])
    draft_prefix = (draft or '')[:INTENT_DRAFT_PREFIX_LEN]
    digest = hashlib.sha256(f'{day_tail}\n---\n{draft_prefix}'.encode('utf-8')).hexdigest()[:16]
    return f'ai:intent:{user_id}:{chat_id or "none"}:{digest}'


def infer_reply_intent(
    *,
    user_id,
    chat_id: str | None,
    day_transcript: str,
    draft: str,
    traits_line: str = '',
) -> dict[str, str]:
    """
    First-pass LLM call: infer conversation topic and the user's reply goal
    before generating a completion suffix.
    """
    cache_key = _intent_cache_key(user_id, chat_id, day_transcript, draft)
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and ('topic' in cached or 'reply_goal' in cached):
        return {
            'topic': str(cached.get('topic') or '').strip()[:160],
            'reply_goal': str(cached.get('reply_goal') or '').strip()[:200],
        }

    day_block = _tail_day_transcript(day_transcript)
    recent_block = recent_focus_turns(day_transcript)
    draft_text = draft if draft is not None else ''
    # Assistant prefill forces content-channel JSON; without it qwen-thinking
    # often returns empty content / CoT-only for this short structured call.
    intent_prefill = '{"topic":"'
    messages = [
        {
            'role': 'system',
            'content': (
                'По последним репликам и черновику автора определи: '
                '1) topic — о чём сейчас говорят (коротко); '
                '2) reply_goal — конкретное, что автор допишет в этот черновик. '
                'reply_goal пиши от лица автора как действие: '
                '«спросить…», «уточнить…», «объяснить…», «согласиться…», '
                '«отшутиться…», «рассказать…». '
                'Нельзя писать мета-цели вроде «понять диалог», '
                '«разобраться в ситуации», «продолжить обсуждение». '
                'Опирайся в первую очередь на последние 2–3 реплики и черновик. '
                'Продолжи JSON со строковыми ключами topic и reply_goal на русском. '
                'Если данных мало, оставь значения пустыми.'
            ),
        },
        {
            'role': 'user',
            'content': (
                f'стиль_автора: {traits_line or "недостаточно данных"}\n'
                f'=== Последние реплики (главный фокус) ===\n{recent_block}\n\n'
                f'=== История за сегодня ===\n{day_block}\n\n'
                f'=== Черновик автора (его нужно продолжить) ===\n{draft_text or "(пусто)"}\n\n'
                'Пример хорошего reply_goal: "уточнить, кого имеют в виду". '
                'Плохой reply_goal: "попытаться понять, о чем говорят". '
                'Продолжи JSON: {"topic":"...","reply_goal":"..."}'
            ),
        },
        {'role': 'assistant', 'content': intent_prefill},
    ]
    try:
        raw = chat_completion(
            messages,
            max_tokens=100,
            temperature=0.2,
            timeout=20,
            disable_thinking=True,
        )
        reconstructed = (raw or '').strip()
        if reconstructed and not reconstructed.startswith('{'):
            reconstructed = f'{intent_prefill}{reconstructed.lstrip()}'
        intent = parse_reply_intent(reconstructed)
    except Exception:
        logger.exception('AI intent inference failed user=%s chat=%s', user_id, chat_id)
        intent = dict(EMPTY_INTENT)

    cache.set(cache_key, intent, timeout=INTENT_CACHE_TTL_SEC)
    return intent


def infer_length_target(draft: str, day_transcript: str = '', traits: dict | None = None) -> str:
    """Hint for the model: short / medium / long continuation."""
    draft = draft or ''
    traits = traits or {}
    trait_len = str(traits.get('length') or '').lower()
    draft_len = len(draft.strip())
    recent = _tail_day_transcript(day_transcript)
    partner_last = _last_labeled_line(day_transcript, mine=False)
    partner_len = len(partner_last)

    # Explicit style trait wins when clear.
    if any(x in trait_len for x in ('корот', 'short', 'лакон', 'кратк')):
        return 'short'
    if any(x in trait_len for x in ('длин', 'long', 'развёрн', 'подроб')):
        return 'long'

    # Draft already long → finish briefly; short chatty drafts stay short.
    if draft_len >= 160 or draft.count('\n') >= 2:
        return 'medium'
    if partner_len >= 180:
        return 'medium'
    # Look at recent own messages length for this stage.
    own_lens = [
        len(ln.split(':', 1)[1].strip())
        for ln in recent.splitlines()
        if ln.startswith('Я:') and ':' in ln
    ]
    if own_lens:
        avg = sum(own_lens[-6:]) / max(len(own_lens[-6:]), 1)
        if avg <= 40:
            return 'short'
        if avg >= 120:
            return 'long'
    if draft_len <= 50 and partner_len <= 80:
        return 'short'
    return 'medium'


def build_completion_messages(
    draft: str,
    samples: list[str],
    traits: dict | None,
    day_transcript: str = '',
    partner_notes: str = '',
    partner_traits: dict | None = None,
    topic: str = '',
    reply_goal: str = '',
) -> list[dict[str, str]]:
    """
    Prompt package for continuation:
    1) how I talk with this partner
    2) inferred topic + reply goal
    3) today's chat history
    4) my full current composer text
    → ask to continue that exact text.
    """
    traits = traits or {}
    partner_traits = partner_traits or {}
    draft = draft if draft is not None else ''
    topic = (topic or '').strip()
    reply_goal = (reply_goal or '').strip()

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

    sample_bits = ' | '.join(
        s.replace('\n', ' ').strip()[:80]
        for s in (samples or [])[:6]
        if isinstance(s, str) and s.strip()
    ) or '(нет)'
    history_messages = day_transcript_to_messages(day_transcript)
    recent_block = recent_focus_turns(day_transcript)
    length_target = infer_length_target(draft, day_transcript, traits)
    length_rule = {
        'short': 'короткий суффикс (несколько слов / короткая фраза), затем стоп',
        'medium': 'средний суффикс (закончить мысль, 1 короткое предложение), затем стоп',
        'long': 'можно 1–3 коротких предложения, если черновик этого требует, затем стоп',
    }[length_target]
    goal_line = reply_goal or 'закончить мысль по последним репликам'
    topic_line = topic or 'текущий диалог'

    # Keep system compact (thinking models stall on huge system prompts).
    system = (
        'Ты автодополнение в мессенджере. Пишешь ОТ ЛИЦА пользователя. '
        'Главный ориентир — цель_ответа и последние 2–3 реплики; '
        'общая тема вторична. '
        'Продолжи ИМЕННО текущий черновик — верни только новый суффикс '
        '(без повтора уже написанного, без кавычек, без пояснений, без markdown). '
        'Суффикс должен продвигать цель_ответа и звучать как естественная '
        'реакция на последние реплики собеседника. '
        'Не уходи в общие фразы, не меняй тему, не начинай новое сообщение. '
        f'Длина: {length_rule}. '
        'История ниже: user — собеседник, assistant — автор черновика. '
        'Остановись, когда сообщение звучит законченным.'
    )
    style_context = (
        '=== Смысл текущего ответа (приоритет) ===\n'
        f'тема: {topic_line}\n'
        f'цель_ответа: {goal_line}\n'
        f'последние_реплики:\n{recent_block}\n'
        f'length_target={length_target}\n'
        '\n'
        '=== Как я общаюсь с этим пользователем ===\n'
        f'общий_стиль: {traits_line}\n'
        f'стиль_с_этим_собеседником: {partner_line or "пока нет"}\n'
        f'примеры_моих_фраз: {sample_bits}\n'
        '\n'
        'Суффикс обязан служить цели_ответа и опираться на последние_реплики.'
    )
    focus_nudge = (
        f'Перед продолжением: цель_ответа = «{goal_line}». '
        f'Последние реплики:\n{recent_block}\n'
        'Продолжи следующий черновик только суффиксом, который выполняет эту цель '
        'и связан с последними репликами. '
        'Запрещены пустые общие хвосты вроде «это», «что», «ну», «как бы» '
        'без конкретной мысли.'
    )
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': style_context},
        *history_messages,
        {'role': 'user', 'content': focus_nudge},
        {'role': 'assistant', 'content': draft},
    ]


def complete_draft(user, draft: str, chat_id: str | None = None) -> dict:
    request_id = str(uuid4())
    # Keep the composer text as-is (including inner newlines); only normalize None.
    draft = draft if draft is not None else ''

    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'suggestion': '', 'request_id': request_id, 'disabled': True}

    min_len = int(getattr(settings, 'AI_MIN_DRAFT_LEN', 1))
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

    traits = profile.traits if isinstance(profile.traits, dict) else {}
    traits_bits = []
    for key in ('tone', 'formality', 'emoji', 'length', 'notes'):
        value = traits.get(key)
        if value:
            traits_bits.append(f'{key}: {value}')
    traits_line = '; '.join(traits_bits) if traits_bits else ''

    intent = infer_reply_intent(
        user_id=user.id,
        chat_id=chat_id,
        day_transcript=day_transcript,
        draft=draft,
        traits_line=traits_line,
    )
    length_target = infer_length_target(draft, day_transcript, traits)
    messages = build_completion_messages(
        draft,
        samples,
        traits,
        day_transcript=day_transcript,
        partner_notes=partner_notes,
        partner_traits=partner_traits,
        topic=intent.get('topic', ''),
        reply_goal=intent.get('reply_goal', ''),
    )

    base_tokens = max(int(settings.AI_MAX_TOKENS), 48)
    token_budget = {
        'short': max(base_tokens, 48),
        'medium': max(base_tokens, 96),
        'long': max(base_tokens, 160),
    }[length_target]
    max_chars = {'short': 120, 'medium': 200, 'long': 320}[length_target]
    try:
        suggestion = ''
        attempts = [(messages, '', 0.7)]
        forced_messages, forced_prefix = build_forced_continuation(messages, draft)
        if forced_prefix:
            attempts.extend(
                (forced_messages, forced_prefix, temperature)
                for temperature in (0.55, 0.75, 0.9)
            )
        else:
            attempts.extend(
                (messages, '', temperature)
                for temperature in (0.55, 0.8)
            )

        for attempt_messages, prefix, temperature in attempts:
            raw = chat_completion(
                attempt_messages,
                max_tokens=token_budget,
                temperature=temperature,
                disable_thinking=False,
            )
            candidate = strip_draft_prefix(raw, draft)
            if prefix and candidate:
                candidate = f'{prefix}{candidate.lstrip()}'
            suggestion = sanitize_suggestion(
                candidate,
                max_chars=max_chars,
            )
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
