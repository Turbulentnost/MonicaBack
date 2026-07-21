from rest_framework import serializers

from apps.chats.models import CallMediaMode, CallSession
from apps.users.serializers import UserSerializer


class CallSessionSerializer(serializers.ModelSerializer):
    caller = UserSerializer(read_only=True)
    callee = UserSerializer(read_only=True)

    class Meta:
        model = CallSession
        fields = [
            'id',
            'chat',
            'caller',
            'callee',
            'status',
            'media_mode',
            'client_instance_id',
            'accepted_client_instance_id',
            'created_at',
            'accepted_at',
            'connected_at',
            'ended_at',
            'ended_by',
            'end_reason',
        ]
        read_only_fields = fields


class StartCallSerializer(serializers.Serializer):
    client_instance_id = serializers.UUIDField()
    media_mode = serializers.ChoiceField(
        choices=CallMediaMode.choices,
        required=False,
        default=CallMediaMode.AUDIO,
    )


class AcceptCallSerializer(serializers.Serializer):
    client_instance_id = serializers.UUIDField()


class EndCallSerializer(serializers.Serializer):
    end_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
    )


class MediaModeSerializer(serializers.Serializer):
    media_mode = serializers.ChoiceField(choices=CallMediaMode.choices)
