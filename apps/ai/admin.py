from django.contrib import admin

from apps.ai.models import UserStyleProfile


@admin.register(UserStyleProfile)
class UserStyleProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'enabled', 'updated_at')
    list_filter = ('enabled',)
    search_fields = ('user__nickname', 'user__email')
    readonly_fields = ('created_at', 'updated_at')
