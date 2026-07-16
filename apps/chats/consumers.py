from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.chats.models import Chat, Message, MessageType
from apps.chats.services import user_in_chat

VALID_MESSAGE_TYPES = {choice.value for choice in MessageType}


class ChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope['user']
        self.chat_id = self.scope['url_route']['kwargs']['chat_id']

        if isinstance(self.user, AnonymousUser) or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        if not await self._user_in_chat():
            await self.close(code=4003)
            return

        self.room_group_name = f'chat_{self.chat_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self._broadcast_typing(False)
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        if action == 'message.send':
            await self._handle_send_message(content)
        elif action == 'typing.start':
            await self._broadcast_typing(True)
        elif action == 'typing.stop':
            await self._broadcast_typing(False)

    async def _broadcast_typing(self, is_typing):
        if not hasattr(self, 'room_group_name'):
            return
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat.typing',
                'user_id': str(self.user.id),
                'nickname': self.user.nickname,
                'is_typing': is_typing,
            },
        )

    async def _handle_send_message(self, content):
        message_type = content.get('message_type', MessageType.TEXT)
        if message_type not in VALID_MESSAGE_TYPES:
            message_type = MessageType.TEXT

        text_content = (content.get('content') or '').strip()
        if not text_content:
            return

        file_name = (content.get('file_name') or '').strip()
        mime_type = (content.get('mime_type') or '').strip()
        file_size = content.get('file_size')

        message = await self._create_message(
            message_type,
            text_content,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        )
        if not message:
            return

        await self._broadcast_typing(False)

        payload = await self._serialize_message(message)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'chat.message', 'message': payload},
        )

    async def chat_message(self, event):
        await self.send_json({'action': 'message.new', 'message': event['message']})

    async def chat_message_deleted(self, event):
        await self.send_json({
            'action': 'message.deleted',
            'message_id': event['message_id'],
        })

    async def chat_typing(self, event):
        if str(self.user.id) == str(event.get('user_id')):
            return
        await self.send_json({
            'action': 'typing.update',
            'user_id': event['user_id'],
            'nickname': event['nickname'],
            'is_typing': event['is_typing'],
        })

    @database_sync_to_async
    def _user_in_chat(self):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return False
        return user_in_chat(chat, self.user)

    @database_sync_to_async
    def _create_message(self, message_type, content, file_name='', mime_type='', file_size=None):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return None
        if not user_in_chat(chat, self.user):
            return None

        message = Message.objects.create(
            chat=chat,
            sender=self.user,
            message_type=message_type,
            content=content,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        )
        chat.updated_at = timezone.now()
        chat.save(update_fields=['updated_at'])

        recipient_ids = list(
            chat.participants.exclude(user=self.user).values_list('user_id', flat=True)
        )
        from apps.notifications.tasks import send_message_push
        for recipient_id in recipient_ids:
            send_message_push.delay(str(message.id), str(recipient_id))

        return message

    @database_sync_to_async
    def _serialize_message(self, message):
        import json
        from apps.chats.serializers import MessageSerializer
        return json.loads(json.dumps(MessageSerializer(message).data, default=str))
