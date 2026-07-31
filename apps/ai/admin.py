from django.contrib import admin

from apps.ai.models import (
    ChatTopicSegment,
    MessageEmbedding,
    PartnerStyleProfile,
    UserStyleProfile,
)


@admin.register(UserStyleProfile)
class UserStyleProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'enabled', 'updated_at')
    list_filter = ('enabled',)
    search_fields = ('user__nickname', 'user__email')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(PartnerStyleProfile)
class PartnerStyleProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'partner', 'last_day_key', 'updated_at')
    search_fields = ('user__nickname', 'partner__nickname')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(MessageEmbedding)
class MessageEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('message', 'chat', 'user', 'updated_at')
    search_fields = ('message__id', 'chat__id')
    readonly_fields = ('created_at', 'updated_at', 'content_hash')


@admin.register(ChatTopicSegment)
class ChatTopicSegmentAdmin(admin.ModelAdmin):
    list_display = ('chat', 'label', 'started_at', 'ended_at', 'message_count')
    search_fields = ('chat__id', 'label')
    readonly_fields = ('created_at', 'updated_at')
