import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


@shared_task
def expire_call(call_id: str):
    """Idempotently turn an unanswered ringing call into a missed call."""
    from apps.chats.call_services import expire_ringing_call

    call, changed = expire_ringing_call(call_id)
    return {
        'ok': call is not None,
        'changed': changed,
        'status': call.status if call else None,
    }


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
    body = (
        message.content[:120]
        if message.message_type == 'text'
        else 'Голосовое сообщение'
        if message.message_type == 'voice'
        else f'[{message.message_type}]'
    )

    try:
        result = send_fcm_to_tokens(
            tokens=tokens,
            title=title,
            body=body,
            data={
                'type': 'chat_message',
                'chat_id': str(message.chat_id),
                'message_id': str(message.id),
                'title': title,
                'body': body,
            },
        )
    except Exception as exc:
        logger.exception('FCM send failed')
        raise self.retry(exc=exc)

    return {'ok': True, **result}


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_call_push(self, call_id: str):
    """FCM data-push о входящем звонке — открывает мобильное приложение."""
    from apps.chats.models import CallSession, CallStatus
    from apps.notifications.models import DeviceToken
    from apps.notifications.fcm import send_fcm_to_tokens

    try:
        call = CallSession.objects.select_related('caller', 'callee', 'chat').get(id=call_id)
    except CallSession.DoesNotExist:
        logger.warning('Call %s not found for push', call_id)
        return {'ok': False, 'reason': 'call_not_found'}

    if call.status != CallStatus.RINGING:
        return {'ok': True, 'sent': 0, 'reason': 'not_ringing'}

    tokens = list(
        DeviceToken.objects.filter(
            user_id=call.callee_id,
            is_active=True,
        ).values_list('token', flat=True)
    )
    if not tokens:
        logger.info('No device tokens for callee %s — call push skipped', call.callee_id)
        return {'ok': True, 'sent': 0, 'reason': 'no_tokens'}

    is_video = call.media_mode == 'video'
    title = 'Входящий видеозвонок' if is_video else 'Входящий аудиозвонок'
    body = f'@{call.caller.nickname}'

    try:
        result = send_fcm_to_tokens(
            tokens=tokens,
            title=title,
            body=body,
            data={
                'type': 'incoming_call',
                'call_id': str(call.id),
                'chat_id': str(call.chat_id),
                'media_mode': call.media_mode,
                'caller_id': str(call.caller_id),
                'caller_nickname': call.caller.nickname or '',
                'title': title,
                'body': body,
            },
            channel_id='calls_monica_v2',
            data_only=True,
        )
    except Exception as exc:
        logger.exception('FCM call push failed')
        raise self.retry(exc=exc)

    return {'ok': True, **result}


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_notification_push(self, notification_id: str):
    """FCM-пуш для in-app уведомления (приватный чат и др.)."""
    from apps.notifications.models import DeviceToken, Notification
    from apps.notifications.fcm import send_fcm_to_tokens

    try:
        notification = Notification.objects.select_related('user').get(id=notification_id)
    except Notification.DoesNotExist:
        logger.warning('Notification %s not found for push', notification_id)
        return {'ok': False, 'reason': 'notification_not_found'}

    tokens = list(
        DeviceToken.objects.filter(
            user_id=notification.user_id,
            is_active=True,
        ).values_list('token', flat=True)
    )
    if not tokens:
        logger.info('No device tokens for user %s — push skipped', notification.user_id)
        return {'ok': True, 'sent': 0, 'reason': 'no_tokens'}

    data = {
        'type': notification.notification_type,
        'notification_id': str(notification.id),
    }
    for key, value in (notification.payload or {}).items():
        data[str(key)] = str(value)

    try:
        result = send_fcm_to_tokens(
            tokens=tokens,
            title=notification.title,
            body=(notification.body or '')[:180],
            data=data,
        )
    except Exception as exc:
        logger.exception('FCM notification push failed')
        raise self.retry(exc=exc)

    return {'ok': True, **result}
