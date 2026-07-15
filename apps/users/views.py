import uuid

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.serializers import (
    EmailSerializer,
    LoginSerializer,
    ProfileSerializer,
    RegistrationTokenSerializer,
    UserSerializer,
    VerifyCodeSerializer,
    complete_registration,
    send_verification_code,
    update_registration_session,
)
from apps.users.services.minio_service import upload_file

User = get_user_model()


def _tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


class RegisterEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            code = send_verification_code(serializer.validated_data['email'])
        except Exception as exc:
            return Response(
                {'detail': f'Не удалось отправить email: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        payload = {'detail': 'Код отправлен на email'}
        # В режиме console/DEBUG показываем код, чтобы можно было пройти регистрацию без SMTP
        if settings.DEBUG and 'console' in settings.EMAIL_BACKEND:
            payload['detail'] = 'SMTP не настроен — код выведен в консоль бэкенда'
            payload['debug_code'] = code
        return Response(payload)


class RegisterVerifyCodeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.users.serializers import verify_code_and_create_session
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = verify_code_and_create_session(
            serializer.validated_data['email'],
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
            'user': UserSerializer(user).data,
            'tokens': _tokens_for_user(user),
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            email=serializer.validated_data['email'],
            password=serializer.validated_data['password'],
        )
        if not user:
            return Response({'detail': 'Неверный email или пароль'}, status=401)
        return Response({
            'user': UserSerializer(user).data,
            'tokens': _tokens_for_user(user),
        })


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
