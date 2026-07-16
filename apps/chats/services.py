import os
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.chats.models import Chat, ChatParticipant, Message, MessageHidden, MessageType
from apps.users.services.minio_service import delete_object, get_presigned_url, upload_file

User = get_user_model()

ALLOWED_IMAGE_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
}
ALLOWED_FILE_TYPES = ALLOWED_IMAGE_TYPES | {
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain',
    'text/x-python',
    'application/x-python-code',
    'text/javascript',
    'application/javascript',
    'application/x-javascript',
    'text/js',
    'application/octet-stream',  # браузеры часто так отдают .py/.js
}


def get_or_create_direct_chat(user_a, user_b):
    if user_a.id == user_b.id:
        raise ValueError('Нельзя создать чат с самим собой')

    existing = Chat.objects.filter(
        participants__user=user_a
    ).filter(
        participants__user=user_b
    ).distinct().first()

    if existing:
        return existing, False

    chat = Chat.objects.create()
    ChatParticipant.objects.create(chat=chat, user=user_a)
    ChatParticipant.objects.create(chat=chat, user=user_b)
    return chat, True


def get_user_chats(user):
    return Chat.objects.filter(participants__user=user).distinct()


def get_chat_partner(chat, user):
    participant = chat.participants.exclude(user=user).select_related('user').first()
    return participant.user if participant else None


def user_in_chat(chat, user):
    return chat.participants.filter(user=user).exists()


def get_visible_messages(chat, user):
    return (
        chat.messages
        .filter(deleted_at__isnull=True)
        .exclude(hidden_for__user=user)
        .select_related('sender')
    )


def get_last_visible_message(chat, user):
    return get_visible_messages(chat, user).order_by('-sent_at').first()


def _extension_for_upload(filename, content_type):
    ext = os.path.splitext(filename or '')[1].lower()
    if ext:
        return ext
    mapping = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
        'application/pdf': '.pdf',
        'text/plain': '.txt',
        'text/x-python': '.py',
        'application/x-python-code': '.py',
        'text/javascript': '.js',
        'application/javascript': '.js',
        'application/x-javascript': '.js',
        'text/js': '.js',
    }
    return mapping.get(content_type, '.bin')


def upload_chat_file(chat, user, uploaded_file):
    if not user_in_chat(chat, user):
        raise PermissionError('Нет доступа к чату')

    content_type = uploaded_file.content_type or 'application/octet-stream'
    ext = os.path.splitext(uploaded_file.name or '')[1].lower()
    is_python = ext == '.py'
    is_javascript = ext == '.js'
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    known_exts = image_exts | {'.py', '.js', '.pdf', '.doc', '.docx', '.txt'}

    # Браузер часто врёт MIME — доверяем расширению
    if ext in image_exts and content_type not in ALLOWED_IMAGE_TYPES:
        content_type = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }.get(ext, 'image/jpeg')

    if content_type not in ALLOWED_FILE_TYPES and not is_python and not is_javascript and ext not in known_exts:
        raise ValueError(f'Неподдерживаемый тип файла: {uploaded_file.name}')

    if content_type == 'application/octet-stream' and ext not in known_exts:
        raise ValueError(f'Неподдерживаемый тип файла: {uploaded_file.name}')

    if is_python:
        content_type = 'text/x-python'
    elif is_javascript:
        content_type = 'text/javascript'

    is_image = content_type in ALLOWED_IMAGE_TYPES or ext in image_exts
    max_bytes = (
        settings.CHAT_IMAGE_MAX_SIZE_MB if is_image else settings.CHAT_FILE_MAX_SIZE_MB
    ) * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ValueError(
            f'Файл «{uploaded_file.name}» слишком большой '
            f'(макс. {max_bytes // (1024 * 1024)} МБ)'
        )

    object_ext = _extension_for_upload(uploaded_file.name, content_type)
    object_name = f'{chat.id}/{uuid.uuid4()}{object_ext}'
    path = upload_file(
        settings.MINIO_BUCKET_CHAT_FILES,
        object_name,
        uploaded_file,
        content_type,
    )
    message_type = MessageType.PHOTO if is_image else MessageType.FILE
    return {
        'path': path,
        'content_url': get_presigned_url(path),
        'file_name': uploaded_file.name,
        'mime_type': content_type,
        'file_size': uploaded_file.size,
        'message_type': message_type,
    }


def upload_chat_files(chat, user, uploaded_files):
    if not uploaded_files:
        raise ValueError('Нужен хотя бы один файл')
    max_count = settings.CHAT_ATTACHMENTS_MAX_COUNT
    if len(uploaded_files) > max_count:
        raise ValueError(f'Можно прикрепить не больше {max_count} файлов')
    return [upload_chat_file(chat, user, f) for f in uploaded_files]


def can_delete_for_everyone(message, user):
    if message.sender_id != user.id:
        return False
    if message.deleted_at:
        return False
    limit = timezone.now() - timezone.timedelta(hours=settings.MESSAGE_DELETE_FOR_ALL_HOURS)
    return message.sent_at >= limit


def broadcast_message_deleted(chat_id, message_id):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f'chat_{chat_id}',
        {'type': 'chat.message_deleted', 'message_id': str(message_id)},
    )


def broadcast_messages_read(chat_id, message_ids, reader_id):
    if not message_ids:
        return
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f'chat_{chat_id}',
        {
            'type': 'chat.messages_read',
            'message_ids': [str(mid) for mid in message_ids],
            'reader_id': str(reader_id),
            'read_at': timezone.now().isoformat(),
        },
    )


def mark_messages_read(chat, user, message_ids=None):
    """Отмечает чужие непрочитанные сообщения как прочитанные. Возвращает id."""
    if not user_in_chat(chat, user):
        raise PermissionError('Нет доступа к чату')

    qs = Message.objects.filter(
        chat=chat,
        deleted_at__isnull=True,
        read_at__isnull=True,
    ).exclude(sender=user)

    if message_ids:
        qs = qs.filter(id__in=message_ids)

    ids = list(qs.values_list('id', flat=True))
    if not ids:
        return []

    now = timezone.now()
    Message.objects.filter(id__in=ids).update(read_at=now)
    return ids


def delete_message_for_user(message, user, scope):
    if not user_in_chat(message.chat, user):
        raise PermissionError('Нет доступа к чату')

    if scope == 'me':
        MessageHidden.objects.get_or_create(user=user, message=message)
        return 'me'

    if scope != 'everyone':
        raise ValueError('scope должен быть me или everyone')

    if not can_delete_for_everyone(message, user):
        raise PermissionError('Нельзя удалить у всех')

    if message.message_type in (MessageType.PHOTO, MessageType.FILE) and message.content:
        delete_object(message.content)

    message.deleted_at = timezone.now()
    message.deleted_by = user
    message.save(update_fields=['deleted_at', 'deleted_by'])
    broadcast_message_deleted(message.chat_id, message.id)
    return 'everyone'
