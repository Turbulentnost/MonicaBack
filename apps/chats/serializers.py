from copy import deepcopy

from rest_framework import serializers

from apps.chats.models import Message
from apps.chats.services import get_photo_caption, looks_like_storage_path
from apps.users.serializers import UserSerializer
from apps.users.services.minio_service import get_presigned_url


class MessageSerializer(serializers.ModelSerializer):
    sender = UserSerializer(read_only=True)
    content_url = serializers.SerializerMethodField()
    attachments = serializers.SerializerMethodField()
    caption = serializers.SerializerMethodField()
    forward_bundle = serializers.SerializerMethodField()
    reply_to_summary = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'chat', 'sender', 'message_type', 'content', 'content_url', 'caption',
            'file_name', 'mime_type', 'file_size', 'attachments',
            'forwarded_from', 'forward_bundle', 'reply_to_summary',
            'sent_at', 'edited_at', 'read_at', 'waveform', 'voice_duration_ms',
        ]
        read_only_fields = fields

    def _primary_media_path(self, obj):
        if isinstance(obj.attachments, list):
            for item in obj.attachments:
                if isinstance(item, dict):
                    path = (item.get('path') or '').strip()
                    if path:
                        return path
        content = (obj.content or '').strip()
        if content and looks_like_storage_path(content):
            return content
        return None

    def get_content_url(self, obj):
        if obj.message_type not in ('photo', 'file', 'voice'):
            return None
        path = self._primary_media_path(obj)
        return get_presigned_url(path) if path else None

    def get_caption(self, obj):
        return get_photo_caption(obj)

    def get_attachments(self, obj):
        raw = obj.attachments if isinstance(obj.attachments, list) else []
        items = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = (item.get('path') or '').strip()
            if not path:
                continue
            items.append({
                'path': path,
                'file_name': item.get('file_name') or '',
                'mime_type': item.get('mime_type') or '',
                'file_size': item.get('file_size'),
                'content_url': get_presigned_url(path),
            })
        # Backward compat: single photo/file without attachments list
        if not items and obj.message_type in ('photo', 'file', 'voice'):
            path = self._primary_media_path(obj)
            if path:
                items.append({
                    'path': path,
                    'file_name': obj.file_name or '',
                    'mime_type': obj.mime_type or '',
                    'file_size': obj.file_size,
                    'content_url': get_presigned_url(path),
                })
        return items

    def get_forward_bundle(self, obj):
        bundle = obj.forward_bundle if isinstance(obj.forward_bundle, list) else []
        result = deepcopy(bundle)
        for item in result:
            if not isinstance(item, dict):
                continue
            if not item.get('original_chat_id') and item.get('chat_id'):
                item['original_chat_id'] = item['chat_id']
            sender = item.get('sender')
            if isinstance(sender, dict):
                photo = (sender.get('photo') or '').strip()
                sender['photo_url'] = get_presigned_url(photo) if photo else None
            content = (item.get('content') or '').strip()
            item['content_url'] = (
                get_presigned_url(content) if looks_like_storage_path(content) else None
            )
            attachments = item.get('attachments')
            if not isinstance(attachments, list):
                item['attachments'] = []
                continue
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                path = (attachment.get('path') or '').strip()
                attachment['content_url'] = get_presigned_url(path) if path else None
        return result

    def get_reply_to_summary(self, obj):
        reply = obj.reply_to
        if reply is None:
            return None
        preview = (reply.content or '').strip()
        if reply.deleted_at:
            preview = 'Сообщение удалено'
        elif looks_like_storage_path(preview):
            preview = reply.file_name or f'[{reply.message_type}]'
        return {
            'id': str(reply.id),
            'chat': str(reply.chat_id),
            'sender': {
                'id': str(reply.sender_id),
                'nickname': reply.sender.nickname,
                'first_name': reply.sender.first_name,
                'last_name': reply.sender.last_name,
            },
            'preview': preview[:160],
            'message_type': reply.message_type,
        }


class ChatListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    chat_type = serializers.ChoiceField(choices=['direct', 'group'])
    is_group = serializers.BooleanField()
    title = serializers.CharField(allow_null=True, allow_blank=True)
    photo = serializers.CharField(allow_null=True, required=False)
    photo_url = serializers.CharField(allow_null=True, required=False)
    partner = UserSerializer(allow_null=True)
    members = serializers.ListField(child=serializers.DictField(), allow_null=True)
    members_count = serializers.IntegerField()
    last_message = MessageSerializer(allow_null=True)
    updated_at = serializers.DateTimeField()
    background_url = serializers.CharField(allow_null=True, required=False)


class CreateGroupChatSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=64, trim_whitespace=True)
    member_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=100,
        allow_empty=False,
    )
    photo = serializers.FileField(required=False, allow_null=True)

    def validate_title(self, value):
        title = (value or '').strip()
        if not title:
            raise serializers.ValidationError('Название группы обязательно')
        if len(title) > 64:
            raise serializers.ValidationError('Название не длиннее 64 символов')
        return title

    def to_internal_value(self, data):
        # Multipart may send member_ids as repeated fields or a JSON string.
        if hasattr(data, 'getlist'):
            import json

            mutable = {}
            for key in data.keys():
                if key == 'member_ids':
                    continue
                value = data.get(key)
                if key == 'photo' and value in ('', None):
                    continue
                mutable[key] = value
            ids = data.getlist('member_ids')
            if ids:
                if len(ids) == 1 and isinstance(ids[0], str) and ids[0].strip().startswith('['):
                    try:
                        mutable['member_ids'] = json.loads(ids[0])
                    except json.JSONDecodeError:
                        mutable['member_ids'] = ids
                else:
                    mutable['member_ids'] = ids
            data = mutable
        return super().to_internal_value(data)


class AddGroupMembersSerializer(serializers.Serializer):
    user_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=100,
        allow_empty=False,
    )


class UpdateChatSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=64, trim_whitespace=True, required=False)

    def validate_title(self, value):
        title = (value or '').strip()
        if not title:
            raise serializers.ValidationError('Название группы обязательно')
        return title


class SendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.UUIDField(required=False)
    message_type = serializers.ChoiceField(
        choices=['text', 'photo', 'file', 'voice', 'code', 'forward']
    )
    content = serializers.CharField()


class ForwardMessagesSerializer(serializers.Serializer):
    source_chat_id = serializers.UUIDField()
    message_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=50,
        allow_empty=False,
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
        trim_whitespace=True,
    )
