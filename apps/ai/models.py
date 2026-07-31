from django.conf import settings
from django.db import models
from pgvector.django import HnswIndex, VectorField

# Keep in sync with settings.AI_EMBEDDING_DIMS (bge-m3 = 1024).
EMBEDDING_DIMS = 1024


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


class MessageEmbedding(models.Model):
    message = models.OneToOneField(
        'chats.Message',
        on_delete=models.CASCADE,
        related_name='embedding',
    )
    chat = models.ForeignKey(
        'chats.Chat',
        on_delete=models.CASCADE,
        related_name='message_embeddings',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='message_embeddings',
    )
    embedding = VectorField(dimensions=EMBEDDING_DIMS)
    content_hash = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Message embedding'
        verbose_name_plural = 'Message embeddings'
        indexes = [
            HnswIndex(
                name='ai_msg_emb_hnsw_cosine',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
            models.Index(fields=['chat', 'created_at'], name='ai_msg_emb_chat_created'),
        ]

    def __str__(self):
        return f'emb:msg:{self.message_id}'


class ChatTopicSegment(models.Model):
    chat = models.ForeignKey(
        'chats.Chat',
        on_delete=models.CASCADE,
        related_name='topic_segments',
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    anchor_message = models.ForeignKey(
        'chats.Message',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )
    label = models.CharField(max_length=160, blank=True, default='')
    centroid = VectorField(dimensions=EMBEDDING_DIMS, null=True, blank=True)
    message_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Chat topic segment'
        verbose_name_plural = 'Chat topic segments'
        indexes = [
            models.Index(fields=['chat', 'ended_at'], name='ai_topic_seg_chat_ended'),
            models.Index(fields=['chat', '-started_at'], name='ai_topic_seg_chat_started'),
        ]

    def __str__(self):
        return f'topic:{self.chat_id}:{self.id}'
