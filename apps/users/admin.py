from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.users.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('nickname', 'phone', 'email', 'role', 'first_name', 'last_name', 'is_active')
    list_filter = ('role', 'is_active', 'is_staff')
    search_fields = ('nickname', 'phone', 'email', 'first_name', 'last_name')
    ordering = ('-created_at',)
    fieldsets = (
        (None, {'fields': ('nickname', 'password')}),
        ('Контакты', {'fields': ('phone', 'email')}),
        ('Профиль', {'fields': ('first_name', 'last_name', 'photo', 'city', 'birth_date', 'role')}),
        ('Права', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'nickname', 'phone', 'email',
                'first_name', 'last_name',
                'password1', 'password2', 'role',
            ),
        }),
    )
