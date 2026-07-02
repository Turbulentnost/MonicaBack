import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.chats.models import Chat, Message
from apps.chats.services import user_in_chat


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
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        if action == 'message.send':
            await self._handle_send_message(content)

    async def _handle_send_message(self, content):
        message_type = content.get('message_type', 'text')
        text_content = content.get('content', '').strip()
        if not text_content:
            return

        message = await self._create_message(message_type, text_content)
        if not message:
            return

        payload = await self._serialize_message(message)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'chat.message', 'message': payload},
        )

    async def chat_message(self, event):
        await self.send_json({'action': 'message.new', 'message': event['message']})

    @database_sync_to_async
    def _user_in_chat(self):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return False
        return user_in_chat(chat, self.user)

    @database_sync_to_async
    def _create_message(self, message_type, content):
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
        )
        chat.updated_at = timezone.now()
        chat.save(update_fields=['updated_at'])
        return message

    @database_sync_to_async
    def _serialize_message(self, message):
        from apps.chats.serializers import MessageSerializer
        return MessageSerializer(message).data
