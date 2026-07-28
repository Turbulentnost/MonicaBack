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
