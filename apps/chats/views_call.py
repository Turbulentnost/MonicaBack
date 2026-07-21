import base64
import hashlib
import hmac
import time

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chats.call_services import (
    CallError,
    accept_call,
    cancel_call,
    get_active_call,
    hangup_call,
    reject_call,
    serialize_call,
    start_call,
)
from apps.chats.serializers_call import (
    AcceptCallSerializer,
    EndCallSerializer,
    StartCallSerializer,
)


def call_error_response(exc):
    return Response(
        {'code': exc.code, 'detail': exc.detail},
        status=exc.http_status,
    )


class StartCallView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, chat_id):
        serializer = StartCallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            call, created = start_call(
                chat_id,
                request.user,
                serializer.validated_data['client_instance_id'],
            )
        except CallError as exc:
            return call_error_response(exc)
        return Response(
            serialize_call(call, request),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CallActionView(APIView):
    permission_classes = [IsAuthenticated]
    action = None

    def post(self, request, call_id):
        end_reason = ''
        client_instance_id = None
        if self.action == 'accept':
            serializer = AcceptCallSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            client_instance_id = serializer.validated_data.get('client_instance_id')
        else:
            serializer = EndCallSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            end_reason = serializer.validated_data.get('end_reason', '')

        try:
            if self.action == 'accept':
                call, _ = accept_call(call_id, request.user, client_instance_id)
            elif self.action == 'reject':
                call, _ = reject_call(call_id, request.user, end_reason or 'rejected')
            elif self.action == 'cancel':
                call, _ = cancel_call(call_id, request.user, end_reason or 'cancelled')
            elif self.action == 'hangup':
                call, _ = hangup_call(call_id, request.user, end_reason or 'hangup')
            else:
                return Response(
                    {'code': 'invalid_action', 'detail': 'Неизвестное действие'},
                    status=400,
                )
        except CallError as exc:
            return call_error_response(exc)
        return Response(serialize_call(call, request))


class AcceptCallView(CallActionView):
    action = 'accept'


class RejectCallView(CallActionView):
    action = 'reject'


class CancelCallView(CallActionView):
    action = 'cancel'


class HangupCallView(CallActionView):
    action = 'hangup'


class ActiveCallView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        call = get_active_call(request.user)
        return Response({'call': serialize_call(call, request) if call else None})


class IceConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        host = settings.TURN_HOST.strip()
        ttl = settings.TURN_CREDENTIAL_TTL
        stun_url = f'stun:{host}:3478' if host else 'stun:stun.l.google.com:19302'
        ice_servers = [{'urls': [stun_url]}]

        if host and settings.TURN_SECRET:
            expires_at = int(time.time()) + ttl
            username = f'{expires_at}:{request.user.id}'
            digest = hmac.new(
                settings.TURN_SECRET.encode('utf-8'),
                username.encode('utf-8'),
                hashlib.sha1,
            ).digest()
            credential = base64.b64encode(digest).decode('ascii')
            ice_servers.append({
                'urls': [
                    f'turn:{host}:3478?transport=udp',
                    f'turn:{host}:3478?transport=tcp',
                ],
                'username': username,
                'credential': credential,
            })

        return Response({
            'ice_servers': ice_servers,
            'ttl': ttl,
            'realm': settings.TURN_REALM,
        })
