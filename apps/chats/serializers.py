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

    class Meta:
        model = Message
        fields = [
            'id', 'chat', 'sender', 'message_type', 'content', 'content_url', 'caption',
            'file_name', 'mime_type', 'file_size', 'attachments',
            'forwarded_from', 'sent_at', 'edited_at', 'read_at', 'waveform', 'voice_duration_ms',
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


class ChatListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    partner = UserSerializer()
    last_message = MessageSerializer(allow_null=True)
    updated_at = serializers.DateTimeField()


class SendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.UUIDField(required=False)
    message_type = serializers.ChoiceField(
        choices=['text', 'photo', 'file', 'voice', 'code', 'forward']
    )
    content = serializers.CharField()
