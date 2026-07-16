from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.models import Chat, PrivateSession, PrivateSessionStatus
from apps.chats.services import get_chat_partner, get_user_chats
from apps.notifications.models import DeviceToken, Notification
from apps.notifications.serializers import (
    DeviceTokenResponseSerializer,
    DeviceTokenSerializer,
    NotificationSerializer,
)
from apps.notifications.services import (
    broadcast_private_session_event,
    notify_private_accepted,
    notify_private_cancelled,
    notify_private_closed,
    notify_private_declined,
    notify_private_invite,
    resolve_invite_notifications,
)


class RegisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['token']
        platform = serializer.validated_data['platform']

        DeviceToken.objects.filter(token=token).exclude(user=request.user).delete()

        device, _ = DeviceToken.objects.update_or_create(
            token=token,
            defaults={
                'user': request.user,
                'platform': platform,
                'is_active': True,
            },
        )
        return Response(
            DeviceTokenResponseSerializer(device).data,
            status=status.HTTP_200_OK,
        )


class UnregisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = DeviceToken.objects.filter(
            user=request.user,
            token=serializer.validated_data['token'],
        ).update(is_active=False)
        return Response({'deactivated': updated})


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(user=request.user)[:50]
        return Response(NotificationSerializer(qs, many=True).data)


class NotificationReadAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        updated = Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'updated': updated})


class NotificationClearView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        deleted, _ = Notification.objects.filter(user=request.user).delete()
        return Response({'deleted': deleted})


class NotificationDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, notification_id):
        deleted, _ = Notification.objects.filter(user=request.user, id=notification_id).delete()
        return Response({'deleted': deleted})


class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        updated = Notification.objects.filter(
            user=request.user, id=notification_id, is_read=False
        ).update(is_read=True)
        return Response({'updated': updated})


class PrivateSessionLeaveView(APIView):
    """Вызов при logout / закрытии приложения — обрыв заявок."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.notifications.services import cancel_user_private_sessions
        cancelled = cancel_user_private_sessions(request.user)
        return Response({'cancelled': cancelled})


class PrivateSessionInviteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, chat_id):
        try:
            chat = get_user_chats(request.user).get(id=chat_id)
        except Chat.DoesNotExist:
            return Response({'detail': 'Чат не найден'}, status=404)

        partner = get_chat_partner(chat, request.user)
        if not partner:
            return Response({'detail': 'Собеседник не найден'}, status=400)

        with transaction.atomic():
            existing = (
                PrivateSession.objects.select_for_update()
                .filter(
                    chat=chat,
                    status__in=[PrivateSessionStatus.PENDING, PrivateSessionStatus.ACTIVE],
                )
                .filter(
                    Q(initiator=request.user, recipient=partner)
                    | Q(initiator=partner, recipient=request.user)
                )
                .first()
            )

            if existing and existing.status == PrivateSessionStatus.ACTIVE:
                return Response({
                    'id': existing.id,
                    'chat_id': chat.id,
                    'status': existing.status,
                    'handshake': False,
                })

            # Встречное приглашение = рукопожатие: принимаем чужой pending
            if (
                existing
                and existing.status == PrivateSessionStatus.PENDING
                and existing.initiator_id == partner.id
                and existing.recipient_id == request.user.id
            ):
                existing.status = PrivateSessionStatus.ACTIVE
                existing.accepted_at = timezone.now()
                existing.save(update_fields=['status', 'accepted_at'])
                session = existing
                handshake = True
            elif existing and existing.status == PrivateSessionStatus.PENDING:
                return Response({
                    'id': existing.id,
                    'chat_id': chat.id,
                    'status': existing.status,
                    'handshake': False,
                })
            else:
                session = PrivateSession.objects.create(
                    chat=chat,
                    initiator=request.user,
                    recipient=partner,
                    status=PrivateSessionStatus.PENDING,
                )
                handshake = False

        if handshake:
            resolve_invite_notifications(session.id, 'accepted', user=request.user)
            notify_private_accepted(session, session.initiator, request.user)
            broadcast_private_session_event(session.id, 'private.opened')
            return Response({
                'id': session.id,
                'chat_id': chat.id,
                'status': session.status,
                'handshake': True,
            })

        notify_private_invite(session, partner, request.user)
        return Response(
            {
                'id': session.id,
                'chat_id': chat.id,
                'status': session.status,
                'handshake': False,
            },
            status=status.HTTP_201_CREATED,
        )


class PrivateSessionAcceptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        try:
            session = PrivateSession.objects.select_related('initiator', 'recipient', 'chat').get(
                id=session_id
            )
        except PrivateSession.DoesNotExist:
            return Response({'detail': 'Сессия не найдена'}, status=404)

        if session.recipient_id != request.user.id:
            return Response({'detail': 'Только получатель может принять'}, status=403)
        if session.status != PrivateSessionStatus.PENDING:
            return Response({'detail': 'Приглашение уже обработано'}, status=400)

        session.status = PrivateSessionStatus.ACTIVE
        session.accepted_at = timezone.now()
        session.save(update_fields=['status', 'accepted_at'])
        resolve_invite_notifications(session.id, 'accepted', user=request.user)
        notify_private_accepted(session, session.initiator, request.user)
        broadcast_private_session_event(session.id, 'private.opened')
        return Response({
            'id': session.id,
            'chat_id': session.chat_id,
            'status': session.status,
        })


class PrivateSessionDeclineView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        try:
            session = PrivateSession.objects.select_related('initiator', 'recipient').get(id=session_id)
        except PrivateSession.DoesNotExist:
            return Response({'detail': 'Сессия не найдена'}, status=404)

        if session.recipient_id != request.user.id:
            return Response({'detail': 'Только получатель может отклонить'}, status=403)

        # Идемпотентно: повторное отклонение / уже закрытая заявка — успех
        if session.status != PrivateSessionStatus.PENDING:
            resolve_invite_notifications(session.id, 'declined', user=request.user)
            return Response({'id': session.id, 'status': session.status})

        session.status = PrivateSessionStatus.DECLINED
        session.closed_at = timezone.now()
        session.save(update_fields=['status', 'closed_at'])
        resolve_invite_notifications(session.id, 'declined', user=request.user)
        notify_private_declined(session, session.initiator, request.user)
        return Response({'id': session.id, 'status': session.status})


class PrivateSessionCloseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, session_id):
        try:
            session = PrivateSession.objects.select_related('initiator', 'recipient').get(id=session_id)
        except PrivateSession.DoesNotExist:
            return Response({'detail': 'Сессия не найдена'}, status=404)

        if request.user.id not in (session.initiator_id, session.recipient_id):
            return Response({'detail': 'Нет доступа'}, status=403)
        if session.status not in (PrivateSessionStatus.ACTIVE, PrivateSessionStatus.PENDING):
            return Response({'id': session.id, 'status': session.status})

        peer = session.recipient if request.user.id == session.initiator_id else session.initiator
        was_active = session.status == PrivateSessionStatus.ACTIVE
        was_pending = session.status == PrivateSessionStatus.PENDING
        is_initiator = request.user.id == session.initiator_id

        if was_pending and not is_initiator:
            session.status = PrivateSessionStatus.DECLINED
        else:
            session.status = PrivateSessionStatus.CLOSED
        session.closed_at = timezone.now()
        session.save(update_fields=['status', 'closed_at'])

        if was_pending:
            if is_initiator:
                resolve_invite_notifications(session.id, 'cancelled', user=peer)
                notify_private_cancelled(session, peer, request.user)
            else:
                resolve_invite_notifications(session.id, 'declined', user=request.user)
                notify_private_declined(session, peer, request.user)
        elif was_active:
            notify_private_closed(session, peer, request.user)
            broadcast_private_session_event(session.id, 'private.closed', {
                'closed_by': str(request.user.id),
            })
        return Response({'id': session.id, 'status': session.status})
