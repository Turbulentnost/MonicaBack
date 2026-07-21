import uuid

from django.conf import settings
from django.db import models


class MessageType(models.TextChoices):
    TEXT = 'text', 'Текст'
    PHOTO = 'photo', 'Фото'
    FILE = 'file', 'Файл'
    VOICE = 'voice', 'Голосовое сообщение'
    CODE = 'code', 'Код'
    FORWARD = 'forward', 'Пересылка'


class Chat(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return str(self.id)


class ChatParticipant(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_participations')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('chat', 'user')

    def __str__(self):
        return f'{self.user.nickname} in {self.chat_id}'


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_messages')
    message_type = models.CharField(max_length=10, choices=MessageType.choices, default=MessageType.TEXT)
    content = models.TextField()
    file_name = models.CharField(max_length=255, blank=True, default='')
    mime_type = models.CharField(max_length=128, blank=True, default='')
    file_size = models.PositiveIntegerField(null=True, blank=True)
    waveform = models.JSONField(default=list, blank=True)
    # Gallery items: [{path, file_name, mime_type, file_size}, ...]
    attachments = models.JSONField(default=list, blank=True)
    voice_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    forwarded_from = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='forwards'
    )
    sent_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deleted_messages',
    )

    class Meta:
        ordering = ['sent_at']

    def __str__(self):
        return f'{self.sender.nickname}: {self.content[:50]}'


class MessageHidden(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hidden_messages')
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='hidden_for')
    hidden_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'message')


class PrivateSessionStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает'
    ACTIVE = 'active', 'Активен'
    DECLINED = 'declined', 'Отклонён'
    CLOSED = 'closed', 'Закрыт'


class PrivateSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name='private_sessions')
    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='private_sessions_started',
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='private_sessions_received',
    )
    status = models.CharField(
        max_length=16,
        choices=PrivateSessionStatus.choices,
        default=PrivateSessionStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.id}:{self.status}'


class CallStatus(models.TextChoices):
    RINGING = 'ringing', 'Вызов'
    ACTIVE = 'active', 'Активен'
    REJECTED = 'rejected', 'Отклонён'
    CANCELLED = 'cancelled', 'Отменён'
    MISSED = 'missed', 'Пропущен'
    ENDED = 'ended', 'Завершён'
    FAILED = 'failed', 'Ошибка'


class CallMediaMode(models.TextChoices):
    AUDIO = 'audio', 'Аудио'
    VIDEO = 'video', 'Видео'


class CallSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name='calls')
    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calls_started',
    )
    callee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='calls_received',
    )
    status = models.CharField(
        max_length=16,
        choices=CallStatus.choices,
        default=CallStatus.RINGING,
    )
    media_mode = models.CharField(
        max_length=8,
        choices=CallMediaMode.choices,
        default=CallMediaMode.AUDIO,
    )
    client_instance_id = models.UUIDField()
    accepted_client_instance_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='calls_ended',
    )
    end_reason = models.CharField(max_length=64, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['caller', 'client_instance_id'],
                condition=models.Q(status__in=['ringing', 'active']),
                name='unique_active_call_client_instance',
            ),
        ]
        indexes = [
            models.Index(fields=['caller', 'status']),
            models.Index(fields=['callee', 'status']),
        ]

    def __str__(self):
        return f'{self.id}:{self.status}'
