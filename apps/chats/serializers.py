from rest_framework import serializers

from apps.chats.models import Message
from apps.users.serializers import UserSerializer


class MessageSerializer(serializers.ModelSerializer):
    sender = UserSerializer(read_only=True)

    class Meta:
        model = Message
        fields = ['id', 'chat', 'sender', 'message_type', 'content', 'forwarded_from', 'sent_at']
        read_only_fields = fields


class ChatListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    partner = UserSerializer()
    last_message = MessageSerializer(allow_null=True)
    updated_at = serializers.DateTimeField()


class SendMessageSerializer(serializers.Serializer):
    recipient_id = serializers.UUIDField(required=False)
    message_type = serializers.ChoiceField(choices=['text', 'photo', 'file', 'code', 'forward'])
    content = serializers.CharField()
