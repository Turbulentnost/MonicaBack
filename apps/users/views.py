import uuid
from mimetypes import guess_type

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.http import HttpResponse
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.serializers import (
    AccountUpdateSerializer,
    LoginSerializer,
    PhoneSerializer,
    ProfileSerializer,
    RegistrationTokenSerializer,
    UserSerializer,
    VerifyCodeSerializer,
    complete_registration,
    send_verification_code,
    update_registration_session,
)
from apps.users.services.minio_service import delete_object, download_object_bytes, upload_file
from apps.users.services.telegram_auth import handle_telegram_update
from apps.users.services.telegram_bot import telegram_configured

User = get_user_model()


def _tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


class RegisterPhoneView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PhoneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = send_verification_code(serializer.validated_data['phone'])
        except drf_serializers.ValidationError:
            raise
        except Exception as exc:
            return Response(
                {'detail': f'Не удалось начать подтверждение: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        payload = {
            'phone': result['phone'],
            'channel': result.get('channel') or 'telegram',
            'detail': result.get('detail') or 'Откройте Telegram, чтобы получить код',
            'telegram_url': result.get('telegram_url') or '',
            'bot_username': result.get('bot_username') or '',
        }
        if result.get('debug_code') and (settings.DEBUG or not telegram_configured()):
            payload['debug_code'] = result['debug_code']
            payload['detail'] = 'Telegram-бот не настроен — используйте код из ответа (dev)'
        return Response(payload)


class TelegramWebhookView(APIView):
    """Telegram Bot webhook — OTP from deep-link /start=<token> bound to phone."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        secret = getattr(settings, 'TELEGRAM_WEBHOOK_SECRET', '') or ''
        if secret:
            header = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if header != secret:
                return Response({'detail': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        try:
            handle_telegram_update(request.data if isinstance(request.data, dict) else {})
        except Exception:
            # Always 200 so Telegram does not retry aggressively on app bugs.
            import logging
            logging.getLogger(__name__).exception('Telegram webhook handler failed')
        return Response({'ok': True})


class RegisterVerifyCodeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.users.serializers import verify_code_and_create_session
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = verify_code_and_create_session(
            serializer.validated_data['phone'],
            serializer.validated_data['code'],
        )
        return Response({'registration_token': token})


class RegisterProfileView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        token = data.pop('registration_token')
        birth_date = data.pop('birth_date', None)
        update_registration_session(token, {
            **data,
            'birth_date': birth_date.isoformat() if birth_date else None,
        })
        return Response({'detail': 'Профиль сохранён'})


class RegisterAvatarView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get('registration_token')
        photo = request.FILES.get('photo')
        if not token:
            return Response({'registration_token': 'Обязательное поле'}, status=400)
        if not photo:
            return Response({'photo': 'Файл обязателен'}, status=400)

        from apps.users.serializers import get_registration_session
        get_registration_session(token)

        ext = photo.name.rsplit('.', 1)[-1].lower() if '.' in photo.name else 'jpg'
        object_name = f'{uuid.uuid4().hex}.{ext}'
        path = upload_file(
            settings.MINIO_BUCKET_AVATARS,
            object_name,
            photo,
            photo.content_type or 'image/jpeg',
        )
        update_registration_session(token, {'photo': path})
        return Response({'photo': path})


class RegisterCompleteView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegistrationTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = complete_registration(serializer.validated_data['registration_token'])
        return Response({
            'user': UserSerializer(user, context={'request': request}).data,
            'tokens': _tokens_for_user(user),
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Accept both "login" and legacy "email" field name from older clients.
        identifier = serializer.validated_data['login']
        if not identifier and request.data.get('email'):
            identifier = request.data.get('email')
        user = authenticate(
            request,
            login=identifier,
            password=serializer.validated_data['password'],
        )
        if not user:
            return Response(
                {'detail': 'Неверный телефон, никнейм, email или пароль'},
                status=401,
            )
        return Response({
            'user': UserSerializer(user, context={'request': request}).data,
            'tokens': _tokens_for_user(user),
        })


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user, context={'request': request}).data)

    def patch(self, request):
        serializer = AccountUpdateSerializer(
            request.user,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user, context={'request': request}).data)


class MeAvatarView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        photo = request.FILES.get('photo')
        if not photo:
            return Response({'photo': 'Файл обязателен'}, status=400)

        allowed_types = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
        content_type = (photo.content_type or '').lower()
        if content_type not in allowed_types:
            return Response(
                {'photo': 'Поддерживаются JPG, PNG, WEBP и GIF'},
                status=400,
            )
        max_size_mb = 10
        max_size = max_size_mb * 1024 * 1024
        if photo.size > max_size:
            return Response(
                {'photo': f'Файл больше {max_size_mb} МБ'},
                status=400,
            )

        ext = photo.name.rsplit('.', 1)[-1].lower() if '.' in photo.name else 'jpg'
        object_name = f'{request.user.id}/{uuid.uuid4().hex}.{ext}'
        path = upload_file(
            settings.MINIO_BUCKET_AVATARS,
            object_name,
            photo,
            content_type,
        )
        old_photo = request.user.photo
        request.user.photo = path
        request.user.save(update_fields=['photo', 'updated_at'])
        if old_photo and old_photo != path:
            delete_object(old_photo)

        return Response(
            UserSerializer(request.user, context={'request': request}).data
        )


class UserAvatarView(APIView):
    """Отдаёт аватар по user id, не полагаясь на presigned URL MinIO."""

    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        try:
            user = User.objects.only('photo', 'updated_at').get(id=user_id)
        except User.DoesNotExist:
            return Response({'detail': 'Пользователь не найден'}, status=404)

        if not user.photo:
            # Не 404: у многих пользователей аватар просто не задан.
            return Response(status=204)

        try:
            data = download_object_bytes(user.photo, max_bytes=20 * 1024 * 1024)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=400)
        if data is None:
            return Response({'detail': 'Аватар не найден в хранилище'}, status=404)

        content_type = guess_type(user.photo)[0] or 'image/jpeg'
        response = HttpResponse(data, content_type=content_type)
        response['Cache-Control'] = 'private, max-age=3600'
        response['ETag'] = f'"{user.photo}:{user.updated_at.timestamp()}"'
        return response
