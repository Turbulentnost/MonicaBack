import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserRole(models.TextChoices):
    ADMIN = 'admin', 'Админ'
    USER = 'user', 'Пользователь'


class UserManager(BaseUserManager):
    def create_user(self, email=None, password=None, **extra_fields):
        nickname = extra_fields.pop('nickname', None)
        phone = extra_fields.pop('phone', None)
        if not nickname:
            raise ValueError('Никнейм обязателен')
        if email:
            email = self.normalize_email(email)
        else:
            email = None
        user = self.model(
            nickname=nickname,
            email=email,
            phone=phone or None,
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, nickname, password=None, email=None, phone=None, **extra_fields):
        extra_fields.setdefault('role', UserRole.ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('first_name', 'Admin')
        extra_fields.setdefault('last_name', 'Admin')
        extra_fields['nickname'] = nickname
        if phone:
            extra_fields['phone'] = phone
        elif 'phone' not in extra_fields:
            extra_fields['phone'] = f'000{uuid.uuid4().hex[:11]}'
        return self.create_user(email=email, password=password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Optional — can be attached later in settings.
    email = models.EmailField(unique=True, null=True, blank=True)
    # Optional for now (registration uses email).
    phone = models.CharField(max_length=32, unique=True, null=True, blank=True)
    role = models.CharField(max_length=10, choices=UserRole.choices, default=UserRole.USER)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    nickname = models.CharField(max_length=50, unique=True)
    photo = models.CharField(max_length=512, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    birth_date = models.DateField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = 'nickname'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.nickname


class UserBlock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    blocker = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='blocks_created',
    )
    blocked = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='blocks_received',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['blocker', 'blocked'],
                name='users_userblock_unique_pair',
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.blocker_id}->{self.blocked_id}'
