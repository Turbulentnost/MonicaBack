import json

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.utils import timezone

from apps.notifications.models import Notification, NotificationType


def user_channel_group(user_id):
    return f'user_{user_id}'


def push_notification_to_user(notification):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    from apps.notifications.serializers import NotificationSerializer
    payload = json.loads(
        json.dumps(NotificationSerializer(notification).data, default=str)
    )
    async_to_sync(channel_layer.group_send)(
        user_channel_group(notification.user_id),
        {'type': 'notify.message', 'notification': payload},
    )


def create_notification(user, notification_type, title, body='', payload=None):
    notification = Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        body=body,
        payload=payload or {},
    )
    push_notification_to_user(notification)
    try:
        from apps.notifications.tasks import send_notification_push
        send_notification_push.delay(str(notification.id))
    except Exception:
        pass
    return notification


def notify_private_invite(session, recipient, initiator):
    return create_notification(
        user=recipient,
        notification_type=NotificationType.PRIVATE_INVITE,
        title='Приглашение в приватный чат',
        body=f'@{initiator.nickname} предлагает открыть приватный чат',
        payload={
            'session_id': str(session.id),
            'chat_id': str(session.chat_id),
            'initiator_id': str(initiator.id),
            'initiator_nickname': initiator.nickname,
        },
    )


def notify_private_accepted(session, initiator, acceptor):
    return create_notification(
        user=initiator,
        notification_type=NotificationType.PRIVATE_ACCEPTED,
        title='Приватный чат принят',
        body=f'@{acceptor.nickname} принял приглашение',
        payload={
            'session_id': str(session.id),
            'chat_id': str(session.chat_id),
        },
    )


def notify_private_declined(session, initiator, decliner):
    return create_notification(
        user=initiator,
        notification_type=NotificationType.PRIVATE_DECLINED,
        title='Приватный чат отклонён',
        body=f'@{decliner.nickname} отклонил приглашение',
        payload={
            'session_id': str(session.id),
            'chat_id': str(session.chat_id),
        },
    )


def notify_private_closed(session, recipient, closer):
    return create_notification(
        user=recipient,
        notification_type=NotificationType.PRIVATE_CLOSED,
        title='Приватный чат закрыт',
        body=f'@{closer.nickname} закрыл приватный чат',
        payload={
            'session_id': str(session.id),
            'chat_id': str(session.chat_id),
        },
    )


def notify_private_cancelled(session, recipient, canceller):
    return create_notification(
        user=recipient,
        notification_type=NotificationType.PRIVATE_CANCELLED,
        title='Приглашение отменено',
        body=f'@{canceller.nickname} отменил приглашение в приватный чат',
        payload={
            'session_id': str(session.id),
            'chat_id': str(session.chat_id),
        },
    )


def broadcast_private_session_event(session_id, action, extra=None):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    data = {'action': action, 'session_id': str(session_id)}
    if extra:
        data.update(extra)
    async_to_sync(channel_layer.group_send)(
        f'private_{session_id}',
        {'type': 'private.event', 'data': data},
    )


def resolve_invite_notifications(session_id, resolved, user=None):
    """Помечаем invite-уведомления по session_id как обработанные."""
    sid = str(session_id)
    qs = Notification.objects.filter(notification_type=NotificationType.PRIVATE_INVITE)
    if user is not None:
        qs = qs.filter(user=user)
    for notif in qs.iterator():
        payload = notif.payload or {}
        if str(payload.get('session_id')) != sid:
            continue
        if payload.get('resolved'):
            continue
        notif.payload = {**payload, 'resolved': resolved}
        notif.is_read = True
        notif.save(update_fields=['payload', 'is_read'])


def cancel_user_pending_invites(user):
    """Только pending-заявки (для presence disconnect). Активный приват не трогаем."""
    from apps.chats.models import PrivateSession, PrivateSessionStatus

    now = timezone.now()
    cancelled = 0

    for session in PrivateSession.objects.select_related('initiator', 'recipient').filter(
        initiator=user,
        status=PrivateSessionStatus.PENDING,
    ):
        session.status = PrivateSessionStatus.CLOSED
        session.closed_at = now
        session.save(update_fields=['status', 'closed_at'])
        resolve_invite_notifications(session.id, 'cancelled', user=session.recipient)
        notify_private_cancelled(session, session.recipient, user)
        cancelled += 1

    for session in PrivateSession.objects.select_related('initiator', 'recipient').filter(
        recipient=user,
        status=PrivateSessionStatus.PENDING,
    ):
        session.status = PrivateSessionStatus.DECLINED
        session.closed_at = now
        session.save(update_fields=['status', 'closed_at'])
        resolve_invite_notifications(session.id, 'declined', user=user)
        notify_private_declined(session, session.initiator, user)
        cancelled += 1

    return cancelled


def cancel_user_private_sessions(user):
    """При выходе из приложения: обрываем pending-заявки и активные приватные сессии."""
    from apps.chats.models import PrivateSession, PrivateSessionStatus

    cancelled = cancel_user_pending_invites(user)
    now = timezone.now()

    for session in PrivateSession.objects.select_related('initiator', 'recipient').filter(
        status=PrivateSessionStatus.ACTIVE,
    ).filter(Q(initiator=user) | Q(recipient=user)):
        peer = session.recipient if session.initiator_id == user.id else session.initiator
        session.status = PrivateSessionStatus.CLOSED
        session.closed_at = now
        session.save(update_fields=['status', 'closed_at'])
        notify_private_closed(session, peer, user)
        broadcast_private_session_event(session.id, 'private.closed', {
            'closed_by': str(user.id),
        })
        cancelled += 1

    return cancelled
