import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from pgvector.django import CosineDistance

from apps.ai.client import cosine_similarity, embed_texts
from apps.ai.models import ChatTopicSegment, MessageEmbedding
from apps.chats.models import Message, MessageType

logger = logging.getLogger(__name__)


def _is_sticker_like(text: str) -> bool:
    text = (text or '').strip()
    return text.startswith('monica-sticker')


def content_hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def message_embeddable(message: Message) -> bool:
    if message.message_type != MessageType.TEXT:
        return False
    if message.deleted_at:
        return False
    text = (message.content or '').strip()
    if len(text) < 1:
        return False
    if _is_sticker_like(text):
        return False
    return True


def ema_centroid(old: list[float] | None, new: list[float], count: int) -> list[float]:
    if not new:
        return list(old or [])
    if not old or count <= 1:
        return list(new)
    # weight previous centroid by existing count
    weight = max(1, count - 1)
    total = weight + 1
    return [
        ((float(o) * weight) + float(n)) / total
        for o, n in zip(old, new)
    ]


def upsert_message_embedding(message: Message, vector: list[float]) -> MessageEmbedding:
    text = message.content or ''
    obj, _created = MessageEmbedding.objects.update_or_create(
        message=message,
        defaults={
            'chat_id': message.chat_id,
            'user_id': message.sender_id,
            'embedding': vector,
            'content_hash': content_hash(text),
        },
    )
    return obj


def user_can_access_chat(chat_id, user) -> bool:
    """Retrieval/index reads are scoped to chats the user participates in."""
    if not chat_id or not user:
        return False
    from apps.chats.models import Chat
    from apps.chats.services import user_in_chat

    try:
        chat = Chat.objects.get(id=chat_id)
    except (Chat.DoesNotExist, ValueError, TypeError):
        return False
    return bool(user_in_chat(chat, user))


def get_active_segment(chat_id) -> ChatTopicSegment | None:
    return (
        ChatTopicSegment.objects
        .filter(chat_id=chat_id, ended_at__isnull=True)
        .order_by('-started_at')
        .first()
    )


def close_segment(segment: ChatTopicSegment, *, at=None) -> None:
    segment.ended_at = at or timezone.now()
    segment.save(update_fields=['ended_at', 'updated_at'])


def open_segment(message: Message, vector: list[float], label: str = '') -> ChatTopicSegment:
    return ChatTopicSegment.objects.create(
        chat_id=message.chat_id,
        started_at=getattr(message, 'sent_at', None) or timezone.now(),
        ended_at=None,
        anchor_message=message,
        label=(label or '')[:160],
        centroid=vector,
        message_count=1,
    )


def should_start_new_topic(
    *,
    previous: MessageEmbedding | None,
    previous_message: Message | None,
    current_message: Message,
    current_vector: list[float],
) -> bool:
    if previous is None or previous_message is None:
        return True

    gap_minutes = int(getattr(settings, 'AI_TOPIC_GAP_MINUTES', 25))
    prev_at = getattr(previous_message, 'sent_at', None)
    cur_at = getattr(current_message, 'sent_at', None)
    if prev_at and cur_at and (cur_at - prev_at) >= timedelta(minutes=gap_minutes):
        return True

    threshold = float(getattr(settings, 'AI_TOPIC_SIM_THRESHOLD', 0.45))
    prev_vec = list(previous.embedding) if previous.embedding is not None else []
    sim = cosine_similarity(prev_vec, current_vector)
    return sim < threshold


def assign_topic_segment(message: Message, vector: list[float]) -> ChatTopicSegment:
    previous_emb = (
        MessageEmbedding.objects
        .filter(chat_id=message.chat_id)
        .exclude(message_id=message.id)
        .select_related('message')
        .order_by('-message__sent_at')
        .first()
    )
    previous_message = previous_emb.message if previous_emb else None
    active = get_active_segment(message.chat_id)

    if should_start_new_topic(
        previous=previous_emb,
        previous_message=previous_message,
        current_message=message,
        current_vector=vector,
    ):
        if active:
            close_segment(active, at=getattr(message, 'sent_at', None))
        return open_segment(message, vector)

    if not active:
        return open_segment(message, vector)

    count = int(active.message_count or 0) + 1
    old_centroid = list(active.centroid) if active.centroid is not None else None
    active.centroid = ema_centroid(old_centroid, vector, count)
    active.message_count = count
    active.save(update_fields=['centroid', 'message_count', 'updated_at'])
    return active


def embed_and_segment_message(message_id) -> dict:
    if not getattr(settings, 'AI_EMBEDDING_ENABLED', True):
        return {'ok': False, 'reason': 'disabled'}

    try:
        message = Message.objects.select_related('sender', 'chat').get(id=message_id)
    except (Message.DoesNotExist, ValueError, TypeError):
        return {'ok': False, 'reason': 'message_missing'}

    if not message_embeddable(message):
        return {'ok': False, 'reason': 'not_embeddable'}

    try:
        vectors = embed_texts([message.content or ''])
    except Exception:
        logger.exception('embed failed message=%s', message_id)
        return {'ok': False, 'reason': 'embed_error'}

    vector = vectors[0] if vectors else []
    expected = int(getattr(settings, 'AI_EMBEDDING_DIMS', 1024))
    if not vector or len(vector) != expected:
        logger.warning(
            'unexpected embedding dims message=%s got=%s expected=%s',
            message_id,
            len(vector) if vector else 0,
            expected,
        )
        if not vector:
            return {'ok': False, 'reason': 'empty_vector'}

    upsert_message_embedding(message, vector)
    segment = assign_topic_segment(message, vector)
    return {
        'ok': True,
        'message_id': str(message.id),
        'segment_id': segment.id,
        'dims': len(vector),
    }


def load_segment_messages(chat_id, user, *, limit: int | None = None) -> list[Message]:
    if not user_can_access_chat(chat_id, user):
        return []
    active = get_active_segment(chat_id)
    qs = (
        Message.objects
        .filter(chat_id=chat_id, message_type=MessageType.TEXT, deleted_at__isnull=True)
        .select_related('sender')
        .order_by('sent_at')
    )
    if active:
        qs = qs.filter(sent_at__gte=active.started_at)
        if active.ended_at:
            qs = qs.filter(sent_at__lte=active.ended_at)
    else:
        fallback = int(getattr(settings, 'AI_TOPIC_FALLBACK_MESSAGES', 12))
        ids = list(
            qs.order_by('-sent_at').values_list('id', flat=True)[:fallback]
        )
        qs = Message.objects.filter(id__in=ids).select_related('sender').order_by('sent_at')

    messages = list(qs)
    if limit is not None:
        messages = messages[-limit:]
    return messages


def messages_to_labeled_transcript(messages: list[Message], user) -> str:
    lines = []
    for message in messages:
        text = (message.content or '').strip()
        if not text or _is_sticker_like(text):
            continue
        label = 'Я' if str(message.sender_id) == str(user.id) else 'Собеседник'
        lines.append(f'{label}: {text}')
    return '\n'.join(lines)


def retrieve_related_messages(
    chat_id,
    user,
    *,
    query_text: str,
    exclude_ids: set | None = None,
    top_k: int | None = None,
    max_distance: float | None = None,
) -> list[tuple[Message, float]]:
    """
    Semantic nearest neighbors for AI complete — strictly within this chat only.
    Other dialogs of the same user are never searched.

    Returns (message, cosine_distance) pairs ordered by ascending distance
    (best semantic match first). Weak hits above max_distance are dropped.
    """
    if not getattr(settings, 'AI_EMBEDDING_ENABLED', True):
        return []
    if not user_can_access_chat(chat_id, user):
        return []
    query_text = (query_text or '').strip()
    if not query_text:
        return []

    k = int(top_k if top_k is not None else getattr(settings, 'AI_RETRIEVAL_TOP_K', 5))
    limit_distance = float(
        max_distance
        if max_distance is not None
        else getattr(settings, 'AI_RETRIEVAL_MAX_DISTANCE', 0.55)
    )
    exclude_ids = exclude_ids or set()
    chat_id_str = str(chat_id)

    try:
        vectors = embed_texts([query_text])
    except Exception:
        logger.exception('retrieval embed failed chat=%s', chat_id)
        return []
    query_vec = vectors[0] if vectors else []
    if not query_vec:
        return []

    # Hard scope: embedding row AND underlying message must belong to this chat.
    qs = (
        MessageEmbedding.objects
        .filter(chat_id=chat_id, message__chat_id=chat_id)
        .select_related('message', 'message__sender')
        .annotate(distance=CosineDistance('embedding', query_vec))
        .order_by('distance')
    )
    if exclude_ids:
        qs = qs.exclude(message_id__in=list(exclude_ids))

    related: list[tuple[Message, float]] = []
    for row in qs[: max(k * 3, k)]:
        distance = float(getattr(row, 'distance', 1.0) or 1.0)
        if distance > limit_distance:
            # Ordered by distance — further rows are worse.
            break
        message = row.message
        if not message or message.deleted_at:
            continue
        if str(message.chat_id) != chat_id_str:
            continue
        if message.message_type != MessageType.TEXT:
            continue
        if str(message.id) in {str(x) for x in exclude_ids}:
            continue
        related.append((message, distance))
        if len(related) >= k:
            break

    return related
