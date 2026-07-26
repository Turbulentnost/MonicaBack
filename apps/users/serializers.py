import json
import random
import re
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.mail import send_mail
from rest_framework import serializers

from apps.users.services.phone import format_phone_display

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
    login = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.CharField(max_length=150, required=False, allow_blank=True)  # legacy
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        identifier = (attrs.get('login') or attrs.get('email') or '').strip()
        if not identifier:
            raise serializers.ValidationError({'login': 'Укажите email или никнейм'})
        attrs['login'] = identifier
        return attrs


class UserSerializer(serializers.ModelSerializer):
    photo_url = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    phone_display = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'phone', 'phone_display', 'role',
            'first_name', 'last_name', 'nickname',
            'photo', 'photo_url', 'is_online', 'last_seen_at', 'city', 'birth_date',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_phone_display(self, obj):
        return format_phone_display(obj.phone or '')

    def get_photo_url(self, obj):
        if not obj.photo:
            return None
        from urllib.parse import quote

        from apps.users.services.minio_service import get_presigned_url

        url = get_presigned_url(obj.photo)
        if url:
            return url

        request = self.context.get('request')
        path = f'/api/media/?path={quote(obj.photo, safe="")}'
        if request is not None:
            return request.build_absolute_uri(path)
        return path

    def get_is_online(self, obj):
        from apps.chats.presence import is_user_online
        return is_user_online(obj.id)


class AccountUpdateSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(max_length=150, required=False)
    last_name = serializers.CharField(max_length=150, required=False)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    birth_date = serializers.DateField(required=False, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'city', 'birth_date', 'email']

    def validate_first_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Укажите имя')
        return value

    def validate_last_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Укажите фамилию')
        return value

    def validate_city(self, value):
        return value.strip()

    def validate_email(self, value):
        value = (value or '').strip().lower()
        if not value:
            return None
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('Этот email уже занят')
        return value


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
        raise serializers.ValidationError(
            {'registration_token': 'Сессия регистрации истекла или недействительна'}
        )
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
            raise serializers.ValidationError(
                {'registration_token': f'Не заполнено поле: {field}'}
            )

    if User.objects.filter(email__iexact=session['email']).exists():
        raise serializers.ValidationError({'registration_token': 'Пользователь уже существует'})
    if User.objects.filter(nickname__iexact=session['nickname']).exists():
        raise serializers.ValidationError({'nickname': 'Никнейм уже занят'})

    birth_date = session.get('birth_date')
    if birth_date:
        from datetime import date
        birth_date = date.fromisoformat(birth_date)

    user = User.objects.create_user(
        email=session['email'],
        password=session['password'],
        nickname=session['nickname'],
        phone=None,
        first_name=session['first_name'],
        last_name=session['last_name'],
        city=session.get('city', ''),
        birth_date=birth_date,
        photo=session.get('photo', ''),
    )
    delete_registration_session(token)
    return user
