from django.apps import AppConfig


class LmStudioConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.lmstudio'
    label = 'lmstudio'
    verbose_name = 'LM Studio proxy'
