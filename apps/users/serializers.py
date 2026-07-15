import json
import random
import re
import secrets
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.mail import send_mail
from rest_framework import serializers

User = get_user_model()

NICKNAME_PATTERN = re.compile(r'^[a-zA-Z0-9_]{3,50}$')


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()


class VerifyCodeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)


class ProfileSerializer(serializers.Serializer):
    registration_token = serializers.CharField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(min_length=8, write_only=True)
    nickname = serializers.CharField(max_length=50)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    birth_date = serializers.DateField(required=False, allow_null=True)

    def validate_nickname(self, value):
        if not NICKNAME_PATTERN.match(value):
            raise serializers.ValidationError(
                'Никнейм: 3-50 символов, только латиница, цифры и _'
            )
        if User.objects.filter(nickname__iexact=value).exists():
            raise serializers.ValidationError('Никнейм уже занят')
        return value


class RegistrationTokenSerializer(serializers.Serializer):
    registration_token = serializers.CharField()


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class UserSerializer(serializers.ModelSerializer):
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'role', 'first_name', 'last_name', 'nickname',
            'photo', 'photo_url', 'city', 'birth_date', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_photo_url(self, obj):
        if not obj.photo:
            return None
        from apps.users.services.minio_service import get_presigned_url
        return get_presigned_url(obj.photo)


def _email_code_key(email):
    return f'email_code:{email.lower()}'


def _reg_session_key(token):
    return f'reg_session:{token}'


def send_verification_code(email):
    if User.objects.filter(email__iexact=email).exists():
        raise serializers.ValidationError({'email': 'Пользователь с таким email уже существует'})

    code = f'{random.randint(0, 999999):06d}'
    cache.set(_email_code_key(email), code, settings.REGISTRATION_CODE_TTL)

    send_mail(
        subject='Monica — код подтверждения',
        message=f'Ваш код подтверждения: {code}\nКод действителен 15 минут.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )
    return code


def verify_code_and_create_session(email, code):
    stored = cache.get(_email_code_key(email))
    if not stored or stored != code:
        raise serializers.ValidationError({'code': 'Неверный или просроченный код'})

    cache.delete(_email_code_key(email))
    token = secrets.token_urlsafe(32)
    session_data = {'email': email.lower(), 'step': 'profile'}
    cache.set(_reg_session_key(token), json.dumps(session_data), settings.REGISTRATION_SESSION_TTL)
    return token


def get_registration_session(token):
    raw = cache.get(_reg_session_key(token))
    if not raw:
        raise serializers.ValidationError({'registration_token': 'Сессия регистрации истекла или недействительна'})
    return json.loads(raw)


def update_registration_session(token, data):
    session = get_registration_session(token)
    session.update(data)
    cache.set(_reg_session_key(token), json.dumps(session), settings.REGISTRATION_SESSION_TTL)
    return session


def delete_registration_session(token):
    cache.delete(_reg_session_key(token))


def complete_registration(token):
    session = get_registration_session(token)
    required = ['email', 'first_name', 'last_name', 'password', 'nickname']
    for field in required:
        if not session.get(field):
            raise serializers.ValidationError({'registration_token': f'Не заполнено поле: {field}'})

    if User.objects.filter(email__iexact=session['email']).exists():
        raise serializers.ValidationError({'registration_token': 'Пользователь уже существует'})

    birth_date = session.get('birth_date')
    if birth_date:
        from datetime import date
        birth_date = date.fromisoformat(birth_date)

    user = User.objects.create_user(
        email=session['email'],
        password=session['password'],
        first_name=session['first_name'],
        last_name=session['last_name'],
        nickname=session['nickname'],
        city=session.get('city', ''),
        birth_date=birth_date,
        photo=session.get('photo', ''),
    )
    delete_registration_session(token)
    return user
