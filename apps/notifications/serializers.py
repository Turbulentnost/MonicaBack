from rest_framework import serializers

from apps.notifications.models import DevicePlatform, DeviceToken, Notification


class DeviceTokenSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
    platform = serializers.ChoiceField(
        choices=DevicePlatform.choices,
        default=DevicePlatform.ANDROID,
    )


class DeviceTokenResponseSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ('id', 'token', 'platform', 'is_active', 'created_at', 'updated_at')
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = (
            'id', 'notification_type', 'title', 'body', 'payload',
            'is_read', 'created_at',
        )
        read_only_fields = fields
