from rest_framework import serializers

from apps.chats.models import Message
from apps.users.serializers import UserSerializer
from apps.users.services.minio_service import get_presigned_url


class MessageSerializer(serializers.ModelSerializer):
    sender = UserSerializer(read_only=True)
    content_url = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id', 'chat', 'sender', 'message_type', 'content', 'content_url',
            'file_name', 'mime_type', 'file_size', 'forwarded_from', 'sent_at',
        ]
        read_only_fields = fields

    def get_content_url(self, obj):
        if obj.message_type in ('photo', 'file') and obj.content:
            return get_presigned_url(obj.content)
        return None


class ChatListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    partner = UserSerializer()
    last_message = MessageSerializer(allow_null=True)
    updated_at = serializers.DateTimeField()


class SendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.UUIDField(required=False)
    message_type = serializers.ChoiceField(choices=['text', 'photo', 'file', 'code', 'forward'])
    content = serializers.CharField()
