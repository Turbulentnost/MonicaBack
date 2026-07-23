import io
import json
import os
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.chats.models import Chat, Message, MessageHidden, MessageType
from apps.chats.services import (
    get_photo_caption,
    invalidate_chat_history_cache,
    looks_like_storage_path,
)
from apps.notifications.services import user_channel_group
from apps.users.services.minio_service import (
    delete_object,
    download_object_bytes,
    upload_file,
)

MAX_FORWARD_MESSAGES = 50


class ForwardError(Exception):
    def __init__(self, detail, status_code=400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _copy_storage_path(source_path, target_chat, content_type, copied_paths):
    if source_path in copied_paths:
        return copied_paths[source_path]

    data = download_object_bytes(source_path)
    if data is None:
        raise ForwardError(f'Не удалось скопировать файл: {source_path}')

    _, object_name = source_path.split('/', 1)
    extension = os.path.splitext(object_name)[1][:16]
    target_name = f'{target_chat.id}/forwards/{uuid.uuid4()}{extension}'
    target_path = upload_file(
        settings.MINIO_BUCKET_CHAT_FILES,
        target_name,
        io.BytesIO(data),
        content_type or 'application/octet-stream',
    )
    copied_paths[source_path] = target_path
    return target_path


def _snapshot_message(message, target_chat, copied_paths):
    content = message.content or ''
    if looks_like_storage_path(content):
        content = _copy_storage_path(
            content,
            target_chat,
            message.mime_type,
            copied_paths,
        )

    attachments = []
    raw_attachments = message.attachments if isinstance(message.attachments, list) else []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            continue
        path = (raw.get('path') or '').strip()
        if not path:
            continue
        copied_path = _copy_storage_path(
            path,
            target_chat,
            raw.get('mime_type') or message.mime_type,
            copied_paths,
        )
        attachments.append({
            'path': copied_path,
            'file_name': raw.get('file_name') or '',
            'mime_type': raw.get('mime_type') or '',
            'file_size': raw.get('file_size'),
        })

    return {
        'original_id': str(message.id),
        'original_chat_id': str(message.chat_id),
        'sender': {
            'id': str(message.sender_id),
            'nickname': message.sender.nickname,
            'first_name': message.sender.first_name,
            'last_name': message.sender.last_name,
            'photo': message.sender.photo or '',
        },
        'message_type': message.message_type,
        'content': content,
        'caption': get_photo_caption(message),
        'file_name': message.file_name or '',
        'mime_type': message.mime_type or '',
        'file_size': message.file_size,
        'attachments': attachments,
        'waveform': message.waveform if isinstance(message.waveform, list) else [],
        'voice_duration_ms': message.voice_duration_ms,
        'sent_at': message.sent_at.isoformat(),
    }


def _load_forward_messages(source_chat, user, message_ids):
    messages = (
        Message.objects.filter(chat=source_chat, id__in=message_ids)
        .select_related('sender')
    )
    by_id = {message.id: message for message in messages}
    hidden_ids = set(
        MessageHidden.objects.filter(
            user=user,
            message_id__in=message_ids,
        ).values_list('message_id', flat=True)
    )

    ordered = []
    for message_id in message_ids:
        message = by_id.get(message_id)
        if (
            message is None
            or message.deleted_at is not None
            or message.id in hidden_ids
        ):
            raise ForwardError('Одно или несколько сообщений недоступны')
        if message.message_type == MessageType.CALL:
            raise ForwardError('Сообщения о звонках нельзя пересылать')
        ordered.append(message)
    return ordered


def _broadcast_forward(message, payload):
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f'chat_{message.chat_id}',
            {'type': 'chat.message', 'message': payload},
        )
        participant_ids = message.chat.participants.values_list('user_id', flat=True)
        for participant_id in participant_ids:
            async_to_sync(channel_layer.group_send)(
                user_channel_group(participant_id),
                {'type': 'chat.preview', 'message': payload},
            )


def forward_messages(*, target_chat_id, source_chat_id, message_ids, user, comment=''):
    if not message_ids:
        raise ForwardError('Нужен хотя бы один message_id')
    if len(message_ids) > MAX_FORWARD_MESSAGES:
        raise ForwardError(f'Можно переслать не больше {MAX_FORWARD_MESSAGES} сообщений')

    chats = {
        chat.id: chat
        for chat in Chat.objects.filter(id__in=[target_chat_id, source_chat_id])
    }
    target_chat = chats.get(target_chat_id)
    source_chat = chats.get(source_chat_id)
    if target_chat is None or source_chat is None:
        raise ForwardError('Чат не найден', status_code=404)

    accessible_chat_ids = set(
        Chat.objects.filter(
            id__in=[target_chat_id, source_chat_id],
            participants__user=user,
        ).values_list('id', flat=True)
    )
    if target_chat_id not in accessible_chat_ids or source_chat_id not in accessible_chat_ids:
        raise ForwardError('Нет доступа к исходному или целевому чату', status_code=403)

    source_messages = _load_forward_messages(source_chat, user, message_ids)
    copied_paths = {}
    committed = False
    try:
        bundle = [
            _snapshot_message(message, target_chat, copied_paths)
            for message in source_messages
        ]
        with transaction.atomic():
            created_message = Message.objects.create(
                chat=target_chat,
                sender=user,
                message_type=MessageType.FORWARD,
                content=(comment or '').strip(),
                forward_bundle=bundle,
                forwarded_from=source_messages[0],
            )
            target_chat.updated_at = timezone.now()
            target_chat.save(update_fields=['updated_at'])
            invalidate_chat_history_cache(target_chat.id)
        committed = True
    except Exception:
        if not committed:
            for copied_path in copied_paths.values():
                delete_object(copied_path)
        raise

    from apps.chats.serializers import MessageSerializer
    from apps.notifications.tasks import enqueue_message_push

    payload = json.loads(json.dumps(MessageSerializer(created_message).data, default=str))
    _broadcast_forward(created_message, payload)
    recipient_ids = target_chat.participants.exclude(user=user).values_list(
        'user_id', flat=True
    )
    for recipient_id in recipient_ids:
        enqueue_message_push(str(created_message.id), str(recipient_id))
    return created_message
