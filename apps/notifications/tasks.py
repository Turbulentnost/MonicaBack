import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_message_push(self, message_id: str, recipient_id: str):
    """
    Фоновая отправка FCM-пуша о новом сообщении.
    Пока логируем; полноценный FCM подключим после DeviceToken с мобилки.
    """
    from apps.chats.models import Message
    from apps.notifications.models import DeviceToken
    from apps.notifications.fcm import send_fcm_to_tokens

    try:
        message = Message.objects.select_related('sender', 'chat').get(id=message_id)
    except Message.DoesNotExist:
        logger.warning('Message %s not found for push', message_id)
        return {'ok': False, 'reason': 'message_not_found'}

    tokens = list(
        DeviceToken.objects.filter(
            user_id=recipient_id,
            is_active=True,
        ).values_list('token', flat=True)
    )
    if not tokens:
        logger.info('No device tokens for user %s — push skipped', recipient_id)
        return {'ok': True, 'sent': 0, 'reason': 'no_tokens'}

    title = message.sender.nickname
    body = message.content[:120] if message.message_type == 'text' else f'[{message.message_type}]'

    try:
        result = send_fcm_to_tokens(
            tokens=tokens,
            title=title,
            body=body,
            data={
                'type': 'chat_message',
                'chat_id': str(message.chat_id),
                'message_id': str(message.id),
            },
        )
    except Exception as exc:
        logger.exception('FCM send failed')
        raise self.retry(exc=exc)

    return {'ok': True, **result}
