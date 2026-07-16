import asyncio

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser

from apps.chats.models import PrivateSession, PrivateSessionStatus
from apps.chats.presence import (
    get_online_user_ids,
    heartbeat,
    is_user_online,
    mark_user_offline,
    mark_user_online,
    prune_stale_online,
    record_last_seen,
)
from apps.notifications.services import user_channel_group

PRESENCE_GROUP = 'presence'


class PresenceConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope['user']
        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        self.user_id = str(self.user.id)
        await self.channel_layer.group_add(PRESENCE_GROUP, self.channel_name)
        await self.channel_layer.group_add(user_channel_group(self.user_id), self.channel_name)
        await self.accept()

        newly_online = await self._mark_online()
        await self.send_json({
            'action': 'presence.snapshot',
            'online_user_ids': await self._get_online_ids(),
        })
        if newly_online:
            await self.channel_layer.group_send(
                PRESENCE_GROUP,
                {
                    'type': 'presence.update',
                    'user_id': self.user_id,
                    'is_online': True,
                    'last_seen_at': None,
                },
            )

    async def disconnect(self, close_code):
        if not hasattr(self, 'user_id'):
            return
        fully_offline = await self._mark_offline()
        await self.channel_layer.group_discard(PRESENCE_GROUP, self.channel_name)
        await self.channel_layer.group_discard(
            user_channel_group(self.user_id), self.channel_name
        )
        if fully_offline:
            # Пауза: при F5 / кратком reconnect не рвём сессии
            await asyncio.sleep(2.5)
            still_offline = not await database_sync_to_async(is_user_online)(self.user_id)
            if still_offline:
                # Только заявки (pending). Активный приват не трогаем.
                await database_sync_to_async(self._cancel_pending_on_leave)()
                last_seen = await database_sync_to_async(record_last_seen)(self.user_id)
                await self.channel_layer.group_send(
                    PRESENCE_GROUP,
                    {
                        'type': 'presence.update',
                        'user_id': self.user_id,
                        'is_online': False,
                        'last_seen_at': last_seen,
                    },
                )

    async def receive_json(self, content, **kwargs):
        if content.get('action') == 'presence.ping':
            status = await database_sync_to_async(heartbeat)(self.user_id)
            gone = await database_sync_to_async(prune_stale_online)()
            for uid, last_seen in gone:
                await self.channel_layer.group_send(
                    PRESENCE_GROUP,
                    {
                        'type': 'presence.update',
                        'user_id': uid,
                        'is_online': False,
                        'last_seen_at': last_seen,
                    },
                )
            if status == 'restored':
                await self.channel_layer.group_send(
                    PRESENCE_GROUP,
                    {
                        'type': 'presence.update',
                        'user_id': self.user_id,
                        'is_online': True,
                        'last_seen_at': None,
                    },
                )
            await self.send_json({'action': 'presence.pong'})

    async def presence_update(self, event):
        await self.send_json({
            'action': 'presence.update',
            'user_id': event['user_id'],
            'is_online': event['is_online'],
            'last_seen_at': event.get('last_seen_at'),
        })

    async def notify_message(self, event):
        await self.send_json({
            'action': 'notification.new',
            'notification': event['notification'],
        })

    def _cancel_pending_on_leave(self):
        from apps.notifications.services import cancel_user_pending_invites
        try:
            cancel_user_pending_invites(self.user)
        except Exception:
            pass

    async def _mark_online(self):
        return await database_sync_to_async(mark_user_online)(self.user_id)

    async def _mark_offline(self):
        return await database_sync_to_async(mark_user_offline)(self.user_id)

    async def _get_online_ids(self):
        return await database_sync_to_async(get_online_user_ids)()


class PrivateSessionConsumer(AsyncJsonWebsocketConsumer):
    """Live dual-pane: каждый шлёт свой текст, пир видит peer_text."""

    async def connect(self):
        self.user = self.scope['user']
        self.session_id = self.scope['url_route']['kwargs']['session_id']

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        # Короткий retry: accept и connect могут гоняться
        allowed = False
        for attempt in range(5):
            allowed = await self._user_allowed()
            if allowed:
                break
            await asyncio.sleep(0.15 * (attempt + 1))

        if not allowed:
            await self.close(code=4003)
            return

        self.room_group_name = f'private_{self.session_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_json({'action': 'private.ready', 'session_id': str(self.session_id)})

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        if action == 'private.sync':
            text = content.get('text', '')
            if not isinstance(text, str):
                return
            if len(text) > 100_000:
                text = text[:100_000]
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'private.peer_text',
                    'user_id': str(self.user.id),
                    'text': text,
                },
            )
        elif action == 'private.close':
            await self._close_session()

    async def private_peer_text(self, event):
        if str(self.user.id) == str(event.get('user_id')):
            return
        await self.send_json({
            'action': 'private.peer_text',
            'user_id': event['user_id'],
            'text': event['text'],
        })

    async def private_event(self, event):
        await self.send_json(event['data'])

    @database_sync_to_async
    def _user_allowed(self):
        try:
            session = PrivateSession.objects.get(id=self.session_id)
        except (PrivateSession.DoesNotExist, ValueError, TypeError):
            return False
        user_id = self.user.id
        if user_id != session.initiator_id and user_id != session.recipient_id:
            return False
        return session.status == PrivateSessionStatus.ACTIVE

    @database_sync_to_async
    def _close_session(self):
        from django.utils import timezone

        from apps.notifications.services import broadcast_private_session_event, notify_private_closed

        try:
            session = PrivateSession.objects.select_related('initiator', 'recipient').get(
                id=self.session_id
            )
        except PrivateSession.DoesNotExist:
            return
        if session.status != PrivateSessionStatus.ACTIVE:
            return
        peer = session.recipient if self.user.id == session.initiator_id else session.initiator
        session.status = PrivateSessionStatus.CLOSED
        session.closed_at = timezone.now()
        session.save(update_fields=['status', 'closed_at'])
        notify_private_closed(session, peer, self.user)
        broadcast_private_session_event(session.id, 'private.closed', {
            'closed_by': str(self.user.id),
        })
