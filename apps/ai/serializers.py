from rest_framework import serializers


class CompleteRequestSerializer(serializers.Serializer):
    draft = serializers.CharField(allow_blank=True, max_length=4000)
    chat_id = serializers.UUIDField(required=False, allow_null=True)


class StyleProfileSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
