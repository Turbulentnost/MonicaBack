import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task
def update_user_style(user_id: str, message_id: str):
    """
    After each own text message:
    - append global style sample
    - continuously adapt partner-specific style for this chat
    """
    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'ok': False, 'reason': 'disabled'}

    from apps.ai.models import UserStyleProfile
    from apps.ai.services import (
        append_style_sample,
        maybe_refresh_partner_style,
        maybe_refresh_traits,
    )
    from apps.chats.models import Message, MessageType
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        message = Message.objects.select_related('sender', 'chat').get(id=message_id)
    except (Message.DoesNotExist, ValueError, TypeError):
        return {'ok': False, 'reason': 'message_missing'}

    if str(message.sender_id) != str(user_id):
        return {'ok': False, 'reason': 'sender_mismatch'}
    if message.message_type != MessageType.TEXT:
        return {'ok': False, 'reason': 'not_text'}
    if message.deleted_at:
        return {'ok': False, 'reason': 'deleted'}

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return {'ok': False, 'reason': 'user_missing'}

    profile = append_style_sample(user, message.content or '')
    partner_result = {'ok': False}
    try:
        partner_result = maybe_refresh_partner_style(user, message.chat, force=False)
    except Exception:
        logger.exception('partner style refresh error user=%s msg=%s', user_id, message_id)

    if profile is not None:
        try:
            maybe_refresh_traits(profile)
        except Exception:
            logger.exception('traits refresh error user=%s', user_id)

    fresh = UserStyleProfile.objects.filter(user_id=user_id).first()
    return {
        'ok': True,
        'sampled': profile is not None,
        'samples_count': len(fresh.samples) if fresh and isinstance(fresh.samples, list) else 0,
        'partner_style': partner_result,
    }


@shared_task
def analyze_day_partner_styles(user_id: str):
    """After user goes offline — force-refresh per-partner styles for today."""
    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'ok': False, 'reason': 'disabled'}
    from apps.ai.services import analyze_user_day_partner_styles

    try:
        return analyze_user_day_partner_styles(user_id)
    except Exception:
        logger.exception('analyze_day_partner_styles failed user=%s', user_id)
        return {'ok': False, 'reason': 'exception'}
