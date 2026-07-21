import json
import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.chats.models import CallSession, CallStatus, Chat
from apps.chats.presence import is_user_online
from apps.notifications.services import user_channel_group

logger = logging.getLogger(__name__)

NONTERMINAL_STATUSES = (CallStatus.RINGING, CallStatus.ACTIVE)
TERMINAL_STATUSES = (
    CallStatus.REJECTED,
    CallStatus.CANCELLED,
    CallStatus.MISSED,
    CallStatus.ENDED,
    CallStatus.FAILED,
)
BUSY_LOCK_TTL_SEC = 24 * 60 * 60


class CallError(Exception):
    def __init__(self, code, detail, http_status=400):
        self.code = code
        self.detail = detail
        self.http_status = http_status
        super().__init__(detail)


def call_group(call_id):
    return f'call_{call_id}'


def busy_key(user_id):
    return f'call-busy:{user_id}'


def serialize_call(call, request=None):
    from apps.chats.serializers_call import CallSessionSerializer

    data = CallSessionSerializer(call, context={'request': request}).data
    return json.loads(json.dumps(data, default=str))


def _send_group(group, event):
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(group, event)


def broadcast_user_event(call, action, user_ids=None):
    data = {'action': action, 'call': serialize_call(call)}
    recipients = user_ids or (call.caller_id, call.callee_id)
    for user_id in recipients:
        _send_group(
            user_channel_group(user_id),
            {'type': 'call.event', 'data': data},
        )


def broadcast_call_signal(call, action, from_user_id=None, data=None):
    payload = {
        'action': action,
        'call_id': str(call.id),
    }
    if from_user_id is not None:
        payload['from_user_id'] = str(from_user_id)
    if data is not None:
        payload['data'] = data
    if action == 'call.connected':
        payload['call'] = serialize_call(call)
    _send_group(call_group(call.id), {'type': 'call.signal', 'data': payload})


def broadcast_call_ended(call, action):
    _send_group(
        call_group(call.id),
        {
            'type': 'call.ended',
            'data': {'action': action, 'call': serialize_call(call)},
        },
    )


def _release_lock(user_id, call_id):
    key = busy_key(user_id)
    if str(cache.get(key) or '') == str(call_id):
        cache.delete(key)


def release_busy_locks(call):
    _release_lock(call.caller_id, call.id)
    _release_lock(call.callee_id, call.id)


def _reserve_users(caller_id, callee_id, call_id):
    value = str(call_id)
    if not cache.add(busy_key(caller_id), value, timeout=BUSY_LOCK_TTL_SEC):
        return False
    if cache.add(busy_key(callee_id), value, timeout=BUSY_LOCK_TTL_SEC):
        return True
    _release_lock(caller_id, call_id)
    return False


def _schedule_expiry(call_id):
    try:
        from apps.notifications.tasks import expire_call

        expire_call.apply_async(
            args=[str(call_id)],
            countdown=settings.CALL_RING_TIMEOUT_SEC,
        )
    except Exception:
        logger.exception('Could not schedule expiry for call %s', call_id)


def start_call(chat_id, caller, client_instance_id):
    existing = (
        CallSession.objects.select_related('chat', 'caller', 'callee')
        .filter(
            caller=caller,
            client_instance_id=client_instance_id,
            status__in=NONTERMINAL_STATUSES,
        )
        .first()
    )
    if existing:
        return existing, False

    try:
        chat = Chat.objects.prefetch_related('participants__user').get(id=chat_id)
    except (Chat.DoesNotExist, ValueError, TypeError):
        raise CallError('not_found', 'Чат не найден', 404)

    participants = list(chat.participants.all())
    if not any(participant.user_id == caller.id for participant in participants):
        raise CallError('forbidden', 'Нет доступа к чату', 403)
    if len(participants) != 2:
        raise CallError('not_one_to_one', 'Звонки доступны только в чате один на один', 409)

    callee = next(participant.user for participant in participants if participant.user_id != caller.id)
    if not caller.is_active or not callee.is_active:
        raise CallError('unavailable', 'Участник звонка недоступен', 409)
    if not is_user_online(callee.id):
        raise CallError('offline', 'Пользователь не в сети', 409)

    call_id = uuid.uuid4()
    if not _reserve_users(caller.id, callee.id, call_id):
        existing = (
            CallSession.objects.select_related('chat', 'caller', 'callee')
            .filter(
                caller=caller,
                client_instance_id=client_instance_id,
                status__in=NONTERMINAL_STATUSES,
            )
            .first()
        )
        if existing:
            return existing, False
        raise CallError('busy', 'Один из участников уже занят звонком', 409)

    try:
        with transaction.atomic():
            call = CallSession.objects.create(
                id=call_id,
                chat=chat,
                caller=caller,
                callee=callee,
                client_instance_id=client_instance_id,
            )
    except IntegrityError:
        release_busy_locks(
            CallSession(
                id=call_id,
                caller=caller,
                callee=callee,
                chat=chat,
                client_instance_id=client_instance_id,
            )
        )
        existing = (
            CallSession.objects.select_related('chat', 'caller', 'callee')
            .filter(
                caller=caller,
                client_instance_id=client_instance_id,
                status__in=NONTERMINAL_STATUSES,
            )
            .first()
        )
        if existing:
            return existing, False
        raise
    except Exception:
        _release_lock(caller.id, call_id)
        _release_lock(callee.id, call_id)
        raise

    _schedule_expiry(call.id)
    broadcast_user_event(call, 'call.ringing', [call.caller_id])
    broadcast_user_event(call, 'call.incoming', [call.callee_id])
    return call, True


def get_call_for_user(call_id, user, lock=False):
    queryset = CallSession.objects.select_related('chat', 'caller', 'callee')
    if lock:
        queryset = queryset.select_for_update()
    try:
        call = queryset.get(id=call_id)
    except (CallSession.DoesNotExist, ValueError, TypeError):
        raise CallError('not_found', 'Звонок не найден', 404)
    if user.id not in (call.caller_id, call.callee_id):
        raise CallError('forbidden', 'Нет доступа к звонку', 403)
    return call


def get_active_call(user):
    return (
        CallSession.objects.select_related('chat', 'caller', 'callee', 'ended_by')
        .filter(Q(caller=user) | Q(callee=user), status__in=NONTERMINAL_STATUSES)
        .order_by('-created_at')
        .first()
    )


def _invalid_state(call, expected):
    raise CallError(
        'invalid_state',
        f'Действие недоступно для статуса {call.status}; ожидается {expected}',
        409,
    )


def accept_call(call_id, user, client_instance_id=None):
    with transaction.atomic():
        call = get_call_for_user(call_id, user, lock=True)
        if user.id != call.callee_id:
            raise CallError('forbidden', 'Принять звонок может только вызываемый пользователь', 403)
        if call.status == CallStatus.ACTIVE:
            return call, False
        if call.status != CallStatus.RINGING:
            _invalid_state(call, CallStatus.RINGING)
        now = timezone.now()
        call.status = CallStatus.ACTIVE
        call.accepted_at = now
        call.accepted_client_instance_id = client_instance_id
        call.save(update_fields=['status', 'accepted_at', 'accepted_client_instance_id'])
    broadcast_user_event(call, 'call.accepted')
    return call, True


def _finish_call(call_id, user, target_status, action, allowed_role, reason):
    with transaction.atomic():
        call = get_call_for_user(call_id, user, lock=True)
        if allowed_role == 'caller' and user.id != call.caller_id:
            raise CallError('forbidden', 'Действие доступно только инициатору звонка', 403)
        if allowed_role == 'callee' and user.id != call.callee_id:
            raise CallError('forbidden', 'Действие доступно только вызываемому пользователю', 403)
        if call.status == target_status:
            return call, False
        expected = CallStatus.ACTIVE if target_status == CallStatus.ENDED else CallStatus.RINGING
        if call.status != expected:
            _invalid_state(call, expected)
        call.status = target_status
        call.ended_at = timezone.now()
        call.ended_by = user
        call.end_reason = reason
        call.save(update_fields=['status', 'ended_at', 'ended_by', 'end_reason'])
    release_busy_locks(call)
    broadcast_user_event(call, action)
    broadcast_call_ended(call, action)
    return call, True


def reject_call(call_id, user, reason='rejected'):
    return _finish_call(
        call_id, user, CallStatus.REJECTED, 'call.rejected', 'callee', reason
    )


def cancel_call(call_id, user, reason='cancelled'):
    return _finish_call(
        call_id, user, CallStatus.CANCELLED, 'call.cancelled', 'caller', reason
    )


def hangup_call(call_id, user, reason='hangup'):
    return _finish_call(
        call_id, user, CallStatus.ENDED, 'call.ended', None, reason
    )


def mark_call_connected(call_id, user):
    with transaction.atomic():
        call = get_call_for_user(call_id, user, lock=True)
        if call.status != CallStatus.ACTIVE:
            _invalid_state(call, CallStatus.ACTIVE)
        changed = call.connected_at is None
        if changed:
            call.connected_at = timezone.now()
            call.save(update_fields=['connected_at'])
    if changed:
        broadcast_call_signal(call, 'call.connected', user.id)
    return call, changed


def expire_ringing_call(call_id):
    with transaction.atomic():
        try:
            call = (
                CallSession.objects.select_for_update()
                .select_related('chat', 'caller', 'callee')
                .get(id=call_id)
            )
        except (CallSession.DoesNotExist, ValueError, TypeError):
            return None, False
        if call.status != CallStatus.RINGING:
            return call, False
        call.status = CallStatus.MISSED
        call.ended_at = timezone.now()
        call.end_reason = 'ring_timeout'
        call.save(update_fields=['status', 'ended_at', 'end_reason'])
    release_busy_locks(call)
    broadcast_user_event(call, 'call.missed')
    broadcast_call_ended(call, 'call.missed')
    return call, True
