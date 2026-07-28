from django.contrib import admin

from apps.ai.models import PartnerStyleProfile, UserStyleProfile


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
