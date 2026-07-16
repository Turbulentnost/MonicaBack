from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.notifications.models import DeviceToken
from apps.notifications.serializers import DeviceTokenResponseSerializer, DeviceTokenSerializer


class RegisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['token']
        platform = serializer.validated_data['platform']

        # Токен может перейти к другому user (перелогин на том же устройстве)
        DeviceToken.objects.filter(token=token).exclude(user=request.user).delete()

        device, _ = DeviceToken.objects.update_or_create(
            token=token,
            defaults={
                'user': request.user,
                'platform': platform,
                'is_active': True,
            },
        )
        return Response(
            DeviceTokenResponseSerializer(device).data,
            status=status.HTTP_200_OK,
        )


class UnregisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = DeviceToken.objects.filter(
            user=request.user,
            token=serializer.validated_data['token'],
        ).update(is_active=False)
        return Response({'deactivated': updated})
