from django.conf import settings
from django.db import models


class UserStyleProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='style_profile',
    )
    enabled = models.BooleanField(default=True)
    samples = models.JSONField(default=list, blank=True)
    traits = models.JSONField(default=dict, blank=True)
    messages_since_traits = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User style profile'
        verbose_name_plural = 'User style profiles'

    def __str__(self):
        return f'style:{self.user_id}'


class PartnerStyleProfile(models.Model):
    """How this user tends to talk specifically with a given partner."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='partner_styles',
    )
    partner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='partner_styles_about',
    )
    chat = models.ForeignKey(
        'chats.Chat',
        on_delete=models.CASCADE,
        related_name='partner_styles',
        null=True,
        blank=True,
    )
    notes = models.TextField(blank=True, default='')
    traits = models.JSONField(default=dict, blank=True)
    messages_since_refresh = models.PositiveIntegerField(default=0)
    last_day_key = models.CharField(max_length=10, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Partner style profile'
        verbose_name_plural = 'Partner style profiles'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'partner'],
                name='ai_partnerstyle_user_partner_uniq',
            ),
        ]

    def __str__(self):
        return f'style:{self.user_id}->partner:{self.partner_id}'
