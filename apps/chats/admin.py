from django.contrib import admin

from apps.chats.models import CallSession, Chat, ChatParticipant, Message


class ChatParticipantInline(admin.TabularInline):
    model = ChatParticipant
    extra = 0


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'updated_at')
    inlines = [ChatParticipantInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat', 'sender', 'message_type', 'is_pinned', 'sent_at')
    list_filter = ('message_type', 'is_pinned')


@admin.register(CallSession)
class CallSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'caller', 'callee', 'status', 'created_at', 'ended_at')
    list_filter = ('status',)
