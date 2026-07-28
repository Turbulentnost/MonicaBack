import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task
def update_user_style(user_id: str, message_id: str):
    """Append outgoing text sample and occasionally refresh global style traits."""
    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'ok': False, 'reason': 'disabled'}

    from apps.ai.models import UserStyleProfile
    from apps.ai.services import append_style_sample, maybe_refresh_traits
    from apps.chats.models import Message, MessageType
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        message = Message.objects.select_related('sender').get(id=message_id)
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
    if profile is None:
        return {'ok': True, 'sampled': False}

    try:
        maybe_refresh_traits(profile)
    except Exception:
        logger.exception('traits refresh error user=%s', user_id)

    fresh = UserStyleProfile.objects.filter(pk=profile.pk).first()
    return {
        'ok': True,
        'sampled': True,
        'samples_count': len(fresh.samples) if fresh and isinstance(fresh.samples, list) else 0,
    }


@shared_task
def analyze_day_partner_styles(user_id: str):
    """After user goes offline — extract/update per-partner communication traits."""
    if not getattr(settings, 'AI_COMPLETION_ENABLED', False):
        return {'ok': False, 'reason': 'disabled'}
    from apps.ai.services import analyze_user_day_partner_styles

    try:
        return analyze_user_day_partner_styles(user_id)
    except Exception:
        logger.exception('analyze_day_partner_styles failed user=%s', user_id)
        return {'ok': False, 'reason': 'exception'}
