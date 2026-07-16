import uuid

from django.conf import settings
from django.db import models


class DevicePlatform(models.TextChoices):
    ANDROID = 'android', 'Android'
    IOS = 'ios', 'iOS'
    WEB = 'web', 'Web'


class DeviceToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='device_tokens',
    )
    token = models.CharField(max_length=512, unique=True)
    platform = models.CharField(
        max_length=16,
        choices=DevicePlatform.choices,
        default=DevicePlatform.ANDROID,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.user_id}:{self.platform}'


class NotificationType(models.TextChoices):
    PRIVATE_INVITE = 'private_invite', 'Приглашение в приватный чат'
    PRIVATE_ACCEPTED = 'private_accepted', 'Приватный чат принят'
    PRIVATE_DECLINED = 'private_declined', 'Приватный чат отклонён'
    PRIVATE_CLOSED = 'private_closed', 'Приватный чат закрыт'
    PRIVATE_CANCELLED = 'private_cancelled', 'Приглашение отменено'
    INFO = 'info', 'Информация'


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    notification_type = models.CharField(max_length=32, choices=NotificationType.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default='')
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user_id}:{self.notification_type}'
