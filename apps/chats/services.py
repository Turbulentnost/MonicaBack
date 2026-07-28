import os
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Prefetch
from django.utils import timezone

from apps.chats.models import (
    Chat,
    ChatParticipant,
    ChatParticipantRole,
    ChatType,
    Message,
    MessageHidden,
    MessageType,
)
from apps.users.services.minio_service import delete_object, get_presigned_url, upload_file

User = get_user_model()

MESSAGE_EDIT_MAX_DAYS = 7
GROUP_TITLE_MAX_LEN = 64
GROUP_ADMIN_ROLES = {ChatParticipantRole.OWNER, ChatParticipantRole.ADMIN}


def get_chat_history_cache_version(chat_id) -> int:
    return int(cache.get(f'chat-history-version:{chat_id}') or 1)


def invalidate_chat_history_cache(chat_id):
    key = f'chat-history-version:{chat_id}'
    cache.set(key, get_chat_history_cache_version(chat_id) + 1, timeout=None)


def looks_like_storage_path(value: str) -> bool:
    """True for MinIO object keys like chat-files/... (not a text caption)."""
    if not value or not isinstance(value, str):
        return False
    text = value.strip()
    if not text or ' ' in text or '\n' in text or '\t' in text:
        return False
    return text.startswith('chat-files/') or text.startswith('user-avatars/')


def get_photo_caption(message) -> str:
    if getattr(message, 'message_type', None) != MessageType.PHOTO:
        return ''
    content = (message.content or '').strip()
    if not content:
        return ''
    paths = set()
    if isinstance(message.attachments, list):
        for item in message.attachments:
            if isinstance(item, dict):
                path = (item.get('path') or '').strip()
                if path:
                    paths.add(path)
    if content in paths:
        return ''
    if looks_like_storage_path(content):
        return ''
    return content


ALLOWED_IMAGE_TYPES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
}


def get_or_create_direct_chat(user_a, user_b):
    if user_a.id == user_b.id:
        raise ValueError('Нельзя создать чат с самим собой')

    existing = (
        Chat.objects.filter(chat_type=ChatType.DIRECT)
        .filter(participants__user=user_a)
        .filter(participants__user=user_b)
        .annotate(participant_count=Count('participants', distinct=True))
        .filter(participant_count=2)
        .distinct()
        .first()
    )

    if existing:
        return existing, False

    chat = Chat.objects.create(chat_type=ChatType.DIRECT)
    ChatParticipant.objects.create(
        chat=chat, user=user_a, role=ChatParticipantRole.MEMBER,
    )
    ChatParticipant.objects.create(
        chat=chat, user=user_b, role=ChatParticipantRole.MEMBER,
    )
    return chat, True


def get_or_create_favorites_chat(user):
    """Personal Saved Messages / Избранное — solo chat owned by the user."""
    existing = (
        Chat.objects.filter(chat_type=ChatType.FAVORITES)
        .filter(participants__user=user)
        .distinct()
        .first()
    )
    if existing:
        return existing, False

    chat = Chat.objects.create(
        chat_type=ChatType.FAVORITES,
        title='Избранное',
        created_by=user,
    )
    ChatParticipant.objects.create(
        chat=chat,
        user=user,
        role=ChatParticipantRole.OWNER,
    )
    return chat, True


def get_user_chats(user):
    return Chat.objects.filter(participants__user=user).distinct()


def get_chat_partner(chat, user):
    chat_type = getattr(chat, 'chat_type', ChatType.DIRECT)
    if chat_type in {ChatType.GROUP, ChatType.FAVORITES}:
        return None
    participant = chat.participants.exclude(user=user).select_related('user').first()
    return participant.user if participant else None


def user_in_chat(chat, user):
    return chat.participants.filter(user=user).exists()


def get_participant(chat, user):
    return chat.participants.filter(user=user).select_related('user').first()


def user_can_manage_group(chat, user):
    participant = get_participant(chat, user)
    return bool(participant and participant.role in GROUP_ADMIN_ROLES)


def create_group_chat(creator, title, member_ids, photo=None):
    """
    Создаёт группу. member_ids — UUID существующих пользователей (без создателя).
    Разрешены любые существующие user id (не только с direct-чатом).
    photo — опциональный аватар группы (UploadedFile).
    """
    title = (title or '').strip()
    if not title:
        raise ValueError('Название группы обязательно')
    if len(title) > GROUP_TITLE_MAX_LEN:
        raise ValueError(f'Название не длиннее {GROUP_TITLE_MAX_LEN} символов')

    if not member_ids:
        raise ValueError('Укажите хотя бы одного участника')

    unique_ids = []
    seen = set()
    for raw_id in member_ids:
        try:
            uid = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            raise ValueError(f'Некорректный id участника: {raw_id}')
        if uid == creator.id or uid in seen:
            continue
        seen.add(uid)
        unique_ids.append(uid)

    if not unique_ids:
        raise ValueError('Укажите хотя бы одного участника кроме себя')

    members = list(User.objects.filter(id__in=unique_ids, is_active=True))
    found_ids = {user.id for user in members}
    missing = [str(uid) for uid in unique_ids if uid not in found_ids]
    if missing:
        raise ValueError(f'Пользователи не найдены: {", ".join(missing)}')

    chat = Chat.objects.create(
        chat_type=ChatType.GROUP,
        title=title,
        created_by=creator,
    )
    ChatParticipant.objects.create(
        chat=chat,
        user=creator,
        role=ChatParticipantRole.OWNER,
    )
    ChatParticipant.objects.bulk_create([
        ChatParticipant(
            chat=chat,
            user=member,
            role=ChatParticipantRole.MEMBER,
        )
        for member in members
    ])
    if photo is not None:
        set_group_photo(chat, creator, photo)
        chat.refresh_from_db(fields=['photo', 'updated_at'])
    return chat


def set_group_photo(chat, actor, uploaded_file):
    if chat.chat_type != ChatType.GROUP:
        raise PermissionError('Аватар можно задать только для группы')
    if not user_can_manage_group(chat, actor):
        raise PermissionError('Недостаточно прав')
    if not uploaded_file:
        raise ValueError('Файл обязателен')

    content_type = (getattr(uploaded_file, 'content_type', None) or '').lower()
    name = getattr(uploaded_file, 'name', '') or 'group.jpg'
    ext = os.path.splitext(name)[1].lower()
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    if content_type not in ALLOWED_IMAGE_TYPES and ext not in image_exts:
        raise ValueError('Поддерживаются JPG, PNG, WEBP и GIF')

    max_bytes = min(10, settings.CHAT_IMAGE_MAX_SIZE_MB) * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ValueError(f'Файл слишком большой (макс. {max_bytes // (1024 * 1024)} МБ)')

    object_ext = ext if ext in image_exts else '.jpg'
    if object_ext == '.jpeg':
        object_ext = '.jpg'
    if content_type not in ALLOWED_IMAGE_TYPES:
        content_type = {
            '.jpg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }.get(object_ext, 'image/jpeg')

    object_name = f'{chat.id}/avatar/{uuid.uuid4().hex}{object_ext}'
    path = upload_file(
        settings.MINIO_BUCKET_CHAT_FILES,
        object_name,
        uploaded_file,
        content_type,
    )
    old_path = chat.photo
    chat.photo = path
    chat.save(update_fields=['photo', 'updated_at'])
    if old_path and old_path != path:
        delete_object(old_path)
    return {
        'photo': path,
        'photo_url': get_presigned_url(path),
    }


def add_group_members(chat, actor, user_ids):
    if chat.chat_type != ChatType.GROUP:
        raise PermissionError('Участников можно добавлять только в группу')
    if not user_can_manage_group(chat, actor):
        raise PermissionError('Недостаточно прав')

    unique_ids = []
    seen = set()
    for raw_id in user_ids or []:
        try:
            uid = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            raise ValueError(f'Некорректный id участника: {raw_id}')
        if uid in seen:
            continue
        seen.add(uid)
        unique_ids.append(uid)

    if not unique_ids:
        raise ValueError('Укажите хотя бы одного участника')

    existing_ids = set(
        chat.participants.filter(user_id__in=unique_ids).values_list('user_id', flat=True)
    )
    to_add_ids = [uid for uid in unique_ids if uid not in existing_ids]
    if not to_add_ids:
        return []

    users = list(User.objects.filter(id__in=to_add_ids, is_active=True))
    found = {user.id for user in users}
    missing = [str(uid) for uid in to_add_ids if uid not in found]
    if missing:
        raise ValueError(f'Пользователи не найдены: {", ".join(missing)}')

    created = ChatParticipant.objects.bulk_create([
        ChatParticipant(chat=chat, user=user, role=ChatParticipantRole.MEMBER)
        for user in users
    ])
    chat.save(update_fields=['updated_at'])
    return created


def remove_group_member(chat, actor, target_user_id):
    if chat.chat_type != ChatType.GROUP:
        raise PermissionError('Участников можно удалять только из группы')

    try:
        target_user_id = uuid.UUID(str(target_user_id))
    except (TypeError, ValueError):
        raise ValueError('Некорректный id участника')

    target = chat.participants.filter(user_id=target_user_id).select_related('user').first()
    if not target:
        raise LookupError('Участник не найден')

    actor_participation = get_participant(chat, actor)
    if not actor_participation:
        raise PermissionError('Нет доступа к чату')

    is_self = actor.id == target_user_id
    if not is_self and actor_participation.role not in GROUP_ADMIN_ROLES:
        raise PermissionError('Недостаточно прав')

    if target.role == ChatParticipantRole.OWNER:
        owners_count = chat.participants.filter(role=ChatParticipantRole.OWNER).count()
        if owners_count <= 1:
            raise PermissionError(
                'Нельзя удалить последнего владельца. Сначала назначьте другого owner.'
            )

    target.delete()
    chat.save(update_fields=['updated_at'])


def update_group_title(chat, actor, title):
    if chat.chat_type != ChatType.GROUP:
        raise PermissionError('Название можно менять только у группы')
    if not user_can_manage_group(chat, actor):
        raise PermissionError('Недостаточно прав')

    title = (title or '').strip()
    if not title:
        raise ValueError('Название группы обязательно')
    if len(title) > GROUP_TITLE_MAX_LEN:
        raise ValueError(f'Название не длиннее {GROUP_TITLE_MAX_LEN} символов')

    chat.title = title
    chat.save(update_fields=['title', 'updated_at'])
    return chat


def serialize_chat_member(participant, request=None):
    from apps.users.serializers import UserSerializer

    user_data = UserSerializer(participant.user, context={'request': request}).data
    return {
        'id': user_data['id'],
        'nickname': user_data['nickname'],
        'first_name': user_data['first_name'],
        'last_name': user_data['last_name'],
        'photo': user_data.get('photo') or '',
        'photo_url': user_data.get('photo_url'),
        'role': participant.role,
        'is_online': user_data.get('is_online'),
    }


def serialize_chat_list_item(chat, user, request=None):
    from apps.chats.serializers import MessageSerializer
    from apps.users.serializers import UserSerializer

    ctx = {'request': request}
    is_group = chat.chat_type == ChatType.GROUP
    is_favorites = chat.chat_type == ChatType.FAVORITES
    participants = list(chat.participants.all())
    last_message = get_last_visible_message(chat, user)
    if is_favorites:
        partner = user
        members = [serialize_chat_member(p, request=request) for p in participants]
        title = chat.title or 'Избранное'
    elif is_group:
        partner = None
        members = [serialize_chat_member(p, request=request) for p in participants]
        title = chat.title
    else:
        partner = next((p.user for p in participants if p.user_id != user.id), None)
        if partner is None:
            partner = get_chat_partner(chat, user)
        members = None
        title = None

    photo = (chat.photo or '') if is_group else ''
    return {
        'id': chat.id,
        'chat_type': chat.chat_type,
        'is_group': is_group,
        'is_favorites': is_favorites,
        'title': title,
        'photo': photo or None,
        'photo_url': get_presigned_url(photo) if photo else None,
        'partner': UserSerializer(partner, context=ctx).data if partner else None,
        'members': members,
        'members_count': len(participants),
        'last_message': (
            MessageSerializer(last_message, context=ctx).data if last_message else None
        ),
        'updated_at': chat.updated_at,
        'background_url': get_participant_background_url(chat, user),
    }


def chats_with_participants_prefetch():
    return Prefetch(
        'participants',
        queryset=ChatParticipant.objects.select_related('user').order_by('joined_at'),
    )


def get_visible_messages(chat, user):
    return (
        chat.messages
        .filter(deleted_at__isnull=True)
        .exclude(hidden_for__user=user)
        .select_related('sender', 'reply_to__sender')
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
        'audio/mp4': '.m4a',
        'audio/m4a': '.m4a',
        'audio/x-m4a': '.m4a',
        'audio/aac': '.aac',
        'audio/mpeg': '.mp3',
        'audio/ogg': '.ogg',
        'audio/webm': '.webm',
        'application/ogg': '.ogg',
        'application/pdf': '.pdf',
        'text/plain': '.txt',
        'text/x-python': '.py',
        'application/x-python-code': '.py',
        'text/javascript': '.js',
        'application/javascript': '.js',
        'application/x-javascript': '.js',
        'text/js': '.js',
        'application/vnd.android.package-archive': '.apk',
    }
    return mapping.get(content_type, '.bin')


def upload_chat_file(chat, user, uploaded_file):
    """Upload any file type; only size and attachment count are limited."""
    if not user_in_chat(chat, user):
        raise PermissionError('Нет доступа к чату')

    content_type = (uploaded_file.content_type or '').strip() or 'application/octet-stream'
    ext = os.path.splitext(uploaded_file.name or '')[1].lower()
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    audio_exts = {'.m4a', '.aac', '.mp3', '.ogg', '.opus'}
    video_exts = {'.mp4', '.webm', '.mov', '.mkv', '.m4v', '.avi', '.ogv', '.3gp'}

    # Normalize common MIME guesses when the browser sends octet-stream / empty.
    if ext in image_exts and content_type not in ALLOWED_IMAGE_TYPES:
        content_type = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }.get(ext, 'image/jpeg')
    elif ext in video_exts and not content_type.startswith('video/'):
        content_type = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mov': 'video/quicktime',
            '.mkv': 'video/x-matroska',
            '.m4v': 'video/mp4',
            '.avi': 'video/x-msvideo',
            '.ogv': 'video/ogg',
            '.3gp': 'video/3gpp',
        }.get(ext, 'video/mp4')
    elif ext == '.py':
        content_type = 'text/x-python'
    elif ext == '.js':
        content_type = 'text/javascript'
    elif ext == '.apk' and content_type == 'application/octet-stream':
        content_type = 'application/vnd.android.package-archive'
    elif not content_type or content_type == 'application/octet-stream':
        # Keep original name extension in storage; generic binary is fine.
        content_type = 'application/octet-stream'

    is_image = content_type in ALLOWED_IMAGE_TYPES or ext in image_exts
    is_video = content_type.startswith('video/') or ext in video_exts
    is_audio = (
        not is_video
        and (content_type.startswith('audio/') or ext in audio_exts)
    )
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
    message_type = (
        MessageType.PHOTO if is_image
        else MessageType.VOICE if is_audio
        else MessageType.FILE
    )
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


def get_participant_background_url(chat, user):
    # Не ходим через related manager после Prefetch(select_related='user'):
    # .only('background') конфликтует с уже заданным select_related.
    prefetched = getattr(chat, '_prefetched_objects_cache', {}).get('participants')
    if prefetched is not None:
        participant = next((p for p in prefetched if p.user_id == user.id), None)
    else:
        participant = ChatParticipant.objects.filter(chat_id=chat.id, user_id=user.id).first()
    if not participant or not participant.background:
        return None
    return get_presigned_url(participant.background)


def set_chat_background(chat, user, uploaded_file):
    if not uploaded_file:
        raise ValueError('Файл обязателен')

    content_type = (getattr(uploaded_file, 'content_type', None) or '').lower()
    name = getattr(uploaded_file, 'name', '') or 'background.jpg'
    ext = os.path.splitext(name)[1].lower()
    image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    if content_type not in ALLOWED_IMAGE_TYPES and ext not in image_exts:
        raise ValueError('Поддерживаются JPG, PNG, WEBP и GIF')

    max_bytes = settings.CHAT_IMAGE_MAX_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise ValueError(
            f'Файл слишком большой (макс. {settings.CHAT_IMAGE_MAX_SIZE_MB} МБ)'
        )

    object_ext = ext if ext in image_exts else '.jpg'
    if object_ext == '.jpeg':
        object_ext = '.jpg'
    if content_type not in ALLOWED_IMAGE_TYPES:
        content_type = {
            '.jpg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
        }.get(object_ext, 'image/jpeg')

    object_name = f'{chat.id}/bg/{user.id}/{uuid.uuid4().hex}{object_ext}'
    path = upload_file(
        settings.MINIO_BUCKET_CHAT_FILES,
        object_name,
        uploaded_file,
        content_type,
    )

    participant, _ = ChatParticipant.objects.get_or_create(chat=chat, user=user)
    old_path = participant.background
    participant.background = path
    participant.save(update_fields=['background'])
    if old_path and old_path != path:
        delete_object(old_path)

    return {
        'background': path,
        'background_url': get_presigned_url(path),
    }


def clear_chat_background(chat, user):
    participant = chat.participants.filter(user=user).first()
    if not participant:
        return {'background': '', 'background_url': None}
    old_path = participant.background
    if old_path:
        participant.background = ''
        participant.save(update_fields=['background'])
        delete_object(old_path)
    return {'background': '', 'background_url': None}


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
    """
    Отмечает чужие непрочитанные сообщения как прочитанные.
    Если переданы конкретные id — также помечает все более ранние
    непрочитанные в этом чате (просмотр позднего = просмотр предыдущих).
    """
    if not user_in_chat(chat, user):
        raise PermissionError('Нет доступа к чату')

    qs = Message.objects.filter(
        chat=chat,
        deleted_at__isnull=True,
        read_at__isnull=True,
    ).exclude(sender=user)

    if message_ids:
        # Anchor on the newest of the requested messages; everything at/before
        # that point from others is considered read as well.
        latest_sent_at = (
            Message.objects.filter(chat=chat, id__in=message_ids)
            .exclude(sender=user)
            .order_by('-sent_at')
            .values_list('sent_at', flat=True)
            .first()
        )
        if latest_sent_at is None:
            return []
        qs = qs.filter(sent_at__lte=latest_sent_at)

    ids = list(qs.values_list('id', flat=True))
    if not ids:
        return []

    now = timezone.now()
    Message.objects.filter(id__in=ids).update(read_at=now)
    invalidate_chat_history_cache(chat.id)
    return ids


def delete_message_for_user(message, user, scope):
    if not user_in_chat(message.chat, user):
        raise PermissionError('Нет доступа к чату')

    if message.message_type == MessageType.CALL:
        raise PermissionError('Сообщения о звонках нельзя удалять')

    if scope == 'me':
        MessageHidden.objects.get_or_create(user=user, message=message)
        invalidate_chat_history_cache(message.chat_id)
        return 'me'

    if scope != 'everyone':
        raise ValueError('scope должен быть me или everyone')

    if not can_delete_for_everyone(message, user):
        raise PermissionError('Нельзя удалить у всех')

    paths = set()
    if (
        message.message_type in (MessageType.PHOTO, MessageType.FILE, MessageType.VOICE)
        and message.content
        and looks_like_storage_path(message.content)
    ):
        paths.add(message.content)
    if isinstance(message.attachments, list):
        for item in message.attachments:
            if isinstance(item, dict):
                path = (item.get('path') or '').strip()
                if path:
                    paths.add(path)
    if message.message_type == MessageType.FORWARD and isinstance(message.forward_bundle, list):
        for forwarded in message.forward_bundle:
            if not isinstance(forwarded, dict):
                continue
            content = (forwarded.get('content') or '').strip()
            if looks_like_storage_path(content):
                paths.add(content)
            for item in forwarded.get('attachments') or []:
                if isinstance(item, dict):
                    path = (item.get('path') or '').strip()
                    if path:
                        paths.add(path)
    for path in paths:
        delete_object(path)

    message.deleted_at = timezone.now()
    message.deleted_by = user
    message.save(update_fields=['deleted_at', 'deleted_by'])
    invalidate_chat_history_cache(message.chat_id)
    broadcast_message_deleted(message.chat_id, message.id)
    return 'everyone'
