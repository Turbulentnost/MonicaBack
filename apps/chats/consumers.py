from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone

from apps.chats.models import Chat, Message, MessageType
from apps.chats.presence import mark_chat_viewing, touch_chat_viewing, unmark_chat_viewing
from apps.chats.services import (
    MESSAGE_EDIT_MAX_DAYS,
    get_photo_caption,
    invalidate_chat_history_cache,
    looks_like_storage_path,
    mark_messages_read,
    user_in_chat,
)

VALID_MESSAGE_TYPES = {choice.value for choice in MessageType}
EDITABLE_MESSAGE_TYPES = {MessageType.TEXT, MessageType.PHOTO}


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
        await database_sync_to_async(mark_chat_viewing)(self.user.id, self.chat_id)
        # Read receipts are client-driven (messages.read) so idle open tabs
        # do not auto-mark incoming messages as read.

    async def disconnect(self, close_code):
        if hasattr(self, 'user') and hasattr(self, 'chat_id'):
            await database_sync_to_async(unmark_chat_viewing)(self.user.id, self.chat_id)
        if hasattr(self, 'room_group_name'):
            await self._broadcast_typing(False)
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if hasattr(self, 'user') and hasattr(self, 'chat_id'):
            await database_sync_to_async(touch_chat_viewing)(self.user.id, self.chat_id)
        action = content.get('action')
        if action == 'message.send':
            await self._handle_send_message(content)
        elif action == 'message.edit':
            await self._handle_edit_message(content)
        elif action == 'messages.read':
            await self._mark_read(content.get('message_ids'))
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

        attachments = self._normalize_attachments(content.get('attachments'))
        text_content = (content.get('content') or '').strip()
        client_id = (content.get('client_id') or '').strip() or None
        file_name = (content.get('file_name') or '').strip()
        mime_type = (content.get('mime_type') or '').strip()
        file_size = content.get('file_size')
        reply_to_id = content.get('reply_to')

        if attachments:
            first = attachments[0]
            paths = {item['path'] for item in attachments}
            file_name = file_name or first.get('file_name') or ''
            mime_type = mime_type or first.get('mime_type') or ''
            if file_size is None:
                file_size = first.get('file_size')
            if message_type == MessageType.TEXT:
                message_type = MessageType.PHOTO
            # Caption if content is non-empty text that is not an attachment path.
            if not text_content or text_content in paths:
                text_content = first['path']
        if not text_content:
            return

        waveform = content.get('waveform') or []
        if not isinstance(waveform, list):
            waveform = []
        waveform = [
            max(0.0, min(1.0, float(value)))
            for value in waveform[:128]
            if isinstance(value, (int, float))
        ]
        try:
            voice_duration_ms = max(0, int(content.get('voice_duration_ms') or 0))
        except (TypeError, ValueError):
            voice_duration_ms = 0

        message = await self._create_message(
            message_type,
            text_content,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            waveform=waveform,
            voice_duration_ms=voice_duration_ms or None,
            attachments=attachments,
            reply_to_id=reply_to_id,
        )
        if not message:
            return

        await self._broadcast_typing(False)

        payload = await self._serialize_message(message)
        if client_id:
            payload['client_id'] = client_id
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'chat.message', 'message': payload},
        )
        # Превью в списке чатов (presence / user_* группы), даже если чат не открыт
        await self._broadcast_chat_preview(payload)

    async def _handle_edit_message(self, content):
        message_id = content.get('message_id')
        if not message_id:
            return
        new_text = (content.get('content') or '').strip()
        message = await self._edit_message(message_id, new_text)
        if not message:
            return
        payload = await self._serialize_message(message)
        await self.channel_layer.group_send(
            self.room_group_name,
            {'type': 'chat.message_edited', 'message': payload},
        )
        await self._broadcast_chat_preview(payload)

    async def _mark_read(self, message_ids=None):
        ids = await self._mark_messages_read(message_ids)
        if ids:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat.messages_read',
                    'message_ids': [str(mid) for mid in ids],
                    'reader_id': str(self.user.id),
                    'read_at': timezone.now().isoformat(),
                },
            )
        return ids

    async def _touch_viewing(self):
        if hasattr(self, 'user') and hasattr(self, 'chat_id'):
            await database_sync_to_async(touch_chat_viewing)(self.user.id, self.chat_id)

    async def chat_message(self, event):
        await self._touch_viewing()
        await self.send_json({'action': 'message.new', 'message': event['message']})

    async def chat_message_deleted(self, event):
        await self._touch_viewing()
        await self.send_json({
            'action': 'message.deleted',
            'message_id': event['message_id'],
        })

    async def chat_message_edited(self, event):
        await self._touch_viewing()
        await self.send_json({
            'action': 'message.edited',
            'message': event['message'],
        })

    async def chat_messages_read(self, event):
        await self._touch_viewing()
        await self.send_json({
            'action': 'messages.read',
            'message_ids': event['message_ids'],
            'reader_id': event['reader_id'],
            'read_at': event['read_at'],
        })

    async def chat_typing(self, event):
        if str(self.user.id) == str(event.get('user_id')):
            return
        await self._touch_viewing()
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

    def _normalize_attachments(self, raw):
        if not isinstance(raw, list):
            return []
        from django.conf import settings
        max_count = getattr(settings, 'CHAT_ATTACHMENTS_MAX_COUNT', 10)
        items = []
        for item in raw[:max_count]:
            if not isinstance(item, dict):
                continue
            path = (item.get('path') or '').strip()
            if not path:
                continue
            try:
                size = item.get('file_size')
                size = int(size) if size is not None else None
                if size is not None and size < 0:
                    size = None
            except (TypeError, ValueError):
                size = None
            items.append({
                'path': path,
                'file_name': (item.get('file_name') or '').strip()[:255],
                'mime_type': (item.get('mime_type') or '').strip()[:128],
                'file_size': size,
            })
        return items

    @database_sync_to_async
    def _create_message(
        self,
        message_type,
        content,
        file_name='',
        mime_type='',
        file_size=None,
        waveform=None,
        voice_duration_ms=None,
        attachments=None,
        reply_to_id=None,
    ):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return None
        if not user_in_chat(chat, self.user):
            return None

        reply_to = None
        if reply_to_id:
            try:
                reply_to = (
                    Message.objects.filter(
                        id=reply_to_id,
                        chat=chat,
                        deleted_at__isnull=True,
                    )
                    .exclude(hidden_for__user=self.user)
                    .select_related('sender')
                    .get()
                )
            except (Message.DoesNotExist, ValueError, TypeError):
                return None

        message = Message.objects.create(
            chat=chat,
            sender=self.user,
            message_type=message_type,
            content=content,
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
            waveform=waveform or [],
            voice_duration_ms=voice_duration_ms,
            attachments=attachments or [],
            reply_to=reply_to,
        )
        invalidate_chat_history_cache(chat.id)
        chat.updated_at = timezone.now()
        chat.save(update_fields=['updated_at'])

        recipient_ids = list(
            chat.participants.exclude(user=self.user).values_list('user_id', flat=True)
        )
        from apps.notifications.tasks import enqueue_message_push
        for recipient_id in recipient_ids:
            enqueue_message_push(str(message.id), str(recipient_id))

        return message

    @database_sync_to_async
    def _mark_messages_read(self, message_ids=None):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return []
        try:
            return mark_messages_read(chat, self.user, message_ids)
        except PermissionError:
            return []

    @database_sync_to_async
    def _edit_message(self, message_id, new_text):
        try:
            message = Message.objects.select_related('chat', 'sender').get(
                id=message_id,
                chat_id=self.chat_id,
                deleted_at__isnull=True,
            )
        except (Message.DoesNotExist, ValueError, TypeError):
            return None

        if message.sender_id != self.user.id:
            return None
        if message.message_type not in EDITABLE_MESSAGE_TYPES:
            return None
        if not user_in_chat(message.chat, self.user):
            return None

        edit_limit = timezone.now() - timezone.timedelta(days=MESSAGE_EDIT_MAX_DAYS)
        if message.sent_at < edit_limit:
            return None

        if message.message_type == MessageType.TEXT:
            if not new_text:
                return None
            if new_text == (message.content or '').strip():
                return None
            message.content = new_text
        else:
            # Photo caption: empty clears caption back to first media path.
            first_path = ''
            if isinstance(message.attachments, list):
                for item in message.attachments:
                    if isinstance(item, dict) and item.get('path'):
                        first_path = item['path'].strip()
                        break
            if not first_path and looks_like_storage_path(message.content or ''):
                first_path = (message.content or '').strip()
            if not first_path:
                return None
            current_caption = get_photo_caption(message)
            if new_text == current_caption:
                return None
            message.content = new_text if new_text else first_path

        message.edited_at = timezone.now()
        message.save(update_fields=['content', 'edited_at'])
        invalidate_chat_history_cache(message.chat_id)
        message.chat.updated_at = timezone.now()
        message.chat.save(update_fields=['updated_at'])
        return message

    @database_sync_to_async
    def _serialize_message(self, message):
        import json
        from apps.chats.serializers import MessageSerializer
        return json.loads(json.dumps(MessageSerializer(message).data, default=str))

    @database_sync_to_async
    def _participant_user_ids(self):
        try:
            chat = Chat.objects.get(id=self.chat_id)
        except Chat.DoesNotExist:
            return []
        return [str(uid) for uid in chat.participants.values_list('user_id', flat=True)]

    async def _broadcast_chat_preview(self, payload):
        from apps.notifications.services import user_channel_group

        for uid in await self._participant_user_ids():
            await self.channel_layer.group_send(
                user_channel_group(uid),
                {'type': 'chat.preview', 'message': payload},
            )
