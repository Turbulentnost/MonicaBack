from django.db.models import Q
from django.utils import timezone

from apps.users.models import UserBlock


class BlockError(Exception):
    def __init__(self, detail, status_code=400):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def is_blocked_either_way(user_a, user_b) -> bool:
    if not user_a or not user_b:
        return False
    if user_a.id == user_b.id:
        return False
    return UserBlock.objects.filter(
        Q(blocker_id=user_a.id, blocked_id=user_b.id)
        | Q(blocker_id=user_b.id, blocked_id=user_a.id)
    ).exists()


def get_block_flags(user, partner) -> tuple[bool, bool]:
    """Return (i_blocked_partner, partner_blocked_me)."""
    if not user or not partner or user.id == partner.id:
        return False, False
    rows = list(
        UserBlock.objects.filter(
            Q(blocker_id=user.id, blocked_id=partner.id)
            | Q(blocker_id=partner.id, blocked_id=user.id)
        ).values_list('blocker_id', flat=True)
    )
    i_blocked = any(blocker_id == user.id for blocker_id in rows)
    blocked_by = any(blocker_id == partner.id for blocker_id in rows)
    return i_blocked, blocked_by


def _close_private_sessions(user_a, user_b):
    from apps.chats.models import PrivateSession, PrivateSessionStatus
    from apps.notifications.services import (
        broadcast_private_session_event,
        notify_private_cancelled,
        notify_private_closed,
        resolve_invite_notifications,
    )

    sessions = list(
        PrivateSession.objects.select_related('initiator', 'recipient').filter(
            status__in=[PrivateSessionStatus.PENDING, PrivateSessionStatus.ACTIVE],
        ).filter(
            Q(initiator=user_a, recipient=user_b)
            | Q(initiator=user_b, recipient=user_a)
        )
    )
    now = timezone.now()
    for session in sessions:
        peer = session.recipient if session.initiator_id == user_a.id else session.initiator
        was_pending = session.status == PrivateSessionStatus.PENDING
        session.status = PrivateSessionStatus.CLOSED
        session.closed_at = now
        session.save(update_fields=['status', 'closed_at'])
        if was_pending:
            resolve_invite_notifications(session.id, 'cancelled')
            if peer.id != user_a.id:
                notify_private_cancelled(session, peer, user_a)
        else:
            if peer.id != user_a.id:
                notify_private_closed(session, peer, user_a)
        broadcast_private_session_event(session.id, 'private.closed', {
            'closed_by': str(user_a.id),
        })


def block_user(blocker, blocked):
    if not blocked:
        raise BlockError('Пользователь не найден', status_code=404)
    if blocker.id == blocked.id:
        raise BlockError('Нельзя заблокировать себя')

    obj, created = UserBlock.objects.get_or_create(blocker=blocker, blocked=blocked)
    if created:
        _close_private_sessions(blocker, blocked)
    return obj, created


def unblock_user(blocker, blocked):
    if not blocked:
        raise BlockError('Пользователь не найден', status_code=404)
    deleted, _ = UserBlock.objects.filter(blocker=blocker, blocked=blocked).delete()
    return deleted > 0


def assert_users_can_interact(user_a, user_b):
    if is_blocked_either_way(user_a, user_b):
        raise BlockError('Общение с этим пользователем недоступно', status_code=403)
