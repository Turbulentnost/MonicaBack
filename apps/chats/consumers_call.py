from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

from apps.chats.call_services import (
    CallError,
    NONTERMINAL_STATUSES,
    call_group,
    get_call_for_user,
    mark_call_connected,
    serialize_call,
)
from apps.chats.models import CallStatus


class CallSignalingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope['user']
        self.call_id = self.scope['url_route']['kwargs']['call_id']
        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        call = await self._get_allowed_call()
        if call is None:
            await self.close(code=4003)
            return

        self.room_group_name = call_group(self.call_id)
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_json({
            'action': 'call.ready',
            'call': await self._serialize(call),
        })

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        if action == 'call.rejoin':
            call = await self._get_allowed_call()
            if call is None:
                await self.send_json({
                    'action': 'call.error',
                    'code': 'invalid_state',
                    'detail': 'Звонок уже завершён',
                })
                return
            await self.send_json({
                'action': 'call.sync',
                'call': await self._serialize(call),
            })
            if call.status == CallStatus.ACTIVE:
                target_user_id = (
                    call.callee_id if self.user.id == call.caller_id else call.caller_id
                )
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'call.signal',
                        'data': {
                            'action': 'call.rejoin',
                            'call_id': str(self.call_id),
                            'from_user_id': str(self.user.id),
                        },
                        'sender_channel': self.channel_name,
                        'target_user_id': str(target_user_id),
                    },
                )
            return

        if action == 'call.connected':
            try:
                await self._mark_connected()
            except CallError as exc:
                await self._send_error(exc)
            return

        if action not in ('call.offer', 'call.answer', 'call.ice'):
            await self.send_json({
                'action': 'call.error',
                'code': 'invalid_action',
                'detail': 'Неизвестное signaling-действие',
            })
            return

        call = await self._get_allowed_call()
        if call is None:
            await self.send_json({
                'action': 'call.error',
                'code': 'invalid_state',
                'detail': 'Звонок уже завершён',
            })
            return
        # Either participant may renegotiate (e.g. camera on/off).
        if call.status != CallStatus.ACTIVE:
            await self.send_json({
                'action': 'call.error',
                'code': 'invalid_state',
                'detail': 'WebRTC signaling доступен только после принятия звонка',
            })
            return
        if 'data' not in content:
            await self.send_json({
                'action': 'call.error',
                'code': 'invalid_payload',
                'detail': 'Поле data обязательно',
            })
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'call.signal',
                'data': {
                    'action': action,
                    'call_id': str(self.call_id),
                    'from_user_id': str(self.user.id),
                    'data': content['data'],
                },
                'sender_channel': self.channel_name,
                'target_user_id': str(
                    call.callee_id if self.user.id == call.caller_id else call.caller_id
                ),
            },
        )

    async def call_signal(self, event):
        if event.get('sender_channel') == self.channel_name:
            return
        target_user_id = event.get('target_user_id')
        if target_user_id and str(self.user.id) != str(target_user_id):
            return
        await self.send_json(event['data'])

    async def call_ended(self, event):
        await self.send_json(event['data'])

    async def _forbidden(self, detail):
        await self.send_json({
            'action': 'call.error',
            'code': 'forbidden',
            'detail': detail,
        })

    async def _send_error(self, exc):
        await self.send_json({
            'action': 'call.error',
            'code': exc.code,
            'detail': exc.detail,
        })

    @database_sync_to_async
    def _get_allowed_call(self):
        try:
            call = get_call_for_user(self.call_id, self.user)
        except CallError:
            return None
        if call.status not in NONTERMINAL_STATUSES:
            return None
        return call

    @database_sync_to_async
    def _serialize(self, call):
        return serialize_call(call)

    @database_sync_to_async
    def _mark_connected(self):
        return mark_call_connected(self.call_id, self.user)
