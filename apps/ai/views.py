from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai.serializers import CompleteRequestSerializer, StyleProfileSerializer
from apps.ai.services import complete_draft, get_or_create_style_profile


class CompleteView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CompleteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        draft = serializer.validated_data.get('draft') or ''
        chat_id = serializer.validated_data.get('chat_id')
        result = complete_draft(
            request.user,
            draft,
            chat_id=str(chat_id) if chat_id else None,
        )
        return Response(result)


class StyleProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = get_or_create_style_profile(request.user)
        samples = profile.samples if isinstance(profile.samples, list) else []
        return Response({
            'enabled': profile.enabled,
            'samples_count': len(samples),
            'traits': profile.traits if isinstance(profile.traits, dict) else {},
        })

    def patch(self, request):
        serializer = StyleProfileSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = get_or_create_style_profile(request.user)
        if 'enabled' in serializer.validated_data:
            profile.enabled = bool(serializer.validated_data['enabled'])
            profile.save(update_fields=['enabled', 'updated_at'])
        samples = profile.samples if isinstance(profile.samples, list) else []
        return Response({
            'enabled': profile.enabled,
            'samples_count': len(samples),
            'traits': profile.traits if isinstance(profile.traits, dict) else {},
        }, status=status.HTTP_200_OK)
